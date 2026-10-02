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
from cocotb.triggers import ClockCycles, RisingEdge

import keyerasm
import protomodels as pm
from keyer_tb import (Lockstep, Pads, SpiMaster, reset, R_CTRL, R_ID, R_IMEM_ADDR,
                     R_IMEM_DATA, R_INBOX0, R_LEVELS, R_OUTBOX0, R_PINMODE, R_PINS,
                     R_STAT, R_PINOUT, R_PC0, R_PC1)

FW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fw")


def fw(name, symbols=None):
    with open(os.path.join(FW, name)) as f:
        words, syms, _ = keyerasm.assemble(f.read(), symbols=symbols)
    return keyerasm.to_list(words), syms


def asm(src, symbols=None):
    words, syms, _ = keyerasm.assemble(src, symbols=symbols)
    return keyerasm.to_list(words), syms


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
        # the memory itself
        for i, w in enumerate(words):
            assert int(dut.user_project.u_imem.mem[0x20 + i].value) == w
    # a full 256-word image in one transaction, read back in one transaction
    # (BUGS 14: the byte counter must not stop the low/high alternation)
    spi = SpiMaster(dut, pads, half=4)
    words = [random.randrange(0x10000) for _ in range(256)]
    await spi.load_program(words, base=0)
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


# ---------------------------------------------------------------- lockstep

async def lockstep_firmware(dut, words, cycles, models=(), run_mask=0b01, pc1=0,
                            inbox0=(), inbox1=(), half=4, after_load=None):
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
        await spi.run(run_mask)
        if after_load:
            await after_load(spi)

    task = cocotb.start_soon(host())
    while not task.done():                  # lockstep covers the host's SPI traffic too
        await ls.run(500)
    await ls.run(cycles)
    assert ls.retired > 0
    return ls, spi


@cocotb.test()
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


@cocotb.test()
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


@cocotb.test()
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


@cocotb.test()
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


@cocotb.test()
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


@cocotb.test()
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


@cocotb.test()
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
                                            "POPT", "PUSHT")])
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
