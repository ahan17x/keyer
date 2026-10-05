# SPDX-License-Identifier: Apache-2.0
"""Corner cases that mutation testing showed the suite did not reach
(tools/mutate.py; each test names the fault it kills in docs/VERIFICATION.md).
All run in lockstep with the golden model unless they only use the pads.
"""

import random

import cocotb
from cocotb.triggers import ClockCycles

import keyer_isa as I
from keyer_tb import (R_CTRL, R_INBOX0, R_LEVELS, R_OUTBOX0, R_STAT, SpiMaster)
from test import GL, asm, lockstep_firmware, start

R_INBOX1, R_OUTBOX1 = R_INBOX0 + 2, R_OUTBOX0 + 2


def undefined_words(rng, per_major=6):
    """Encodings the table does not define, a few per major opcode, with
    random operand fields (SEMANTICS 2.2: they complete with no effect)."""
    words = []
    for major in range(16):
        found, tries = 0, 0
        while found < per_major and tries < 4000:
            tries += 1
            w = (major << 12) | rng.randrange(1 << 12)
            if I.decode(w)[0] is None:
                words.append(w)
                found += 1
    return words


@cocotb.test(skip=GL)
async def test_lockstep_undefined_encodings(dut):
    """Undefined sub-opcodes of every major that has any complete with no
    effect but PC + 1: no register, flag, pin, FIFO or timer change, with
    both flags set and with both clear."""
    rng = random.Random(7)
    und = undefined_words(rng)
    assert len({w >> 12 for w in und}) >= 5, sorted({w >> 12 for w in und})
    pre = [I.encode("LDI", rd=r, imm=0x11 * (r + 1)) for r in range(8)]
    pre += [I.encode("LDI", rd=0, imm=9), I.encode("SETT", rs=0), I.encode("OD", pin=2), I.encode("OEN", pin=1)]
    flags1 = [I.encode("CMPI", rs=7, imm=0x88), I.encode("SETC")]          # Z = 1, C = 1
    flags0 = [I.encode("CMPI", rs=7, imm=0x01), I.encode("CLC")]           # Z = 0, C = 0
    words = pre + flags1 + und + flags0 + und + [I.encode("HALT")]
    assert len(words) <= 256
    ls, _ = await lockstep_firmware(dut, words, 2 * len(words) + 200, inbox0=[0x5A, 0xA5])
    t = ls.m.threads[0]
    assert t.halted and t.pc == len(words) and ls.retired >= len(words)
    assert t.regs[1:] == [0x11 * (r + 1) for r in range(1, 8)] and len(t.inbox) == 2 and not t.outbox


@cocotb.test(skip=GL)
async def test_lockstep_status_word_fifo_bits(dut):
    """RDS in every FIFO state: inbox full / neither / empty, outbox empty /
    neither / full (bits 0-3), on both threads, so bit 6 and bit 5 differ."""
    words, syms = asm("""
    t0:     rds   r0            ; inbox full (16 queued), outbox empty
            pop   r1
            rds   r2            ; inbox neither full nor empty
            ldi   r3, 15
    drain:  pop   r1
            djnz  r3, drain
            rds   r4            ; inbox empty
            ldi   r3, 15
    fill:   push  r3
            djnz  r3, fill
            rds   r5            ; outbox neither
            push  r3
            rds   r6            ; outbox full
            start               ; thread 1 looks at its own, both empty
            halt
    t1:     rds   r0
            push  r0
            rds   r1
            halt
    """)

    async def setup(spi):
        await spi.write(R_INBOX0, list(range(16)))

    ls, spi = await lockstep_firmware(dut, words, 600, pc1=syms["t1"], after_load=setup, run_after=True)
    t0, t1 = ls.m.threads
    assert t0.halted and t1.halted
    assert [t0.regs[i] & 0xF for i in (0, 2, 4, 5, 6)] == [0b0110, 0b0100, 0b0101, 0b0001, 0b1001], t0.regs
    assert t1.regs[0] & 0x4F == 0b100_0101 and t1.regs[1] & 0xF == 0b0001
    assert await spi.read(R_LEVELS, 4) == [0, 16, 0, 1]


@cocotb.test()
async def test_thread1_fifos_from_the_host(dut):
    """INBOX1 and OUTBOX1 over SPI (the other tests feed thread 0): an echo
    on thread 1 only, thread 0 stopped; LEVELS, STAT and the data."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    words, _ = asm("""
    loop:   pop  r0
            xori r0, 0xFF
            push r0
            bra  loop
    """)
    await spi.load_program(words)
    await spi.run(0b10)
    data = [0x00, 0x01, 0x7F, 0x80, 0xFE]
    await spi.write(R_INBOX1, data)
    await ClockCycles(dut.clk, 60)
    assert await spi.read(R_LEVELS, 4) == [0, 0, 0, len(data)]
    assert await spi.read(R_STAT, 1) == [0b100010]            # thread 1 running and blocked on POP
    await spi.read(R_OUTBOX0, 2)                              # empty: a stale byte, and outbox 1 untouched
    assert await spi.read(R_LEVELS, 4) == [0, 0, 0, len(data)]
    assert await spi.read(R_OUTBOX1, len(data)) == [b ^ 0xFF for b in data]
    assert await spi.read(R_LEVELS, 4) == [0, 0, 0, 0]
    await spi.write(R_CTRL, [0])


@cocotb.test()
async def test_two_byte_registers_take_exactly_two_bytes(dut):
    """CAP_CFG, CAP_BUF and REP_BUF are written once, after byte 1
    (SEMANTICS 10.3): a one-byte write changes nothing and a third byte is
    ignored."""
    from keyer_tb import R_CAP_BUF, R_CAP_CFG, R_REP_BUF
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    for reg, first, second in ((R_CAP_CFG, [0xA5, 0x73], [0x12, 0x34]), (R_CAP_BUF, [0xC0, 40], [0x11, 0x22]),
                               (R_REP_BUF, [0x80, 7], [0x33, 0x44])):
        await spi.write(reg, first)
        assert await spi.read(reg, 2) == first
        await spi.write(reg, [second[0]])                    # one byte: no write
        assert await spi.read(reg, 2) == first, reg
        await spi.write(reg, second + [0xEE])                # three bytes: the first two count
        assert await spi.read(reg, 2) == second, reg
        await spi.write(reg, [0x5A])
        assert await spi.read(reg, 2) == second, reg


@cocotb.test()
async def test_miso_is_low_outside_a_transaction(dut):
    """MISO (uo[0]) is 0 while CS_n is high: after reset, between
    transactions, and after a read that ended with ones on the wire
    (proposed for SEMANTICS 10.1, DECISIONS D-033)."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    await ClockCycles(dut.clk, 6)
    assert int(dut.uo_out.value) & 1 == 0
    await spi.write(R_INBOX0 + 5, [0xFF])                    # PINMODE = 0xFF, reads back as ones
    assert await spi.read(R_INBOX0 + 5, 1) == [0xFF]
    for _ in range(12):
        assert int(dut.uo_out.value) & 1 == 0
        await ClockCycles(dut.clk, 1)
    await spi.write(R_INBOX0 + 5, [0x00])


async def _lockstep_host(dut, body, models=()):
    """Run the host coroutine `body(spi)` with the lockstep harness watching."""
    from keyer_tb import Lockstep
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    ls = Lockstep(dut, pads, models=list(models))
    task = cocotb.start_soon(body(spi))
    while not task.done():
        await ls.run(100)
    await ls.run(100)
    return ls, spi


@cocotb.test(skip=GL)
async def test_lockstep_capture_trigger_needs_a_transition(dut):
    """SEMANTICS 14.3: with a trigger mask, the capture triggers when the
    group starts to match, not while it already matches at ARM. ui3 is high
    when the capture is armed (no trigger); it triggers at the next rise."""

    class Ui3:
        level = 1

        def on_cycle(self, m):
            m.ext_ui = (m.ext_ui & ~0x08) | (self.level << 3)

    pin = Ui3()
    seen = {}

    async def body(spi):
        await spi.cap_config(group=2, mask=0x8, tpat=0x8, tmask=0x8, base=0x80, length=8)
        await spi.cr_ctrl(0b0001)                                 # ARM while the pattern already matches
        await ClockCycles(dut.clk, 40)
        seen["armed"] = await spi.cr_status()
        pin.level = 0
        await ClockCycles(dut.clk, 20)
        seen["low"] = await spi.cr_status()
        pin.level = 1
        await ClockCycles(dut.clk, 20)
        seen["rise"] = await spi.cr_status()
        await spi.cr_ctrl(0b0010)                                 # DISARM

    ls, spi = await _lockstep_host(dut, body, models=[pin])
    assert seen["armed"] & 0b11 == 0b01 and seen["low"] & 0b11 == 0b01, seen     # active, not triggered
    assert seen["rise"] & 0b10, seen
    assert ls.m.cr.cap_done and (await spi.cr_count())[0] == 1


@cocotb.test(skip=GL)
async def test_lockstep_replay_stop_and_underrun(dut):
    """SEMANTICS 14.5-14.7: STOP in the middle of a replay empties the
    prefetch and ends it; a replay whose entries are one cycle apart while a
    thread is running (the port is free only every other cycle) ends with
    the sticky underrun flag, and nothing is applied after it."""
    words, _ = asm("spin: bra spin")
    wave = [(0, 0x4)] + [(400, 0xC), (400, 0x0), (400, 0x4)] + [(1, 0xC), (1, 0x0)] * 5
    entries = [(d << 4) | p for d, p in wave]
    seen = {}

    async def body(spi):
        await spi.load_program(words)
        await spi.write_entries(0x40, entries)
        await spi.rep_config(group=4, mask=0xC, base=0x40, length=4)
        await spi.cr_ctrl(0b0100)                                 # START: entries 400 cycles apart
        await spi.cr_ctrl(0b1000)                                 # STOP while entries wait in the prefetch
        seen["stopped"] = (await spi.cr_status(), await spi.cr_count())
        await spi.rep_config(group=4, mask=0xC, base=0x44, length=10)
        await spi.run(0b01)                                       # thread 0 spins: half the port slots are gone
        await spi.cr_ctrl(0b0100)
        await ClockCycles(dut.clk, 60)
        seen["under"] = (await spi.cr_status(), await spi.cr_count())
        await spi.run(0)

    ls, spi = await _lockstep_host(dut, body)
    st, (_, k) = seen["stopped"]
    assert st & 0b0111_0000 == 0b0010_0000 and 1 <= k < 4, seen   # done, not active, no underrun
    st, (_, k) = seen["under"]
    assert st & 0b0111_0000 == 0b0110_0000 and 1 <= k < 10, seen  # done with the underrun flag
    assert ls.m.cr.pf_count == 0 and ls.m.cr.rep_under
