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
    assert await kh.selftest_all(host) == 3


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
    assert await task == 3
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


@cocotb.test()
async def test_host_reset_through_the_driver(dut):
    """No transaction is in progress when reset is released (SEMANTICS 10.1,
    DECISIONS D-038): the driver's reset() holds CS_n high from before rst_n
    falls until after it rises, the chip answers at once afterwards, a
    reset from inside a transaction is refused, and no transaction starts
    while rst_n is low."""
    from cocotb.triggers import ClockCycles, RisingEdge
    pads = await start(dut)
    t = kh.SimTransport(dut, pads)
    host = kh.KeyerHost(t)
    await host.write(kh.R_PINMODE, [0xA5])
    await host.write(kh.R_IRQEN, [0xFF])
    csn_low_in_reset = []

    async def watch():
        prev = 1
        while True:
            await RisingEdge(dut.clk)
            rst = int(dut.rst_n.value)
            csn = (int(dut.ui_in.value) >> 2) & 1
            if (rst == 0 or prev == 0) and csn == 0:
                csn_low_in_reset.append(1)
            prev = rst

    cocotb.start_soon(watch())
    await host.reset()
    assert await host.id() == (kh.ID_BYTE, 3)
    assert await host.read(kh.R_PINMODE, 1) == [0] and await host.read(kh.R_IRQEN, 1) == [0]

    async def reset_inside():
        await ClockCycles(dut.clk, 20)              # the read below is under way
        assert t.spi.busy
        try:
            await host.reset()
        except kh.KeyerError:
            return True
        return False

    task = cocotb.start_soon(reset_inside())
    assert await host.read(kh.R_ID, 4) == [kh.ID_BYTE, 3, kh.ID_BYTE, 3]
    assert await task is True and int(dut.rst_n.value) == 1

    dut.rst_n.value = 0                             # a reset the driver did not make
    await ClockCycles(dut.clk, 3)
    try:
        await host.id()
        raised = False
    except kh.KeyerError:
        raised = True
    assert raised and (int(dut.ui_in.value) >> 2) & 1 == 1
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 4)
    assert await host.id() == (kh.ID_BYTE, 3)
    assert not csn_low_in_reset



@cocotb.test()
async def test_host_capture_decode_i2c(dut):
    """The datasheet's worked example of reading a capture (docs/info.md,
    docs/CAPTURE.md section 5), through the command layer the board and the
    PC use: load fw/i2c_master.s, arm a START-triggered capture of SCL/SDA
    into the words above the firmware, run thread 0, push a write of the
    pointer 0x10 to the EEPROM at 0x50, a repeated START and a one-byte
    read, then `capture read`, `capture listing` and `capture decode i2c`.
    The decoded transfers must be exactly what the firmware was told to
    send, and the byte read the one it pushed to the outbox. Pads only.

    One byte, not two: this transaction is 102 entries and 117 words are
    free above the 139-word firmware, but a two-byte read needs 116 to 128
    entries depending on the data (124 for 5A C3). The slave model's SDA
    changes reach the pads one cycle after the SCL fall they answer (the
    registered wire of run_pads), so each is an entry of its own. The ACK
    of all but the last read byte is covered on a synthetic waveform in
    tools/test_keyerhost.py."""
    import keyerasm
    from test import FW
    import os
    Q = 30
    with open(os.path.join(FW, "i2c_master.s")) as f:
        prog, _, _ = keyerasm.assemble(f.read(), symbols={"I2C_Q": Q})
    end = max(prog) + 1                          # the buffer starts above the firmware
    image = keyerasm.to_list(prog, size=end)     # what `load fw/i2c_master.s -D I2C_Q=30` sends
    length = 256 - end
    pads = await start(dut)
    host = kh.KeyerHost(kh.SimTransport(dut, pads))
    view = PadView(dut)
    slave = pm.I2cSlaveModel(scl=2, sda=3, address=0x50, stretch=0, min_high=Q, min_low=Q)
    slave.mem[0x10:0x12] = [0x5A, 0xC3]
    edges = EdgeLog(0x0C)
    # START, WRITE [A0 10] (address 0x50 W, pointer 0x10), START (repeated),
    # WRITE [A1] (address 0x50 R), READ 1, STOP, WRITE of no bytes: the
    # last only pushes a status byte, so the host knows the STOP is done
    script = [0x01, 0x03, 2, 0xA0, 0x10, 0x01, 0x03, 1, 0xA1, 0x04, 1, 0x02, 0x03, 0]
    out = {}

    async def cmd(*argv):
        text = await kh.command(host, list(argv))
        out[argv[0] if argv[0] != "capture" else " ".join(argv[:2])] = text
        dut._log.info("> keyerhost %s\n%s", " ".join(argv), text)
        return text

    async def body():
        await cmd("load", *["%04X" % w for w in image])
        await cmd("capture", "0", "C", "4", "C", "%X" % end, "%X" % length)
        await cmd("run", "1")
        await cmd("push", "0", *["%02X" % b for b in script])
        out["outbox"] = await host.pop(0, 4, wait=True)
        await cmd("capture", "disarm")
        await host.wait_capture_done()
        await cmd("stop")
        await cmd("capture", "status")
        out["entries"] = await host.capture_read()
        await cmd("capture", "read")
        await cmd("capture", "listing", "--clock", "50000000", "--names", "2=SCL,3=SDA")
        await cmd("capture", "decode", "i2c", "--clock", "50000000")

    task = cocotb.start_soon(body())
    await run_pads(dut, pads, view, [slave, edges], until=task.done)
    await task
    entries = out["entries"]
    assert slave.errors == [] and [e[0] for e in slave.events] == ["start", "start", "nack", "stop"]
    assert "OVERFLOW" not in out["capture status"] and "done" in out["capture status"]
    assert 20 < len(entries) < length                          # ended by the DISARM, not by the length
    res, text = kh.decode_capture(entries, 0, "i2c")
    assert res["errors"] == [], res["errors"]
    assert res["transfers"] == [("start", 0x50, 0, 0, [(0x10, 0)], "restart"),
                                ("restart", 0x50, 1, 0, [(0x5A, 1)], "stop")], res["transfers"]
    assert out["outbox"] == [0, 0, 0x5A, 0]                     # write statuses, the byte read, the fence
    assert out["outbox"][2:3] == [b for b, _ in res["transfers"][1][4]]
    # the entries are the bus's edges from the START on
    first = [c for k, c in slave.events if k == "start"][0]
    stop = [c for k, c in slave.events if k == "stop"][0]
    assert [d for d, _ in entries][1:] == edges.deltas(first - 1, stop + 1), entries[:8]
    # the command layer: read back, listing, decode
    assert kh.parse_capture(out["capture read"]) == (entries, 0, 0xC)
    head = out["capture listing"].splitlines()[0]
    assert head.startswith("capture: group 0 (pins 0-3), %d entries, %d cycles = "
                           % (len(entries), sum(d for d, _ in entries))), head
    assert out["capture decode"].splitlines()[0] == "i2c (scl bit 2 = pin 2, sda bit 3 = pin 3): 2 transfers, 0 errors"
    assert "0x50 W ACK : 10 ACK" in out["capture decode"] and "0x50 R ACK : 5A NACK" in out["capture decode"]


@cocotb.test()
async def test_host_capture_decode_uart(dut):
    """fw/uart.s's transmitter on uo2 (pin 18, bit 2 of group 4), recorded
    from its first start bit by a capture of group 4 and decoded with the
    UART decoder at the firmware's BAUD_DIV: the bytes pushed come back.
    Thread 1 stays stopped (the capture writes in its slots). Pads only."""
    P = 40
    words, syms = fw("uart.s", {"BAUD_DIV": P})
    data = [0x55, 0xA3, 0x00, 0xFF, 0x31, 0x4B, 0x0F, 0xF0]
    pads = await start(dut)
    host = kh.KeyerHost(kh.SimTransport(dut, pads))
    await host.load_program(words)
    await host.run(0b01)                                         # TX idles high once running
    await host.capture_config(group=4, mask=0x4, trigger_pattern=0x0, trigger_mask=0x4, base=0x40, length=0xC0)
    await host.capture_arm()
    await host.push(0, data)
    while (await host.levels())[0]:
        await host.t.idle(P)
    await host.t.idle(12 * P)                                   # the last character's bits
    await host.capture_disarm()
    await host.wait_capture_done()
    await host.stop()
    st = await host.cr_status()
    assert not st["overflow"], st
    entries = await host.capture_read()
    assert entries[0] == (0, 0x0) and len(entries) < 0xC0
    res, text = kh.decode_capture(entries, 4, "uart", period=P)
    dut._log.info("%s", text)
    assert res["bytes"] == data and res["errors"] == [], (res["bytes"], res["errors"])
    gaps = [b - a for a, b in zip(res["starts"], res["starts"][1:])]
    assert len(set(gaps)) == 1 and gaps[0] >= 10 * P, gaps         # back to back, one firmware loop apart
    assert await kh.command(host, ["capture", "decode", "uart", "--period", str(P)]) == text
