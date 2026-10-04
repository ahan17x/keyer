# SPDX-License-Identifier: Apache-2.0
"""Keyer cocotb tests. Run `make` in this directory (DUMP=1 for a waveform).

Two kinds of test:
  1. Host interface: registers, program memory and FIFOs over the SPI slave.
  2. Lockstep: the Python ISS runs cycle by cycle against the RTL from reset,
     with real firmware and the protocol models from tools/protomodels.py.
"""

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

import keyerasm
import protomodels as pm
from keyer_tb import (Lockstep, Pads, SpiMaster, reset, R_CTRL, R_ID, R_IMEM_ADDR,
                     R_IMEM_DATA, R_INBOX0, R_LEVELS, R_OUTBOX0, R_PINMODE, R_PINS,
                     R_STAT, R_PINOUT, R_PC0, R_PC1, R_IRQEN, R_CR_CTRL, R_CAP_CFG,
                     R_CAP_BUF, R_REP_CFG, R_REP_BUF, R_CR_COUNT)

FW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fw")

# Gate-level run (GATES=yes make, the gl_test job): the netlist is flat, so
# the lockstep tests, which compare RTL internals with the model every cycle,
# are skipped; the host-interface tests and the pads-only firmware tests
# below run in both modes.
GL = os.environ.get("GATES") == "yes"


def fw(name, symbols=None):
    with open(os.path.join(FW, name)) as f:
        words, syms, _ = keyerasm.assemble(f.read(), symbols=symbols)
    return keyerasm.to_list(words), syms


def asm(src, symbols=None):
    words, syms, _ = keyerasm.assemble(src, symbols=symbols)
    return keyerasm.to_list(words), syms


def fw_merge(*parts):
    """Assemble several firmware files into one 256-word image: parts are
    (name, symbols); each file places itself with .org."""
    image, allsyms = {}, {}
    for name, symbols in parts:
        with open(os.path.join(FW, name)) as f:
            words, syms, _ = keyerasm.assemble(f.read(), symbols=symbols)
        assert not (set(words) & set(image)), "firmware images overlap"
        image.update(words)
        allsyms.update(syms)
    return keyerasm.to_list(image), allsyms


class EdgeLog:
    """Records (cycle, value) whenever the masked pad bits change (a model
    for the lockstep harness; it drives nothing)."""

    def __init__(self, mask):
        self.mask, self.log, self._prev = mask, [], None

    def on_cycle(self, m):
        v = m.pad() & self.mask
        if v != self._prev:
            self.log.append((m.cycle, v))
            self._prev = v

    def deltas(self, start, end):
        pts = [c for c, _ in self.log if start <= c < end]
        return [b - a for a, b in zip(pts, pts[1:])]


async def start(dut, period_ns=20):
    cocotb.start_soon(Clock(dut.clk, period_ns, unit="ns").start())
    pads = Pads(dut)
    await reset(dut, pads)
    return pads


# ---------------------------------------------------------------- host interface

@cocotb.test()
async def test_id_and_registers(dut):
    pads = await start(dut)
    for half in (4, 8, 16):                 # SCK = clk/8 (the limit), clk/16, clk/32
        spi = SpiMaster(dut, pads, half=half)
        assert await spi.read(R_ID, 2) == [0x4B, 0x02], half
        assert await spi.read(R_STAT, 1) == [0]
        await spi.write(R_PINMODE, [0xA5])
        assert await spi.read(R_PINMODE, 1) == [0xA5]
        await spi.write(R_PINMODE, [0x00])
        assert await spi.read(R_LEVELS, 4) == [0, 0, 0, 0]
        pads.set_fw_inputs(0xF8, 0x3C)
        await ClockCycles(dut.clk, 4)
        assert await spi.read(R_PINS, 3) == [0x3C, 0xF8, 0x00], half   # uio pads, ui (CS_n is low during the read), uo


@cocotb.test()
async def test_pc_readback_and_soft_reset(dut):
    """SEMANTICS 3.2 and 10.4: PC0/PC1 and IMEM_ADDR read back; CTRL RSTn
    empties the thread's FIFOs and leaves its PC alone (BUGS 12, 13)."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    words, syms = asm("""
        ldi  r0, 1
        push r0
        halt
    """)
    await spi.load_program(words)
    await spi.write(R_IMEM_ADDR, [0x20, 0])
    assert await spi.read(R_IMEM_ADDR, 2) == [0x20, 0]
    await spi.set_pc(1, 0x40)
    assert await spi.read(R_PC1, 2) == [0x40, 0]
    await spi.run(0b01)
    await ClockCycles(dut.clk, 40)
    assert await spi.read(R_STAT, 1) == [0b000100]               # T0 halted
    assert await spi.read(R_PC0, 2) == [3, 0]                    # PC after HALT
    await spi.write(R_INBOX0, [1, 2, 3])
    assert await spi.read(R_LEVELS, 4) == [3, 1, 0, 0]
    await spi.write(R_CTRL, [0b0100])                            # RST0, RUN bits 0
    assert await spi.read(R_LEVELS, 4) == [0, 0, 0, 0]
    assert await spi.read(R_PC0, 2) == [3, 0]                    # PC unchanged by RSTn
    assert await spi.read(R_STAT, 1) == [0b000100]               # still halted, not running


@cocotb.test()
async def test_capture_registers(dut):
    """Capture/replay registers read back; status and counts are 0 after
    reset; IRQEN carries 8 bits (docs/CAPTURE.md section 2)."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    assert await spi.cr_status() == 0
    assert await spi.cr_count() == [0, 0]
    await spi.cap_config(group=5, mask=0xA, tpat=0x3, tmask=0x7, base=0xC0, length=40)
    assert await spi.read(R_CAP_CFG, 2) == [0xA5, 0x73]
    assert await spi.read(R_CAP_BUF, 2) == [0xC0, 40]
    await spi.rep_config(group=1, mask=0xF, base=0x80, length=0)
    assert await spi.read(R_REP_CFG, 1) == [0xF1]
    assert await spi.read(R_REP_BUF, 2) == [0x80, 0]
    await spi.write(R_IRQEN, [0xC0])
    assert await spi.read(R_IRQEN, 1) == [0xC0]
    await spi.cr_ctrl(0b0100)                       # START a replay of length 0: done at once
    await ClockCycles(dut.clk, 4)
    assert await spi.cr_status() == 0b0010_0000     # replay done, nothing active
    assert int(dut.uo_out.value) & 2                # IRQ: replay done enabled
    await spi.write(R_IRQEN, [0])


@cocotb.test()
async def test_imem_write_read(dut):
    pads = await start(dut)
    for half in (4, 16):
        spi = SpiMaster(dut, pads, half=half)
        words = [random.randrange(0x10000) for _ in range(12)]
        await spi.load_program(words, base=0x20)
        await spi.write(R_IMEM_ADDR, [0x20, 0])
        rb = await spi.read(R_IMEM_DATA, 2 * len(words))
        got = [rb[2 * i] | (rb[2 * i + 1] << 8) for i in range(len(words))]
        assert got == words, (half, ["%04X" % w for w in got])
        # the memory itself (RTL with the behavioural array only)
        if not GL and hasattr(dut.user_project.u_imem, "mem"):
            for i, w in enumerate(words):
                assert int(dut.user_project.u_imem.mem[0x20 + i].value) == w
    # a full 256-word image in one transaction, read back in one transaction
    # (BUGS 14: the byte counter must not stop the low/high alternation)
    spi = SpiMaster(dut, pads, half=4)
    words = [random.randrange(0x10000) for _ in range(256)]
    await spi.load_program(words, base=0)
    if not GL and hasattr(dut.user_project.u_imem, "mem"):
        for a in (0x7E, 0x7F, 0x80, 0xFF):
            assert int(dut.user_project.u_imem.mem[a].value) == words[a], ("word", a)
    await spi.write(R_IMEM_ADDR, [0, 0])
    rb = await spi.read(R_IMEM_DATA, 512)
    got = [rb[2 * i] | (rb[2 * i + 1] << 8) for i in range(256)]
    assert got == words, [i for i in range(256) if got[i] != words[i]][:8]


@cocotb.test()
async def test_fifo_roundtrip_and_status(dut):
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    words, _ = asm("""
    loop:   pop  r0
            inc  r0
            push r0
            bra  loop
    """)
    await spi.load_program(words)
    await spi.run(0b01)
    data = [1, 2, 3, 250, 255, 0]
    await spi.write(R_INBOX0, data)
    await ClockCycles(dut.clk, 60)
    assert await spi.read(R_LEVELS, 4) == [0, len(data), 0, 0]
    assert await spi.read(R_STAT, 1) == [0b010001]       # T0 running and blocked on POP
    got = await spi.read(R_OUTBOX0, len(data))
    assert got == [(b + 1) & 0xFF for b in data], got
    assert await spi.read(R_LEVELS, 4) == [0, 0, 0, 0]
    await spi.run(0)
    assert await spi.read(R_STAT, 1) == [0]


# ---------------------------------------------------------------- pads only

class PadView:
    """What the protocol models need from a machine, taken from the DUT's
    pads alone (works on the gate-level netlist): pad(), cycle, ext_ui,
    ext_uio. The models' outputs reach the pads one cycle after the levels
    they react to (a registered wire), which the protocols do not notice."""

    def __init__(self, dut):
        self.dut, self.cycle, self.ext_ui, self.ext_uio, self._pad = dut, 0, 0, 0xFF, 0

    def pad(self):
        return self._pad

    @staticmethod
    def _bits(sig):
        # X/Z read as 0: MISO (uo[0]) is briefly unknown in simulation after a
        # read that empties a FIFO whose memory was never written; the models
        # never look at uo[1:0] anyway.
        return int("".join(c if c in "01" else "0" for c in str(sig.value)), 2)

    def sample(self):
        oe, out, uo = self._bits(self.dut.uio_oe), self._bits(self.dut.uio_out), self._bits(self.dut.uo_out)
        uio = (oe & out) | (~oe & self.ext_uio & 0xFF)
        self._pad = uio | ((self.ext_ui & 0xFF) << 8) | ((uo & 0xFC) << 16)


async def run_pads(dut, pads, view, models, cycles=0, until=None):
    n = 0
    while (until is None and n < cycles) or (until is not None and not until()):
        await ReadOnly()
        view.sample()
        for mod in models:
            mod.on_cycle(view)
        await RisingEdge(dut.clk)
        pads.set_fw_inputs(view.ext_ui, view.ext_uio)
        view.cycle += 1
        n += 1
        assert n < 400000, "pads run did not finish"


@cocotb.test()
async def test_pads_uart_loopback(dut):
    """Full-duplex UART through the pads only: thread 0 transmits the inbox
    on uo2, an external wire feeds ui3, thread 1 receives into its outbox.
    Runs on RTL and on the gate-level netlist."""
    P = 40
    words, syms = fw("uart.s", {"BAUD_DIV": P})
    data = [0x55, 0xA3, 0x00, 0xFF, 0x31]
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    view = PadView(dut)

    class Wire:
        def on_cycle(self, m):
            tx = (m.pad() >> 18) & 1
            m.ext_ui = (m.ext_ui & ~0x08) | (tx << 3)

    async def host():
        await spi.load_program(words)
        await spi.set_pc(1, syms["rx_init"])
        await spi.write(R_INBOX0, data)
        await spi.run(0b11)

    task = cocotb.start_soon(host())
    await run_pads(dut, pads, view, [Wire()], until=task.done)
    await run_pads(dut, pads, view, [Wire()], cycles=P * 14 * len(data) + 1500)
    got = await spi.drain_outbox(1)
    assert got == data, got
    assert (await spi.levels())[0] == 0


@cocotb.test()
async def test_pads_capture_replay_demo(dut):
    """The capture-and-replay demo through the pads only (RTL and gate
    level): fw/capture_demo.s records thread 0's I2C transaction and replays
    it; the slave sees it twice with identical edge timing and the entries
    drained over SPI carry the recorded deltas."""
    Q = 30
    words, syms = fw_merge(("i2c_master.s", {"I2C_Q": Q}), ("capture_demo.s", None))
    base, length = 0xA4, 92
    cmds = [0x01, 0x03, 2, 0xA0, 0x10, 0x02, 0x05]
    slave = pm.I2cSlaveModel(scl=2, sda=3, address=0x50, stretch=0, min_high=Q, min_low=Q)
    edges = EdgeLog(0x0C)
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    view = PadView(dut)

    async def host():
        await spi.load_program(words)
        await spi.write(R_PINMODE, [0x0C])
        await spi.cap_config(group=0, mask=0xC, tpat=0x4, tmask=0xC, base=base, length=length)
        await spi.rep_config(group=0, mask=0xC, base=base, length=0)
        await spi.set_pc(1, syms["cap_demo"])
        await spi.write(R_INBOX0, cmds)
        await spi.run(0b10)

    task = cocotb.start_soon(host())
    await run_pads(dut, pads, view, [slave, edges], until=task.done)
    await run_pads(dut, pads, view, [slave, edges], cycles=16000)
    assert slave.errors == [], slave.errors[:3]
    kinds = [e[0] for e in slave.events]
    assert kinds == ["start", "stop", "start", "stop"], kinds
    assert slave.ptr == 0x10 and len(slave.transfers) == 2
    starts = [c for k, c in slave.events if k == "start"]
    stops = [c for k, c in slave.events if k == "stop"]
    first = edges.deltas(starts[0] - 1, stops[0] + 5)
    second = edges.deltas(starts[1] - 1, stops[1] + 5)
    assert first == second, (first[:10], second[:10])
    st = await spi.cr_status()
    assert st & 0b0100 and st & 0b10_0000 and not (st & 0b1000) and not (st & 0b100_0000), bin(st)
    n, k = await spi.cr_count()
    assert n == k and n >= 10, (n, k)
    assert await spi.read(R_STAT, 1) == [0b001000]                # T1 halted, T0 stopped
    entries = await spi.read_entries(base, n)
    deltas = [e >> 4 for e in entries]
    assert deltas[0] == 0 and deltas[1:] == first[:n - 1], (deltas[:12], first[:12])


# ---------------------------------------------------------------- lockstep

async def lockstep_firmware(dut, words, cycles, models=(), run_mask=0b01, pc1=0,
                            inbox0=(), inbox1=(), half=4, after_load=None, run_after=False):
    """Load firmware over SPI with the ISS mirroring every host action, then
    run both machines in lockstep for `cycles` cycles."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=half)
    ls = Lockstep(dut, pads, models=models)

    async def host():
        await spi.load_program(words)
        if pc1:
            await spi.set_pc(1, pc1)
        for b in inbox0:
            await spi.write(R_INBOX0, [b])
        for b in inbox1:
            await spi.write(R_INBOX0 + 2, [b])
        if after_load and run_after:
            await after_load(spi)
        await spi.run(run_mask)
        if after_load and not run_after:
            await after_load(spi)

    task = cocotb.start_soon(host())
    while not task.done():                  # lockstep covers the host's SPI traffic too
        await ls.run(500)
    await ls.run(cycles)
    assert ls.retired > 0
    return ls, spi


@cocotb.test(skip=GL)
async def test_lockstep_alu_and_branches(dut):
    words, _ = asm("""
        ldw  r0, 0x1234
        ldw  r1, 0xFFFF
        add  r0, r1
        adc  r1, r0
        sub  r1, r0
        sbc  r0, r1
        and  r2, r0
        or   r2, r1
        xor  r3, r2
        mov  r4, r3
        cmp  r4, r2
        tst  r4, r1
        shl  r0
        shr  r1
        rcl  r2
        rcr  r3
        not  r4
        neg  r5
        inc  r6
        dec  r7
        swap r0
        rev8 r1
        addi r2, -5
        andi r3, 0x0F
        ori  r4, 0xF0
        xori r5, 0xAA
        cmpi r6, 1
        ldi  r7, 9
    loop: djnz r7, loop
        ldi  r6, 3
    l2:   dec  r6
        bne  l2
        beq  l3
        nop
    l3: bcs  l4
        bcc  l4
    l4: call sub
        rdcyc r0
        rds  r1
        rdlr r2
        halt
    sub: rdlr r6
         jmpr r6
    """)
    ls, _ = await lockstep_firmware(dut, words, 4000)
    assert ls.retired >= 45 and not ls.m.threads[0].running


@cocotb.test(skip=GL)
async def test_lockstep_pins_timer_delay(dut):
    words, syms = asm("""
        ldi  r1, 7
        sett r1
        od   uio0
        set  uio0
        clr  uio0
        oen  uio1
        set  uio1
        outb r1
        ldi  r2, 0xF0
        outoe r2
        waitd 1
        set  uo2
        waitd 1
        clr  uo2
        delay 5
        wt1  ui3
        wtf  ui3
        wtr  ui3
        inb  r3
        inw  r4
        inr  r5, ui4
        rdc  ui4
        wrc  uo3
        tstp ui4
        bp1  ui4, skip
        set  uo4
    skip: setc
        clc
        setd 0
        rdt  r6
        setd 2
        wt0t ui4
        popt r7
        bdr  skip2
        nop
    skip2: rds r6
        waitd 2
        pusht r6
        start
        halt
    t1: ldi r0, 20
        sett r0
        waitd 1
        set uo5
        setd 3
        wtrt ui3
        waitd 1
        wtft ui3
        halt
    """)

    class Wiggle:
        def on_cycle(self, m):
            c = m.cycle
            v = 1 if (c // 50) % 2 == 1 else 0        # ui3 toggles every 50 cycles
            w = 1 if (c // 33) % 2 == 1 else 0        # ui4 every 33
            m.ext_ui = (m.ext_ui & 0xE7) | (v << 3) | (w << 4)

    ls, _ = await lockstep_firmware(dut, words, 3000, models=[Wiggle()], pc1=syms["t1"])
    assert all(t.halted for t in ls.m.threads), [t.pc for t in ls.m.threads]


@cocotb.test(skip=GL)
async def test_lockstep_uart_loopback(dut):
    P = 40
    words, syms = fw("uart.s", {"BAUD_DIV": P})
    data = [0x55, 0xA3, 0x00, 0xFF, 0x31]

    class Wire:                       # TX (uo2) feeds RX (ui3) externally
        def on_cycle(self, m):
            tx = (m.pad() >> 18) & 1
            m.ext_ui = (m.ext_ui & ~0x08) | (tx << 3)

    ls, spi = await lockstep_firmware(dut, words, P * 14 * len(data) + 3000,
                                      models=[Wire()], run_mask=0b11,
                                      pc1=syms["rx_init"], inbox0=data)
    got = await spi.drain_outbox(1)
    assert got == data, got


@cocotb.test(skip=GL)
async def test_lockstep_spi_master(dut):
    words, _ = fw("spi_master.s", {"SPI_HALF": 12})
    frames = [[0x9F], [0x03, 0x00, 0x10, 0xAA]]
    stream = []
    for f in frames:
        stream += [len(f)] + f
    slave = pm.SpiSlaveModel(sck=19, mosi=20, csn=21, miso_ui_bit=4,
                             reply=[0xEF, 0x40, 0x18, 0x11, 0x22])
    ls, spi = await lockstep_firmware(dut, words, 6000, models=[slave], inbox0=stream)
    assert slave.frames == frames and slave.errors == []
    got = await spi.drain_outbox(0)
    assert got == [0xEF, 0x40, 0x18, 0x11, 0x22], got


@cocotb.test(skip=GL)
async def test_lockstep_i2c_master(dut):
    Q = 30
    words, _ = fw("i2c_master.s", {"I2C_Q": Q})
    cmds = ([0x01, 0x03, 3, 0xA0, 0x10, 0x5C, 0x02]        # write 0x5C at 0x10
            + [0x01, 0x03, 2, 0xA0, 0x10]                 # set pointer
            + [0x01, 0x03, 1, 0xA1, 0x04, 1, 0x02])       # repeated start, read 1
    slave = pm.I2cSlaveModel(scl=2, sda=3, address=0x50, stretch=200, min_high=Q, min_low=Q)
    feeder = pm.HostFeeder(0, [])          # bytes arrive over SPI instead

    async def after(spi):
        for b in cmds:                      # the real host feeds the inbox as it drains
            while (await spi.levels())[0] >= 14:
                await ClockCycles(dut.clk, 200)
            await spi.write(R_INBOX0, [b])

    ls, spi = await lockstep_firmware(dut, words, 60000, models=[slave], after_load=after)
    assert slave.errors == []
    assert slave.mem[0x10] == 0x5C
    got = await spi.drain_outbox(0)
    assert got == [0, 0, 0, 0x5C], got


@cocotb.test(skip=GL)
async def test_lockstep_i2c_timeout(dut):
    """Stuck SCL: the slave never releases the clock after its ACK. The WRITE
    and the following STOP must each end with status 0xFF and the thread must
    be back waiting for a command, in lockstep with the model (D-018)."""
    Q = 30
    words, syms = fw("i2c_master.s", {"I2C_Q": Q, "I2C_TMO_PERIOD": 30, "I2C_TMO_TICKS": 8})
    cmds = [0x01, 0x03, 3, 0xA0, 0x10, 0x5C, 0x02]
    slave = pm.I2cSlaveModel(scl=2, sda=3, address=0x50, stretch=10 ** 9, min_high=Q, min_low=Q)
    ls, spi = await lockstep_firmware(dut, words, 20000, models=[slave], inbox0=cmds)
    got = await spi.drain_outbox(0)
    assert got == [0xFF, 0xFF], got
    t = ls.m.threads[0]
    assert t.running and t.blocked and t.pc == syms["i2c_cmd"] + 1
    assert int(dut.uio_oe.value) & 0x0C == 0


@cocotb.test(skip=GL)
async def test_lockstep_capture_replay_demo(dut):
    """fw/capture_demo.s on thread 1 captures thread 0's I2C transaction
    (trigger: START condition), then replays it on the same pins. The slave
    must see the same transaction twice with identical edge timing, the
    buffer drained over SPI must hold the recorded deltas, and both threads
    end halted or stopped (docs/CAPTURE.md section 4)."""
    Q = 30
    words, syms = fw_merge(("i2c_master.s", {"I2C_Q": Q}), ("capture_demo.s", None))
    base, length = 0xA4, 92
    cmds = [0x01, 0x03, 2, 0xA0, 0x10, 0x02, 0x05]      # START, write address + pointer, STOP, wake T1
    slave = pm.I2cSlaveModel(scl=2, sda=3, address=0x50, stretch=0, min_high=Q, min_low=Q)
    edges = EdgeLog(0x0C)

    async def setup(spi):
        await spi.write(R_PINMODE, [0x0C])                        # SCL, SDA open-drain
        await spi.cap_config(group=0, mask=0xC, tpat=0x4, tmask=0xC, base=base, length=length)
        await spi.rep_config(group=0, mask=0xC, base=base, length=0)
        await spi.set_pc(1, syms["cap_demo"])
        for b in cmds:
            await spi.write(R_INBOX0, [b])

    ls, spi = await lockstep_firmware(dut, words, 16000, models=[slave, edges], run_mask=0b10,
                                      after_load=setup, run_after=True)
    m = ls.m
    assert m.threads[1].halted and not m.threads[0].running, (m.threads[0].pc, m.threads[1].pc)
    assert slave.errors == [], slave.errors[:3]
    kinds = [e[0] for e in slave.events]
    assert kinds == ["start", "stop", "start", "stop"], kinds
    assert slave.ptr == 0x10 and len(slave.transfers) == 2
    starts = [c for k, c in slave.events if k == "start"]
    stops = [c for k, c in slave.events if k == "stop"]
    first = edges.deltas(starts[0] - 1, stops[0] + 5)
    second = edges.deltas(starts[1] - 1, stops[1] + 5)
    assert first == second, (first[:10], second[:10])
    # the recorded entries: count and deltas match the first transaction
    st = await spi.cr_status()
    assert st & 0b0100 and st & 0b10_0000 and not (st & 0b1000) and not (st & 0b100_0000), bin(st)
    n, k = await spi.cr_count()
    assert n == k and n >= 10, (n, k)
    entries = await spi.read_entries(base, n)
    deltas = [e >> 4 for e in entries]
    assert deltas[0] == 0 and deltas[1:] == first[:n - 1], (deltas[:12], first[:12])
    assert all(((e & 0xF) & ~0xC) == 0 for e in entries)


@cocotb.test(skip=GL)
async def test_lockstep_replay_host_waveform_and_loopback_capture(dut):
    """Both threads stopped: the host writes a waveform and replays it on
    uo2/uo3 (group 4); uo2 is wired to ui3 externally and a capture on group 2
    records it. Replay edges land exactly delta cycles apart; the capture
    reproduces the deltas."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)

    class Wire:                           # uo2 (pin 18) feeds ui3 (pin 11)
        def on_cycle(self, m):
            v = (m.pad() >> 18) & 1
            m.ext_ui = (m.ext_ui & ~0x08) | (v << 3)

    edges = EdgeLog(0x0C0000)
    ls = Lockstep(dut, pads, models=[Wire(), edges])
    wave = [(0, 0x4), (5, 0xC), (3, 0x8), (1, 0x0), (0, 0x4), (40, 0xC), (2, 0x0)]
    entries = [(d << 4) | p for d, p in wave]

    async def host():
        await spi.write_entries(0x40, entries)
        await spi.rep_config(group=4, mask=0xC, base=0x40, length=len(entries))
        await spi.cap_config(group=2, mask=0x8, tpat=0, tmask=0, base=0x80, length=16)
        await spi.cr_ctrl(0b0001)                                 # ARM capture (triggers at once)
        await spi.cr_ctrl(0b0100)                                 # START replay

    task = cocotb.start_soon(host())
    while not task.done():
        await ls.run(200)
    await ls.run(400)
    m = ls.m
    assert m.cr.rep_done and not m.cr.rep_under and m.cr.rep_k == len(wave)
    gaps = edges.deltas(0, 10 ** 9)
    exp = [max(d, 1) for d, _ in wave[1:]]
    # entry 3 -> 4 changes nothing on uo3? it does (0x0 -> 0x4): every entry changes the pads
    assert gaps[-len(exp):] == exp, (gaps, exp)
    task = cocotb.start_soon(spi.cr_ctrl(0b0010))                 # DISARM, mirrored by the lockstep
    while not task.done():
        await ls.run(50)
    await ls.run(20)
    assert m.cr.cap_done and not m.cr.cap_ovf
    n, _ = await spi.cr_count()
    rec = await spi.read_entries(0x80, n)
    # ui3 follows uo2 (bit 2 of each entry): its edges are the entries whose bit 2 changed
    uo2 = [(p >> 2) & 1 for _, p in wave]
    exp_rec, prev = [], 0                     # uo2 is 0 before the replay starts
    for v in uo2:
        if v != prev:
            exp_rec.append(v)
            prev = v
    got = [(e & 0x8) >> 3 for e in rec[1:]]
    assert got == exp_rec, (got, exp_rec)


@cocotb.test(skip=GL)
async def test_lockstep_random_programs(dut):
    """Constrained-random instruction streams, both threads, random pin wiggling."""
    import keyer_isa as I
    rng = random.Random(int(os.environ.get("KEYER_SEED", "1")))
    # directed prefix: writes to the reserved pin indices 24-31 must do nothing
    # (SEMANTICS 5.1, BUGS 15); the assembler rejects them, so encode by hand
    words = [I.encode("SET", pin=p) for p in range(24, 32)]
    words += [I.encode("CLR", pin=p) for p in range(24, 32)]
    words += [I.encode("SETC"), I.encode("WRC", pin=26), I.encode("OUTR", pin=27, rs=0)]
    for _ in range(120):
        ins = rng.choice([i for i in I.INSTRUCTIONS
                          if i.name not in ("HALT", "STOP", "JMP", "JMPR", "CALL", "RET",
                                            "WT0", "WT1", "WTR", "WTF", "WAITD", "POP",
                                            "PUSH", "DELAY", "WT0T", "WT1T", "WTRT", "WTFT",
                                            "POPT", "PUSHT", "CAPC")])
        ops = {}
        for opname, fname in ins.operands:
            if fname in ("rd", "rs", "r"):
                ops[opname] = rng.randrange(8)
            elif fname == "pin":
                ops[opname] = rng.randrange(32)        # 24-31 are reserved (SEMANTICS 5.1)
            elif fname in ("off8", "off6", "off5"):
                ops[opname] = rng.choice([1, 2, 3])          # forward only: no infinite loops
            elif fname == "imm8" or fname == "n8":
                ops[opname] = rng.randrange(256)
            elif fname == "simm8":
                ops[opname] = rng.randrange(-128, 128)
        words.append(I.encode(ins.name, **ops))
    words += [I.encode("HALT")] * 8
    half = len(words) // 2

    class Noise:
        def __init__(self, r):
            self.r = r

        def on_cycle(self, m):
            if self.r.random() < 0.1:
                m.ext_ui = (m.ext_ui & 0x07) | (self.r.randrange(256) & 0xF8)
                m.ext_uio = self.r.randrange(256)

    ls, _ = await lockstep_firmware(dut, words, 2500, models=[Noise(rng)],
                                    run_mask=0b11, pc1=half)
    assert ls.retired > 100
