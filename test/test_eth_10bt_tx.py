# SPDX-License-Identifier: Apache-2.0
"""fw/eth_10bt_tx.s on the RTL, in lockstep with the golden model.

Run from test/:
  make SIM_BUILD=sim_build/eth COCOTB_RESULTS_FILE=sim_build/eth/results.xml \\
       COCOTB_TEST_MODULES=test_eth_10bt_tx

The 10BASE-T receiver model (tools/protomodels_eth_10bt.py) watches TX+ and
TX- (uio0, uio1) at 40 MHz (25 ns per cycle, two cycles per half-bit) and
checks every link pulse and frame. The link pulse interval is shortened
from 16 ms to 80 us (LP_PERIOD = 400 cycles, LP_TICKS = 8; the receiver's
8..24 ms window scaled by 0.005) so that each test stays under about
60,000 cycles including the program load. The host stages each 16-byte
block with one SPI write to INBOX0 at clk/8 while the firmware runs.
"""

import cocotb

from protomodels_eth_10bt import Eth10BTReceiver
from keyer_tb import R_INBOX0
from test import GL, fw, lockstep_firmware

SYMS = dict(LP_PERIOD=400, LP_TICKS=8)
INTERVAL_NS = 400 * 8 * 25.0
DST, SRC, ETYPE = [0xFF] * 6, [0x02, 0x4B, 0x45, 0x59, 0x45, 0x52], 0x88B5


def expected_payload(blk):
    cmd = blk[0]
    n, k = cmd & 15, cmd >> 4
    p = [cmd] + list(blk[1:1 + n])
    return p + ([i & 0xFF for i in range(96 * k)] if k else [0] * (46 - len(p)))


def check_frame(f, blk):
    assert f.ok and f.fcs_ok, f.errors
    assert (f.dst, f.src, f.type) == (DST, SRC, ETYPE)
    assert f.payload == expected_payload(blk), f.payload


async def eth(dut):
    words, syms = fw("eth_10bt_tx.s", SYMS)
    words = words[:max(a for a, w in enumerate(words) if w) + 1]
    rx = Eth10BTReceiver(clk_ns=25.0, lp_scale=INTERVAL_NS / 16e6)
    ls, spi = await lockstep_firmware(dut, words, 10, models=[rx])
    return ls, spi, rx


async def stage(ls, spi, blk):
    """What a host does: read LEVELS until INBOX0 is empty (a write to a
    full inbox is dropped), then one SPI write of the block; lockstep runs
    while the SPI transactions are on the pins."""
    async def host():
        while (await spi.levels())[0]:
            pass
        await spi.write(R_INBOX0, blk)

    task = cocotb.start_soon(host())
    while not task.done():
        await ls.run(100)


@cocotb.test(skip=GL)
async def test_lockstep_eth_pulses_and_frame(dut):
    """Two link pulses, a frame staged by the host, two more link pulses."""
    ls, spi, rx = await eth(dut)
    await ls.run(7000)
    assert len(rx.pulses) >= 2 and not rx.frames
    blk = [0x04, 0xCA, 0xFE, 0xBA, 0xBE] + [0x77] * 11
    await stage(ls, spi, blk)
    await ls.run(20000, until=lambda _: len(rx.frames) == 1)
    n = len(rx.pulses)
    await ls.run(7000)
    rx.finish()
    dut._log.info("lockstep: %d cycles, %d pulses, frame of %d bytes, start of idle %.0f ns",
                  ls.cycle, len(rx.pulses), len(rx.frames[0].bytes), rx.frames[0].soi_ns)
    assert rx.errors == [], rx.errors[:5]
    (f,) = rx.frames
    check_frame(f, blk)
    assert len(f.bytes) == 64
    assert len(rx.pulses) >= n + 2
    assert all(w == 100.0 for _, w, _ in rx.pulses)
    assert await spi.drain_outbox(0) == [0x04]
    assert ls.cycle < 150000


@cocotb.test(skip=GL)
async def test_lockstep_eth_two_frames(dut):
    """Two frames with different staged payloads; the second block is staged
    while the first frame is on the wire (as soon as LEVELS shows the first
    block has left the inbox) and follows it after the gap."""
    ls, spi, rx = await eth(dut)
    await ls.run(500)
    b1 = [0x03, 0x10, 0x20, 0x30] + [0x55] * 12
    b2 = [0x1F] + list(range(0xE0, 0xEF))
    await stage(ls, spi, b1)
    await ls.run(5000, until=lambda x: x.m.ser.tx_state != 0)
    await stage(ls, spi, b2)
    await ls.run(30000, until=lambda _: len(rx.frames) == 2)
    await ls.run(4000)
    rx.finish()
    f1, f2 = rx.frames
    gap = f2.start - f1.bits_end
    dut._log.info("lockstep: %d cycles, frames of %d and %d bytes, gap %.0f ns",
                  ls.cycle, len(f1.bytes), len(f2.bytes), gap)
    assert rx.errors == [], rx.errors[:5]
    check_frame(f1, b1)
    check_frame(f2, b2)
    assert (len(f1.bytes), len(f2.bytes)) == (64, 14 + 16 + 96 + 4)
    # the firmware's own minimum is 11.55 us (tools/test_fw_eth_10bt_tx.py);
    # here the host's LEVELS polling and 17-byte write may come later
    assert 11550.0 <= gap < 20000.0, gap
    assert await spi.drain_outbox(0) == [0x03, 0x1F]
    assert ls.cycle < 150000
