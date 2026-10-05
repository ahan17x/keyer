# SPDX-License-Identifier: Apache-2.0
"""fw/ps2_host.s on the RTL in lockstep with the golden model, against the
PS/2 device model (tools/protomodels_ps2_host.py). The same scenarios as
tools/test_fw_ps2_host.py, with fast timing constants.

Run: make SIM_BUILD=sim_build/ps2_host COCOTB_RESULTS_FILE=sim_build/ps2_host/results.xml \
          COCOTB_TEST_MODULES=test_ps2_host
"""

import cocotb
from cocotb.triggers import ClockCycles

from keyer_tb import R_INBOX0
from protomodels_ps2_host import Ps2DeviceModel
from test import GL, fw, lockstep_firmware

ESC = 0xA5
EV_PARITY, EV_RX_TMO, EV_NOACK = 1, 3, 4


def ps2_fw(symbols):
    """The image, cut to its length so the SPI load is short."""
    words, syms = fw("ps2_host.s", symbols)
    used = max(i for i, w in enumerate(words) if w) + 1
    return words[:used], syms


class When:
    """A model that calls action() once, `after` cycles after cond(m) first holds."""

    def __init__(self, cond, action, after=0):
        self.cond, self.action, self.after, self.at = cond, action, after, None

    def on_cycle(self, m):
        if self.at is None:
            if self.cond(m):
                self.at = m.cycle + self.after
        if self.action is not None and self.at is not None and m.cycle >= self.at:
            self.action()
            self.action = None


def quiet(ls, syms, dut):
    """The thread is in its polling loop and both lines are released."""
    t = ls.m.threads[0]
    assert t.running and t.pc in (syms["idle"], syms["idle"] + 1), "pc=0x%02X" % t.pc
    assert int(dut.uio_oe.value) & 0x03 == 0
    assert int(dut.uio_out.value) & 0x03 == 0          # open-drain: never driven high


@cocotb.test(skip=GL)
async def test_lockstep_ps2_host(dut):
    """A command with its answer, scan codes (one equal to the escape), a
    parity error, a frame abandoned half way and the good frame after it, and
    a command the device does not acknowledge."""
    H, TICK, INH, RTS, TMO = 24, 28, 3, 20, 22
    words, syms = ps2_fw({"PS2_TICK": TICK, "PS2_INH_TICKS": INH, "PS2_RTS_TICKS": RTS,
                          "PS2_TMO_TICKS": TMO})
    dev = Ps2DeviceModel(clk=0, data=1, half=H, min_inhibit=INH * TICK - 4)
    dev.replies[0xFF] = [0xFA, 0xAA]
    dev.acks = [True, False]

    def frames():
        dev.send(0x1C)
        dev.send(ESC)
        dev.send(0x55, parity_error=True)
        dev.send(0x77, stop_after=5)
        dev.send(0x5A, delay=TMO * TICK)

    script = When(lambda m: len(dev.sent) == 2, frames)       # once the answer to Reset is out
    received = [0xFA, 0xAA, 0x1C, ESC, ESC, ESC, EV_PARITY, ESC, EV_RX_TMO, 0x5A]

    async def host(spi):
        while (await spi.levels())[1] < len(received):
            await ClockCycles(dut.clk, 100)
        await spi.write(R_INBOX0, [0xED])                     # this one gets no ACK

    ls, spi = await lockstep_firmware(dut, words, 1200, models=[script, dev], inbox0=[0xFF],
                                      after_load=host)
    dut._log.info("lockstep cycles: %d, instructions retired: %d", ls.cycle, ls.retired)
    quiet(ls, syms, dut)
    got = await spi.drain_outbox(0)
    assert got == received + [ESC, EV_NOACK], [hex(b) for b in got]
    assert dev.errors == [], dev.errors[:3]
    assert dev.commands == [0xFF, 0xED]
    assert [acked for _, _, _, acked in dev.command_log] == [True, False]
    assert dev.sent == [0xFA, 0xAA, 0x1C, ESC, 0x55, 0x5A]
    assert [b for _, b in dev.abandoned] == [0x77] and dev.aborted == []
    assert len(dev.inhibits) == 2 and all(n >= INH * TICK for _, n in dev.inhibits)


@cocotb.test(skip=GL)
async def test_lockstep_ps2_host_full_outbox(dut):
    """The host does not read: the outbox fills, the firmware inhibits the
    device (CLK held low) with the seventeenth byte in hand, and every byte
    arrives in order once the host reads."""
    H, TICK, INH, IDLE = 12, 16, 3, 72
    words, syms = ps2_fw({"PS2_TICK": TICK, "PS2_INH_TICKS": INH, "PS2_RTS_TICKS": 20,
                          "PS2_TMO_TICKS": 20})
    dev = Ps2DeviceModel(clk=0, data=1, half=H, min_inhibit=INH * TICK - 4, idle=IDLE)
    data = [(11 * i + 5) & 0xFF for i in range(18)]
    assert ESC not in data

    def frames():
        for b in data:
            dev.send(b)

    script = When(lambda m: m.threads[0].running, frames, after=40)   # the firmware is polling by then
    seen = {}

    async def host(spi):
        while (await spi.levels())[1] < 16:
            await ClockCycles(dut.clk, 100)
        await ClockCycles(dut.clk, 2 * (22 * H + IDLE))      # time for two more frames, were the bus free
        seen["oe"] = int(dut.uio_oe.value) & 0x03
        seen["sent"] = list(dev.sent)
        seen["state"] = dev.state
        seen["first"] = await spi.drain_outbox(0)

    ls, spi = await lockstep_firmware(dut, words, 600, models=[script, dev], after_load=host)
    dut._log.info("lockstep cycles: %d, instructions retired: %d", ls.cycle, ls.retired)
    quiet(ls, syms, dut)
    rest = await spi.drain_outbox(0)
    assert seen["oe"] == 0x01                                # CLK held low by the firmware, DATA released
    assert seen["sent"] == data[:17] and seen["state"] == "inhibit"
    assert seen["first"] == data[:16], [hex(b) for b in seen["first"]]
    assert seen["first"] + rest == data, [hex(b) for b in rest]
    assert dev.sent == data and dev.errors == [], dev.errors[:3]
    assert len(dev.inhibits) == 1 and dev.inhibits[0][1] >= 2 * (22 * H + IDLE)
