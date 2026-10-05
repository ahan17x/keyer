# SPDX-License-Identifier: Apache-2.0
"""fw/spi_slave.s on the RTL, in lockstep with the golden model.

Run from test/:
  make SIM_BUILD=sim_build/spi_slave COCOTB_RESULTS_FILE=sim_build/spi_slave/results.xml \
       COCOTB_TEST_MODULES=test_spi_slave

The SPI master model (tools/protomodels_spi_slave.py) clocks both machines
at the fastest SCK the firmware supports (half period 10 cycles). Only the
program's words are loaded, so most of each test is the bus traffic.
"""

import cocotb
from cocotb.triggers import ClockCycles

from protomodels_spi_slave import SpiMasterModel
from test import GL, fw, lockstep_firmware

HALF, CS_SETUP, CS_HOLD, CS_IDLE, AFTER_ABORT = 10, 20, 2, 32, 48


async def bus(dut, replies, frames, half=HALF):
    """Load, queue the reply bytes, run thread 0, then let the master play
    `frames` (keyword arguments of SpiMasterModel.send) in lockstep."""
    words, syms = fw("spi_slave.s")
    words = words[:max(a for a, w in enumerate(words) if w) + 1]
    master = SpiMasterModel(sck_ui_bit=3, mosi_ui_bit=4, csn_ui_bit=5, miso_pin=0, half=half,
                            cs_setup=CS_SETUP, cs_hold=CS_HOLD, cs_idle=CS_IDLE, setup=2, hold=1, t_dis=24)
    played = []

    async def go(spi):                       # the thread has just been started: give it the
        await ClockCycles(dut.clk, 60)       # 40 cycles it needs to stage the first reply
        for kw in frames:
            played.append(master.send(**kw))

    ls, spi = await lockstep_firmware(dut, words, 10, models=[master], inbox0=replies, after_load=go)
    await ls.run(12000, until=lambda _: not master.busy)
    assert not master.busy
    await ls.run(60)
    assert int(dut.uio_oe.value) & 1 == 0                           # MISO released
    return master, played, spi


@cocotb.test(skip=GL)
async def test_lockstep_spi_slave(dut):
    """Frames in both directions at the fastest clock, on both parities of
    the SCK edges; a frame aborted in mid-byte; then a good frame."""
    replies = [0x81, 0x5A, 0x00, 0xFF, 0xC3, 0x3C, 0x7E, 0x99, 0x66]
    frames = [dict(data=[0x9F, 0x01], align=(0, 2)),
              dict(data=[0x03, 0xA5, 0x5A], align=(1, 2)),
              dict(data=[0xF0, 0x0F], bits=11),                      # aborted three bits into byte 2
              dict(data=[0x12, 0x34], gap=AFTER_ABORT - CS_IDLE)]
    master, played, spi = await bus(dut, replies, frames)
    assert master.errors == [], master.errors[:3]
    # the reply stream in order; the byte partly shifted out in the aborted frame (0x7E) is used up
    assert [f.received for f in played] == [[0x81, 0x5A], [0x00, 0xFF, 0xC3], [0x3C], [0x99, 0x66]]
    for f in played:
        assert 0 < f.miso_on - f.cs_fall <= 18 and f.miso_off - f.cs_rise <= 24
    got = await spi.drain_outbox(0)
    assert got == [0x9F, 0x01, 0x03, 0xA5, 0x5A, 0xF0, 0x12, 0x34], got


@cocotb.test(skip=GL)
async def test_lockstep_spi_slave_filler_and_foreign_traffic(dut):
    """No reply queued: the filler goes out. Traffic with CS_n high is
    ignored and MISO stays off the bus. A slower clock."""
    frames = [dict(data=[0xDE, 0xAD], select=False),
              dict(data=[0x42, 0x24]),
              dict(data=[0x99], cs_high_abort=True, bits=5),         # a master that breaks off with SCK high
              dict(data=[0x77], gap=AFTER_ABORT - CS_IDLE)]
    master, played, spi = await bus(dut, [], frames, half=13)
    assert master.errors == [], master.errors[:3]
    assert played[0].miso_on is None
    assert played[1].received == [0xFF, 0xFF] and played[3].received == [0xFF]
    got = await spi.drain_outbox(0)
    assert got == [0x42, 0x24, 0x77], got
