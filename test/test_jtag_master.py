# SPDX-License-Identifier: Apache-2.0
"""fw/jtag_master.s on the RTL in lockstep with the golden model, with the
JTAG TAP model of tools/protomodels_jtag_master.py on the pins.

Run from this directory:
  make SIM_BUILD=sim_build/jtag_master COCOTB_RESULTS_FILE=sim_build/jtag_master/results.xml \
       COCOTB_TEST_MODULES=test_jtag_master
"""

import cocotb
from cocotb.triggers import ClockCycles

import protomodels_jtag_master as jm
from keyer_tb import R_INBOX0
from test import GL, fw, lockstep_firmware

HALF = 18                                      # TCK half period in cycles: the fastest supported
TCK, TMS, TDI, TDO_UI = 19, 20, 21, 4          # uo3, uo4, uo5, ui4
IDCODE = 0x14B592AB
IDCODE_IR, BYPASS = 0b0010, 0b1111
RESET, DR, IR = 1, 2, 3                        # the firmware's command bytes
RTI = "Run-Test/Idle"


def image():
    """The program without the unused rest of the memory: loading 256 words
    over SPI would be most of the test."""
    words, syms = fw("jtag_master.s", {"JTAG_HALF": HALF})
    while words and words[-1] == 0:
        words.pop()
    return words, syms


def tap_model(start_state):
    return jm.JtagTapModel(tck=TCK, tms=TMS, tdi=TDI, tdo_ui_bit=TDO_UI, setup=HALF - 6, hold=HALF + 2,
                           min_high=HALF, min_low=HALF, idcode=IDCODE, ir_len=4, idcode_ir=IDCODE_IR,
                           start_state=start_state)


def scan(cmd, n, value):
    return [cmd, n & 0xFF] + list(value.to_bytes((n + 7) // 8, "little"))


def on_grid(tap):
    r, f = tap.tck_rises, tap.tck_falls
    return (len(r) == len(f) and all((e - r[0]) % HALF == 0 for e in r + f)
            and all(b - a == HALF for a, b in zip(r, f)))


@cocotb.test(skip=GL)
async def test_lockstep_jtag_reset_idcode_bypass(dut):
    """TAP reset from Pause-IR, IDCODE read, an IR scan selecting BYPASS and a
    13-bit DR scan through it: RTL and golden model agree every cycle, the
    TAP model sees a legal waveform, the host reads the right bytes."""
    words, syms = image()
    data = 0x1BA5
    cmds = [RESET] + scan(DR, 32, 0) + scan(IR, 4, BYPASS) + scan(DR, 13, data)
    assert len(cmds) <= 16                                    # all queued in the inbox before the start
    tap = tap_model("Pause-IR")
    ls, spi = await lockstep_firmware(dut, words, 3600, models=[tap], inbox0=cmds)
    assert tap.errors == [], tap.errors[:3]
    assert tap.state == RTI and tap.ir == BYPASS
    states = [s for _, s in tap.state_log]
    assert states[5] == "Test-Logic-Reset" and states[6] == RTI, states[:8]
    got = [(s.kind, s.nbits, s.tdi, s.tdo, s.ir) for s in tap.scans[-3:]]
    assert got == [("DR", 32, 0, IDCODE, IDCODE_IR), ("IR", 4, BYPASS, 0b0001, IDCODE_IR),
                   ("DR", 13, data, (data << 1) & 0x1FFF, BYPASS)], got
    assert len(tap.tck_rises) == 6 + 37 + 10 + 18 and on_grid(tap)
    t = ls.m.threads[0]
    assert t.running and t.blocked and t.pc == syms["jtag_cmd"]
    assert (int(dut.uo_out.value) >> 3) & 3 == 0              # TCK and TMS low at rest
    out = await spi.drain_outbox(0)
    assert out == [0xAB, 0x92, 0xB5, 0x14, 0x01, 0x4A, 0x17], out


@cocotb.test(skip=GL)
async def test_lockstep_jtag_slow_host(dut):
    """The host delivers the second and third data byte of a 20-bit DR scan
    late: the thread waits on POP in the middle of the scan with TCK low and
    picks the timer grid up again (SETD 0 + WAITD 1), in lockstep."""
    words, syms = image()
    data = 0xC5A37
    stream = [RESET] + scan(IR, 4, BYPASS) + scan(DR, 20, data)
    early, late = stream[:-2], stream[-2:]
    tap = tap_model("Shift-DR")

    async def after(spi):
        await ClockCycles(dut.clk, 1700)                      # reset, IR scan and eight bits: about 1200
        await spi.write(R_INBOX0, [late[0]])
        await ClockCycles(dut.clk, 500)                       # the next eight bits: about 300
        await spi.write(R_INBOX0, [late[1]])

    ls, spi = await lockstep_firmware(dut, words, 1200, models=[tap], inbox0=early, after_load=after)
    assert tap.errors == [], tap.errors[:3]
    assert tap.state == RTI
    s = tap.scans[-1]
    assert (s.kind, s.nbits, s.tdi, s.tdo, s.ir) == ("DR", 20, data, (data << 1) & 0xFFFFF, BYPASS), s
    assert on_grid(tap)
    lows = [r - f for f, r in zip(tap.tck_falls, tap.tck_rises[1:])]
    stalls = [tap.state_log[i + 1][1] for i, x in enumerate(lows) if x > 10 * HALF]
    assert stalls == ["Shift-DR", "Shift-DR"], stalls         # nothing else was slow
    t = ls.m.threads[0]
    assert t.running and t.blocked and t.pc == syms["jtag_cmd"]
    out = await spi.drain_outbox(0)
    assert out == [0x01] + list(((data << 1) & 0xFFFFF).to_bytes(3, "little")), out
