# SPDX-License-Identifier: Apache-2.0
"""The serializer (SEMANTICS 15) on the RTL, in lockstep with the golden
model: every register of the engine is compared every cycle
(keyer_tb.SER_STATE). The scenarios (firmware, line stimulus, protocol
checks) are tools/ser_scenarios.py, the ones tools/test_ser_scenarios.py
runs on the model alone.

Run from test/:
  make SIM_BUILD=sim_build/ser COCOTB_RESULTS_FILE=sim_build/ser/results.xml COCOTB_TEST_MODULES=test_ser
"""

import os

import cocotb
from cocotb.triggers import ClockCycles

import ser_scenarios as S
from test import GL, lockstep_firmware


async def _run(dut, words, models, cycles, **kw):
    """Lockstep run with the host draining thread 0's outbox the whole time."""
    got, stop = [], []

    async def drain(spi):
        while not stop:
            got.extend(await spi.drain_outbox(0))
            await ClockCycles(dut.clk, 40)

    ls, spi = await lockstep_firmware(dut, words, 0, models=models, **kw)
    task = cocotb.start_soon(drain(spi))
    await ls.run(cycles)
    stop.append(1)
    while not task.done():
        await ls.run(100)
    got.extend(await _final(ls, spi))
    return ls, got


async def _final(ls, spi):
    task = cocotb.start_soon(spi.drain_outbox(0))
    while not task.done():
        await ls.run(100)
    return task.result()


@cocotb.test(skip=GL)
async def test_lockstep_ser_nrzi_tx(dut):
    """Two USB-shaped frames at 4 cycles per bit: stuffing, CRC-16, SE0 SE0 J,
    release; decoded from the pads by an independent NRZI decoder."""
    words, models, cycles, check = S.nrzi_tx(T=4)
    ls, got = await _run(dut, words, models, cycles)
    check(ls.m, got)
    dut._log.info("ser nrzi tx: %d cycles in lockstep" % ls.cycle)


@cocotb.test(skip=GL)
async def test_lockstep_ser_nrzi_tx_usb_rate(dut):
    """The same at the low-speed USB setting, 32 cycles per bit (48 MHz)."""
    words, models, cycles, check = S.nrzi_tx(T=32, k=0)
    ls, got = await _run(dut, words, models, cycles)
    check(ls.m, got)


@cocotb.test(skip=GL)
async def test_lockstep_ser_manchester_tx(dut):
    """A 60-byte Ethernet-shaped frame at 2 cycles per half-bit (10BASE-T at
    40 MHz): preamble, SFD, CRC-32 equal to binascii.crc32, six-period tail."""
    words, models, cycles, check = S.manchester_tx(T=2)
    ls, got = await _run(dut, words, models, cycles)
    check(ls.m, got)


@cocotb.test(skip=GL)
async def test_lockstep_ser_manchester_tx_one_cycle_halves(dut):
    """The same at one cycle per half-bit (10BASE-T at 20 MHz): every cycle is a tick."""
    words, models, cycles, check = S.manchester_tx(T=1, k=2)
    ls, got = await _run(dut, words, models, cycles)
    check(ls.m, got)


@cocotb.test(skip=GL)
async def test_lockstep_ser_nrzi_rx(dut):
    """Six packets from a line driver: a token (CRC-5 good), a data packet
    with stuffed bits (CRC-16 good), a bad CRC, seven ones in a row, a frame
    that ends off a byte boundary, and a good token again."""
    words, models, cycles, check, _ = S.nrzi_rx(T=8, gap=250)
    ls, got = await _run(dut, words, models, cycles)
    check(ls.m, got)


@cocotb.test(skip=GL)
async def test_lockstep_ser_manchester_rx(dut):
    """Three Manchester frames from a line driver (good, corrupted, good):
    SFD detection, CRC-32 verdicts, the idle timeout as the frame end."""
    words, models, cycles, check, _ = S.manchester_rx(T=4)
    ls, got = await _run(dut, words, models, cycles)
    check(ls.m, got)


async def _random(dut, seed):
    words, base, models = S.random_scenario(seed)
    ls, _ = await lockstep_firmware(dut, words, 9000, models=models, run_mask=0b11, pc1=base)
    assert ls.retired > 500
    dut._log.info("ser random seed %d: %d cycles, %d instructions" % (seed, ls.cycle, ls.retired))


@cocotb.test(skip=GL)
async def test_lockstep_ser_random_tx_heavy(dut):
    """Both threads loop over random serializer, timer and pin instructions
    while random noise and coded frames arrive on the pins."""
    await _random(dut, int(os.environ.get("KEYER_SEED", "6")))


@cocotb.test(skip=GL)
async def test_lockstep_ser_random_rx_heavy(dut):
    await _random(dut, int(os.environ.get("KEYER_SEED", "1")))


@cocotb.test(skip=GL)
async def test_lockstep_ser_random_mixed(dut):
    await _random(dut, int(os.environ.get("KEYER_SEED", "2")))
