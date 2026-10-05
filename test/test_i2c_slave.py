# SPDX-License-Identifier: Apache-2.0
"""fw/i2c_slave.s on the RTL, in lockstep with the golden model.

Run from test/:
  make SIM_BUILD=sim_build/i2c_slave COCOTB_RESULTS_FILE=sim_build/i2c_slave/results.xml \
       COCOTB_TEST_MODULES=test_i2c_slave

The I2C master model (tools/protomodels_i2c_slave.py) drives SCL and SDA of
both machines at the fastest timing the firmware supports (quarter period 8
cycles: SCL low 16, high 16). The data table is 8 bytes and sits right
behind the program, so that one SPI transaction loads both and each test
stays near 20,000 cycles, two thirds of them the SPI load.
"""

import cocotb

import keyer_isa
from protomodels_i2c_slave import I2cMasterModel
from test import GL, fw, lockstep_firmware

Q = 8                                        # quarter period in core cycles
TMO = {"TMO_PERIOD": 100, "TMO_TICKS": 3}    # bus timeout: 200 to 300 cycles
START, STOP = ("start",), ("stop",)


def image(data, symbols):
    """Program and table as one list of words, and the symbols."""
    sy = dict(symbols, TABLE_BYTES=len(data))
    words, _ = fw("i2c_slave.s", sy)
    sy["TABLE"] = max(a for a, w in enumerate(words) if w) + 1      # the table follows the program
    words, syms = fw("i2c_slave.s", sy)
    base = syms["TABLE"]
    for i, b in enumerate(data):
        words[base + 2 * i] = keyer_isa.encode("LDI", rd=0, imm=b)
        words[base + 2 * i + 1] = keyer_isa.encode("RET")
    return words[:base + 2 * len(data)], syms


class DriveLog:
    """Records the runs of cycles in which the chip pulls a uio pin low, as
    [first cycle, length] (a model for the lockstep harness; it drives
    nothing)."""

    def __init__(self, pin):
        self.pin, self.runs = pin, []

    def on_cycle(self, m):
        if m.uio_oe & (1 << self.pin):
            if self.runs and sum(self.runs[-1]) == m.cycle:
                self.runs[-1][1] += 1
            else:
                self.runs.append([m.cycle, 1])


async def bus(dut, data, script, budget):
    """Load, run thread 0, then let the master play the script in lockstep.
    Returns the master, the SDA drive log, the SPI host and the symbols."""
    words, syms = image(data, TMO)
    master = I2cMasterModel(scl=2, sda=3, quarter=Q)
    sda = DriveLog(3)

    async def go(spi):                       # runs once thread 0 has been started
        master.queue([("wait", 40)] + script)    # the thread listens 18 cycles after its start

    ls, spi = await lockstep_firmware(dut, words, 10, models=[master, sda], after_load=go)
    await ls.run(budget, until=lambda _: master.idle)
    assert master.idle, "the master did not finish in %d cycles" % budget
    await ls.run(40)
    t = ls.m.threads[0]
    # not in a transfer: following the bus from its idle loop, waiting for a START
    assert t.running and t.lr in (syms["listen"], syms["idle"] + 1) and syms["clk"] <= t.pc < syms["listen"], t
    assert int(dut.uio_oe.value) & 0x0C == 0                        # both lines released
    return master, sda, spi, syms


@cocotb.test(skip=GL)
async def test_lockstep_i2c_slave(dut):
    """Pointer write, repeated START, read across the wrap, current-address
    read, another device's address, data bytes handed to the host, a STOP in
    the middle of a byte, then a good read."""
    data = [0x81, 0x00, 0xFF, 0x5A, 0x3C, 0xA7, 0x12, 0xE0]
    addr = 0x50
    W, R = addr << 1, (addr << 1) | 1
    script = [START, ("write", W), ("write", 6), START, ("write", R),
              ("read", True), ("read", True), ("read", False), STOP,
              START, ("write", R), ("read", False), STOP,
              START, ("write", (addr ^ 1) << 1, False), STOP,
              START, ("write", W), ("write", 3), ("write", 0xC3), ("write", 0x5A), STOP,
              START, ("wbits", W, 5), STOP,
              START, ("write", R), ("read", False), STOP]
    master, sda, spi, syms = await bus(dut, data, script, 8000)
    assert master.errors == [], master.errors[:3]
    assert master.read == [data[6], data[7], data[0], data[1], data[5]], master.read
    assert master.acks == [(W, True), (6, True), (R, True), (R, True), ((addr ^ 1) << 1, False),
                           (W, True), (3, True), (0xC3, True), (0x5A, True), (R, True)]
    assert [k for k, _ in master.events] == ["start", "start", "stop"] + ["start", "stop"] * 5
    assert max(n for _, n in master.stretches) > 0                  # the stretch was exercised
    assert max(n + 2 * Q for _, n in master.stretches if n) <= 56   # and ended when the header says
    assert min(n for _, n in master.setup) >= 2                     # SDA stable before SCL rises
    assert min(n for _, n in master.scl_low) == 2 * Q               # the master ran at full speed
    got = await spi.drain_outbox(0)
    assert got == [3, 0xC3, 4, 0x5A], got


@cocotb.test(skip=GL)
async def test_lockstep_i2c_slave_timeouts(dut):
    """A master that dies with SCL low during the slave's ACK, and one that
    dies with SCL high while the slave sends a 0: both times the slave lets
    SDA go after the timeout (200 to 300 cycles here) and answers the next
    START."""
    data = [0x81, 0x00, 0x7F, 0x5A, 0x3C, 0xA7, 0x12, 0xE0]
    W, R = 0xA0, 0xA1
    script = [START, ("wbits", W, 8), ("wait", 400), ("rbits", 1), STOP,
              START, ("write", W), ("write", 2), START, ("write", R),
              ("high", 400), ("rbits", 1), ("rbits", 8), STOP,
              START, ("write", R), ("read", False), STOP]
    master, sda, spi, syms = await bus(dut, data, script, 6000)
    levels = [lv for lv, _ in master.bits]
    assert levels == [1] + [0] + [1] * 8, levels        # no ACK left; one 0 bit; then nothing
    first, held = sda.runs[0]                           # the ACK nobody clocked
    assert first < master.bits[0][1] and 200 <= held <= 320, sda.runs[:2]
    assert [e[0] for e in master.errors] == ["sda changed while scl high"], master.errors
    rise = max(c for c, _ in master.scl_low if c < master.errors[0][1])
    assert 190 <= master.errors[0][1] - rise <= 310, master.errors[0][1] - rise
    assert master.read == [data[3]], master.read
    assert await spi.drain_outbox(0) == []
