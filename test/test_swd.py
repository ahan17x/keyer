# SPDX-License-Identifier: Apache-2.0
"""fw/swd.s on the RTL, in lockstep with the golden model.

Run from test/:
  make SIM_BUILD=sim_build/swd COCOTB_RESULTS_FILE=sim_build/swd/results.xml COCOTB_TEST_MODULES=test_swd

The SW-DP target model (tools/protomodels_swd.py) answers both machines.
SWD_HALF is 12, the fastest the firmware supports; only the program's words
are loaded (three quarters of each test is that SPI load).
"""

import cocotb

from protomodels_swd import ACK_OK, ACK_WAIT, SwdTargetModel
from test import GL, fw, lockstep_firmware

HALF = 12
DPIDR = 0x2BA01477
CONNECT, XFER, IDLE = 1, 2, 3
RD_DPIDR, RD_CTRL, WR_CTRL = 0xA5, 0x8D, 0xA9


def le32(v):
    return [(v >> (8 * i)) & 0xFF for i in range(4)]


async def session(dut, cmds, target, clocks):
    words, syms = fw("swd.s", {"SWD_HALF": HALF})
    words = words[:max(a for a, w in enumerate(words) if w) + 1]
    ls, spi = await lockstep_firmware(dut, words, 2 * HALF * clocks + 300, models=[target], inbox0=cmds)
    t = ls.m.threads[0]
    assert t.running and t.blocked and t.pc == syms["swd_cmd"], t          # back at the command loop
    assert target.errors == [], target.errors[:3]
    assert len({c % HALF for c in target.rises + target.falls}) == 1       # one clock grid
    assert {r - f for f, r in zip(target.falls, target.rises[1:])} == {HALF}
    return await spi.drain_outbox(0)


def target(**kw):
    return SwdTargetModel(swclk=19, swdio=0, dpidr=DPIDR, setup=HALF - 5, hold=8, min_high=HALF, min_low=HALF, **kw)


@cocotb.test(skip=GL)
async def test_lockstep_swd_connect_dpidr_write_read(dut):
    """Connect from the dormant state, read DPIDR, write CTRL/STAT, read it back."""
    tg = target()
    value = 0x50000F01
    cmds = [CONNECT, XFER, RD_DPIDR, XFER, WR_CTRL] + le32(value) + [IDLE, XFER, RD_CTRL]
    got = await session(dut, cmds, tg, 152 + 46 + 46 + 8 + 46 + 12)
    assert got == [ACK_OK] + le32(DPIDR) + [0] + [ACK_OK] + [ACK_OK] + le32(value) + [0], got
    assert [e[0] for e in tg.events] == ["line reset", "swd selected", "line reset"]
    assert tg.ctrlstat == value and [t["kind"] for t in tg.transactions] == ["r", "w", "r"]


@cocotb.test(skip=GL)
async def test_lockstep_swd_wait_parity_error_and_silence(dut):
    """A WAIT response (no data phase), a read whose parity bit is wrong
    (reported), a request with bad parity (no answer: 7), and after IDLE a
    good read."""
    tg = target(selected=True)
    tg.next_ack = ACK_WAIT

    class Corrupt:                            # corrupt the parity of the second answered request
        def on_cycle(self, m):
            if len(tg.transactions) == 1 and not tg.transactions[0].get("seen"):
                tg.transactions[0]["seen"] = True
                tg.corrupt_parity = True

    words, syms = fw("swd.s", {"SWD_HALF": HALF})
    cmds = [XFER, RD_DPIDR, XFER, RD_DPIDR, XFER, RD_CTRL ^ 0x20, IDLE, XFER, RD_DPIDR]
    words = words[:max(a for a, w in enumerate(words) if w) + 1]
    ls, spi = await lockstep_firmware(dut, words, 2 * HALF * (13 + 46 + 13 + 8 + 46 + 12) + 300,
                                      models=[tg, Corrupt()], inbox0=cmds)
    got = await spi.drain_outbox(0)
    assert got == [ACK_WAIT] + [ACK_OK] + le32(DPIDR) + [1] + [7] + [ACK_OK] + le32(DPIDR) + [0], got
    assert tg.errors == [], tg.errors[:3]
    assert [t["ack"] for t in tg.transactions] == [ACK_WAIT, ACK_OK, ACK_OK]
