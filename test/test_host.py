# SPDX-License-Identifier: Apache-2.0
"""The host driver (tools/keyerhost.py) on the simulation transport.

The self-test bodies in keyerhost.py take only a KeyerHost, so the same
functions run on the demo board. Here they run against the RTL (and, under
GATES=yes, the gate-level netlist) through the SPI pads, once on their own
and once under the lockstep harness, which checks the model against the RTL
during all of the driver's traffic.
"""

import cocotb

import keyerhost as kh
from keyer_tb import Lockstep
import protomodels as pm
from test import GL, EdgeLog, PadView, fw, fw_merge, run_pads, start


@cocotb.test()
async def test_host_selftests(dut):
    """ID, 256-word program read-back, PCs, FIFO echo with level checks,
    capture and replay on the uo pins: the driver's self-tests, pads only."""
    pads = await start(dut)
    host = kh.KeyerHost(kh.SimTransport(dut, pads))
    assert await kh.selftest_all(host) == 2


@cocotb.test()
async def test_host_uart_loopback(dut):
    """Load the UART firmware with the driver, wire TX to RX outside the
    chip, and move more bytes than a FIFO holds through push() and pop()."""
    P = 40
    words, syms = fw("uart.s", {"BAUD_DIV": P})
    pads = await start(dut)
    host = kh.KeyerHost(kh.SimTransport(dut, pads))
    view = PadView(dut)
    data = [0x30 + i for i in range(24)]
    result = {}

    class Wire:                                   # uo2 (TX) feeds ui3 (RX)
        def on_cycle(self, m):
            tx = (m.pad() >> 18) & 1
            m.ext_ui = (m.ext_ui & ~0x08) | (tx << 3)

    async def body():
        await host.load_program(words)
        await host.set_pc(1, syms["rx_init"])
        await host.run(0b11)
        got = []
        for i in range(0, len(data), 8):
            await host.push(0, data[i:i + 8])
            got += await host.pop(1, 8, wait=True)
        result["got"] = got
        result["status"] = await host.status()
        await host.stop()

    task = cocotb.start_soon(body())
    await run_pads(dut, pads, view, [Wire()], until=task.done)
    await task
    assert result["got"] == data, result["got"]
    assert result["status"]["running"] == [1, 1]


@cocotb.test(skip=GL)
async def test_lockstep_host_selftests(dut):
    """The same self-tests with the model in lockstep: every register write,
    FIFO transfer, memory access and capture/replay action the driver makes
    is mirrored into the model and the two must agree every cycle."""
    pads = await start(dut)
    ls = Lockstep(dut, pads)
    host = kh.KeyerHost(kh.SimTransport(dut, pads))
    task = cocotb.start_soon(kh.selftest_all(host))
    while not task.done():
        await ls.run(500)
    assert await task == 2
    await ls.run(50)
    assert ls.retired > 0


@cocotb.test()
async def test_host_capture_replay_demo(dut):
    """The datasheet's capture example, step for step, through the driver:
    thread 1 (fw/capture_demo.s) records thread 0's I2C transaction and
    replays it; the host waits for the replay, stops the threads and drains
    the buffer. The slave model must see the transaction twice with the
    same edge timing."""
    Q = 30
    image, syms = fw_merge(("i2c_master.s", {"I2C_Q": Q}), ("capture_demo.s", None))
    pads = await start(dut)
    host = kh.KeyerHost(kh.SimTransport(dut, pads))
    view = PadView(dut)
    slave = pm.I2cSlaveModel(scl=2, sda=3, address=0x50, stretch=0, min_high=Q, min_low=Q)
    edges = EdgeLog(0x0C)
    result = {}

    async def body():
        await host.load_program(image)
        await host.pinmode(0x0C)
        await host.capture_config(group=0, mask=0xC, trigger_pattern=0x4, trigger_mask=0xC, base=0xA4, length=92)
        await host.replay_config(group=0, mask=0xC, base=0xA4, length=0)
        await host.set_pc(1, syms["cap_demo"])
        await host.push(0, [0x01, 0x03, 2, 0xA0, 0x10, 0x02, 0x05])
        await host.run(0b10)
        await host.wait_replay_done()
        await host.stop()
        result["entries"] = await host.capture_drain(0xA4)
        result["status"] = await host.cr_status()
        result["counts"] = await host.cr_counts()

    task = cocotb.start_soon(body())
    await run_pads(dut, pads, view, [slave, edges], until=task.done)
    await task
    assert slave.errors == [], slave.errors[:3]
    assert [e[0] for e in slave.events] == ["start", "stop", "start", "stop"]
    starts = [c for k, c in slave.events if k == "start"]
    stops = [c for k, c in slave.events if k == "stop"]
    first = edges.deltas(starts[0] - 1, stops[0] + 5)
    assert first == edges.deltas(starts[1] - 1, stops[1] + 5)
    st, (n, k), entries = result["status"], result["counts"], result["entries"]
    assert st["capture_done"] and st["replay_done"] and not st["overflow"] and not st["underrun"], st
    assert n == k == len(entries) and n >= 10
    assert [d for d, _ in entries][1:] == first[:n - 1]
