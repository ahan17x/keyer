# SPDX-License-Identifier: Apache-2.0
"""Corners of the core, the pin unit, the capture and replay engines and the
program-memory wrapper that the full mutation campaign (tools/mutate.py,
GitHub run 37260274984) showed no test reached. Each test names the
mutants it kills; the lockstep tests also compare every drive register,
timer, PC, flag and engine register with the golden model in every cycle.
"""

import os

import cocotb
from cocotb.triggers import ClockCycles

import keyer_isa as I
import keyerasm
from keyer_tb import (Lockstep, SpiMaster, reset, R_CAP_BUF, R_CAP_CFG, R_CR_COUNT,
                      R_CR_CTRL, R_CTRL, R_INBOX0, R_IRQEN, R_LEVELS, R_PC0, R_PC1, R_PINMODE,
                      R_PINOUT, R_PINS, R_REP_BUF, R_REP_CFG, R_STAT)
from test import GL, start

# The behavioural program memory (KEYER_IMEM_FLOPS) has no SRAM macro to look at.
FLOPS = "KEYER_IMEM_FLOPS" in os.environ.get("COMPILE_ARGS", "")


def asm(src):
    """Assemble; only the words up to the last one used (test.asm pads to
    256 words, and loading those over SPI costs 33,000 cycles a test)."""
    words, syms, _ = keyerasm.assemble(src)
    return [words.get(a, 0) for a in range(max(words) + 1)], syms


class LockstepW(Lockstep):
    """keyer_tb.Lockstep with one comparison corrected. The harness compares
    the executing instruction word with the model's memory after the model
    has stepped the cycle, so a capture write in that cycle to the word the
    slot executes (a thread blocked or in a DELAY at the capture address:
    with one thread running, the free cycles are that thread's own slots)
    is reported as a mismatch although the RTL executes, as SEMANTICS 2.1 and
    14.4 say, the word as it was during the cycle. Here that word is the
    reference; every other check is the harness's own."""

    def _snapshot_host(self):
        self._word_during = self.m.imem[self.m.threads[self.m.cycle & 1].pc & 0xFF]
        return super()._snapshot_host()

    def _fail(self, what, exp, got):
        if what.startswith("ir ") and got == "%04X" % self._word_during:
            return
        super()._fail(what, exp, got)


async def lockstep_firmware(dut, words, cycles, models=(), run_mask=0b01, pc1=0, after_load=None,
                            run_after=False):
    """test.lockstep_firmware with LockstepW: load over SPI with the model
    mirroring every host action, RUN, then `cycles` cycles in lockstep."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    ls = LockstepW(dut, pads, models=models)

    async def host():
        await spi.load_program(words)
        if pc1:
            await spi.set_pc(1, pc1)
        if after_load and run_after:
            await after_load(spi)
        await spi.run(run_mask)
        if after_load and not run_after:
            await after_load(spi)

    task = cocotb.start_soon(host())
    while not task.done():
        await ls.run(100)
    await ls.run(cycles)
    assert ls.retired > 0
    return ls, spi


def w(name, **ops):
    """One instruction as a `.word` line: the assembler rejects the reserved
    pin indices 24-31, so instructions on them are encoded by hand."""
    return ".word 0x%04X" % I.encode(name, **ops)


class Watch:
    """A model for the lockstep harness that drives nothing: it records, for
    every cycle, thread 0's PC and running flag as they are during that cycle."""

    def __init__(self):
        self.log = []

    def on_cycle(self, m):
        t = m.threads[0]
        self.log.append((m.cycle, t.pc, bool(t.running)))

    def first(self, cond, after=0):
        return next(c for c, pc, run in self.log if c >= after and cond(pc, run))


class UiDriver:
    """Drives ui4..ui7 (firmware pins 12-15, capture group 3) to `nib`."""

    def __init__(self, nib=0):
        self.nib = nib

    def on_cycle(self, m):
        m.ext_ui = (m.ext_ui & 0x0F) | ((self.nib & 0xF) << 4)


async def lockstep_host(dut, body, models=()):
    """Run the host coroutine body(spi) with the lockstep harness watching
    (both threads stopped unless body starts them)."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    ls = LockstepW(dut, pads, models=list(models))
    task = cocotb.start_soon(body(spi))
    while not task.done():
        await ls.run(100)
    await ls.run(100)
    return ls, spi


# ---------------------------------------------------------------- pin unit and reserved pins

@cocotb.test(skip=GL)
async def test_lockstep_pin_modes_and_reserved_pins(dut):
    """SEMANTICS 5.1 and 5.3 instruction by instruction: OEN and OEF are
    ignored on an open-drain pin; pinwrite to an open-drain pin touches only
    that pin; OD releases the pin it names and no other; PP clears only its
    own od_mask bit (uio0 and uio6); OEN, OEF, OD, PP on pins >= 8 and SET on
    uo0/uo1 do nothing; the reserved pins 24-31 read 0 for RDC, TSTP, INR,
    BP1 and the timeout waits; the host PINMODE write leaves the uio_oe bits
    of the pins it does not name alone.
    Kills pins-983c45a8, pins-455f94c1 (OEN), pins-8d8945d2, pins-1849ef72
    (OEF), pins-84e03b92 (open-drain pinwrite), pins-4c33f4c6 (OD),
    pins-64cccf68, pins-ff23e1f7, pins-0fc6aa13, pins-a0a64703,
    pins-900fcd0c (PP), pins-96a9f093 (PINMODE), core-ddbd449f (level of
    pin 24), core-f7cb0614 (level2 of pin 24)."""
    words, syms = asm("""
            oen   uio1
            oen   uio3
            set   uio1
            set   uio3          ; uio1, uio3 push-pull outputs, high
            oen   uio5          ; uio5 push-pull output, low
            od    uio2
            od    uio6
            od    uio0
            oen   uio2          ; open-drain: ignored
            clr   uio2          ; drives uio2 low (oe = 1); uio1 and uio3 stay high
            oef   uio2          ; open-drain: ignored, uio2 stays driven low
            od    uio1          ; releases uio1 only (oe and out of uio1 cleared)
            pp    uio6          ; od_mask: only bit 6 cleared
            pp    uio0          ; only bit 0 cleared
            pp    ui3           ; pins >= 8: no effect
            oen   ui3
            oef   uo2
            od    uo3
            set   uo0           ; uo0, uo1 belong to the host interface: no effect
            set   uo1
            set   uo2
            setc
            ldi   r5, 1
            ori   r5, 0         ; Z = 0
            %s                  ; RDC 24: C = 0
            %s                  ; TSTP 24: Z = 1
            ori   r5, 0         ; Z = 0
            %s                  ; INR r4, 24: r4 = 0, Z = 1
            ldi   r6, 0
            %s                  ; BP1 24, +1: not taken
            ldi   r6, 0x11
            clc
            %s                  ; WTFT 24: the deadline is reached (NOW = DEADLINE = 0), no edge: C = 1
            %s                  ; WT0T 24: level 0, the base condition holds: C = 0
            %s                  ; WTRT 25: C = 1
            halt
    """ % (w("RDC", pin=24), w("TSTP", pin=24), w("INR", rd=4, pin=24), w("BP1", pin=24, off=1),
           w("WTFT", pin=24), w("WT0T", pin=24), w("WTRT", pin=25)))
    seen = {}

    async def host(spi):
        await ClockCycles(dut.clk, 150)
        seen["stat"] = await spi.read(R_STAT, 1)
        seen["before"] = (await spi.read(R_PINOUT, 2), await spi.read(R_PINMODE, 1))
        await spi.write(R_PINMODE, [0x01])                  # names uio0 only
        seen["after"] = (await spi.read(R_PINOUT, 2), await spi.read(R_PINMODE, 1))
        seen["pins"] = await spi.read(R_PINS, 3)

    ls, _ = await lockstep_firmware(dut, words, 100, after_load=host)
    t = ls.m.threads[0]
    assert t.halted and seen["stat"] == [0b000100], seen
    assert t.regs[4] == 0 and t.regs[6] == 0x11 and t.c == 1, (t.regs, t.c)
    # uio_out = uio3; uio_oe = uio2 (open-drain, low), uio3, uio5; od_mask = uio1, uio2
    assert seen["before"] == ([0x08, 0x2C], [0x06]), seen
    # PINMODE 0x01: od_mask = 0x01, uio_oe & ~0x01, uio_out & ~0x01 (5.3)
    assert seen["after"] == ([0x08, 0x2C], [0x01]), seen
    assert seen["pins"][2] == 0x04, seen                    # firmware uo_out: uo2 only


@cocotb.test(skip=GL)
async def test_lockstep_replay_writes_every_group(dut):
    """SEMANTICS 14.7 and 5.3: a replay entry writes exactly the masked pins
    of its group: group 0 uio0-3, group 1 uio4-7, group 4 uo18-19 (uo16, 17
    reserved), group 5 uo20-23, groups 2 and 3 (ui) and 6 and 7 (reserved)
    nothing. One entry per replay, threads stopped.
    Kills pins-b84bfce1, pins-7ffe3933, pins-3c5e28ed, pins-f742bd91,
    pins-bae6017d, pins-c6d1e425."""
    # (group, mask, pins): each is chosen so that a write to a pin outside
    # the group (uio0 or uio4 or uo20 with the entry's bit 0) changes a pad
    plan = [(0, 0x1, 0x1),        # uio0 <= 1; uio4 stays 0
            (1, 0xF, 0xE),        # uio5-7 <= 1, uio4 <= 0; uio0 stays 1
            (4, 0xF, 0xD),        # uo18, uo19 <= 1; uo16, uo17 reserved; uo20 stays 0
            (2, 0xF, 0xE), (3, 0xF, 0xE), (6, 0xF, 0xE), (7, 0xF, 0xE),   # nothing; uio0 stays 1
            (5, 0xF, 0x5)]        # uo20 <= 1, uo21 <= 0, uo22 <= 1, uo23 <= 0
    seen = []

    async def body(spi):
        await spi.write_entries(0x80, [p for _, _, p in plan])
        for i, (g, mask, _) in enumerate(plan):
            await spi.rep_config(group=g, mask=mask, base=0x80 + i, length=1)
            await spi.cr_ctrl(0b0100)                        # START
            await ClockCycles(dut.clk, 10)
            seen.append((await spi.cr_status(), await spi.read(R_PINOUT, 1), await spi.read(R_PINS, 3)))

    ls, _ = await lockstep_host(dut, body)
    assert all(st & 0x70 == 0x20 for st, _, _ in seen), seen          # each replay done, no underrun
    assert [po[0] for _, po, _ in seen] == [0x01, 0xE1, 0xE1, 0xE1, 0xE1, 0xE1, 0xE1, 0xE1], seen
    assert [p[2] for _, _, p in seen] == [0, 0, 0x0C, 0x0C, 0x0C, 0x0C, 0x0C, 0x5C], seen
    assert ls.m.uio_out == 0xE1 and ls.m.uo_out == 0x5C


# ---------------------------------------------------------------- DELAY, ADDI, JMP, soft reset

@cocotb.test(skip=GL)
async def test_lockstep_delay_jmp_addi_and_soft_reset(dut):
    """SEMANTICS 7.6: DELAY n occupies n + 1 slots (3 as the first
    instruction after reset, 0 and 1), and a DELAY stopped by host RUN = 0
    starts over when restarted; 4: ADDI sets Z on a zero result only; 2.4:
    JMP leaves LR alone; 3.2: a soft reset clears LR, Z, C and the DELAY
    count (also of a running thread, RST and RUN in one CTRL write).
    Kills core-ddeb8f89 (DELAY count reset to 1), core-0c5338ef,
    core-58b6fdba, core-8a5789ce (DELAY 0), core-c5d1de32 (host RUN = 0
    leaves a count of 1), core-05b3cc0c (ADDI Z), core-5db14d78 (JMP writes
    LR), core-44ea7f16 (soft reset ignored)."""
    words, syms = asm("""
            delay 3             ; address 0: the first instruction after reset
            delay 0
            delay 1
            ldi   r0, 5
            addi  r0, -5        ; 0: Z = 1, C = 1
            addi  r0, 1         ; 1: Z = 0, C = 0
            call  sub
    back:   cmpi  r0, 1         ; Z = 1
            setc                ; C = 1
    long:   delay 200           ; the host stops the thread in the middle and restarts it
            halt
    tail:   nop
    tdel:   delay 100           ; soft reset with RUN = 1 in the middle: starts over
            halt
    sub:    jmp   next
    next:   ret
    """)
    watch = Watch()
    seen = {}

    async def host(spi):
        await ClockCycles(dut.clk, 40)
        await spi.run(0)                                    # RUN = 0 inside DELAY 200
        seen["stop"] = watch.log[-1][0]
        seen["pc_stopped"] = await spi.read(R_PC0, 1)
        await spi.run(0b01)
        await ClockCycles(dut.clk, 500)
        seen["halt"] = await spi.read(R_STAT, 1)
        await spi.write(R_CTRL, [0b0100])                   # soft reset of thread 0, RUN = 0
        await spi.set_pc(0, syms["tail"])
        await spi.run(0b01)                                 # NOP: LR, Z, C are 0 (compared at its commit)
        seen["tail_run"] = watch.log[-1][0]
        await spi.write(R_CTRL, [0b0101])                   # soft reset with RUN = 1, inside DELAY 100
        await ClockCycles(dut.clk, 260)

    ls, _ = await lockstep_firmware(dut, words, 200, models=[watch], after_load=host)
    t = ls.m.threads[0]
    assert t.halted and t.pc == syms["tdel"] + 2 and t.lr == 0, (t.pc, t.lr)
    assert seen["pc_stopped"] == [syms["long"]] and seen["halt"] == [0b000100], seen
    # 7.6, measured on the model's PC: DELAY 3 first evaluated at the first slot s
    s = watch.first(lambda pc, run: run, after=0)
    s += s % 2
    t1 = watch.first(lambda pc, run: pc == 1)
    t2 = watch.first(lambda pc, run: pc == 2)
    t3 = watch.first(lambda pc, run: pc == 3)
    assert (t1 - s, t2 - t1, t3 - t2) == (7, 2, 4), (s, t1, t2, t3)
    # after the restart DELAY 200 takes 201 slots again
    r = watch.first(lambda pc, run: run, after=seen["stop"] + 1)
    r += r % 2
    assert watch.first(lambda pc, run: pc == syms["long"] + 1, after=r) - r == 401, r
    # DELAY 100 alone would leave tdel 202 cycles after the PC reached it; the
    # soft reset with RUN = 1 landed inside it and it started over
    y = watch.first(lambda pc, run: pc == syms["tdel"])
    assert y < seen["tail_run"], (y, seen["tail_run"])
    k = watch.first(lambda pc, run: pc == syms["tdel"] + 1, after=y)
    assert k - y > 202 + 20, (y, k)


@cocotb.test(skip=GL)
async def test_lockstep_delay_count_cleared_under_a_rewritten_word(dut):
    """SEMANTICS 7.6 and 14.4: the DELAY count is cleared whenever an
    instruction of the thread completes. The capture engine writes its
    trigger entry (0x0000, ADD r0, r0) over a DELAY 200 that thread 0 has
    just started; the thread executes the new word, and the following
    DELAY 3 takes 4 slots, not the rest of the old count.
    Kills core-53c4b1b2 (the count is not cleared on completion)."""
    words, syms = asm("""
            ldi   r0, 7
            ldi   r1, 1
            capc  r1            ; ARM: no trigger mask, triggers in the next cycle
    long:   delay 200           ; the trigger entry is written over this word
            delay 3
            halt
    """)

    class Corner:
        hit = False

        def on_cycle(self, m):
            if m.imem[syms["long"]] == 0 and m.threads[0].delay_left > 0 and m.threads[0].pc == syms["long"]:
                self.hit = True

    corner, watch = Corner(), Watch()

    async def setup(spi):
        await spi.cap_config(group=0, mask=0x0, tpat=0, tmask=0, base=syms["long"], length=1)

    ls, spi = await lockstep_firmware(dut, words, 300, models=[corner, watch], after_load=setup,
                                      run_after=True)
    t = ls.m.threads[0]
    assert corner.hit, "the entry did not land while the DELAY was counting"
    assert t.halted and t.regs[0] == 14, (t.halted, t.regs)
    a = watch.first(lambda pc, run: pc == syms["long"] + 1)
    b = watch.first(lambda pc, run: pc == syms["long"] + 2)
    assert b - a == 8, (a, b)                                # DELAY 3: 4 slots
    assert await spi.read_entries(syms["long"], 1) == [0x0000]


@cocotb.test(skip=GL)
async def test_lockstep_delay_count_untouched_while_blocked(dut):
    """SEMANTICS 7.1, 7.6 and 14.4: a blocked wait leaves the (zero) DELAY
    count alone. Thread 0 blocks on WT1 ui5; the capture's second entry,
    {0xF4F, 1} = DELAY 241 (ui4 rises 3919 cycles after the trigger), is
    written over the wait; the thread then occupies exactly 242 slots in it.
    Kills core-821a6331 and core-c887a9cd (a blocked instruction counts the
    DELAY count down)."""
    words, syms = asm("""
            ldi   r1, 1
            capc  r1            ; ARM: capture group 3, watch ui4, no trigger mask
    first:  nop                 ; the trigger entry {0, 0} is written here
    wait:   wt1   ui5           ; ui5 stays low; the second entry is written here
            halt
    """)
    delta = 0xF4F
    entry = (delta << 4) | 1
    assert I.decode(entry)[0].name == "DELAY" and I.decode(entry)[1]["n"] == 241

    class Ui4:
        """Raises ui4 so that level changes exactly `delta` cycles after the
        trigger entry; notes the first cycle the new word is in memory."""
        up = False
        written = None

        def on_cycle(self, m):
            cr = m.cr
            if cr.cap_trig and cr.cap_armed and cr.cap_dt == delta - 2:
                self.up = True
            m.ext_ui = (m.ext_ui & ~0x30) | (0x10 if self.up else 0)
            if self.written is None and m.imem[syms["wait"]] == entry:
                self.written = m.cycle

    ui4, watch = Ui4(), Watch()

    async def setup(spi):
        await spi.cap_config(group=3, mask=0x1, tpat=0, tmask=0, base=syms["first"], length=2)

    ls, spi = await lockstep_firmware(dut, words, 4600, models=[ui4, watch], after_load=setup,
                                      run_after=True)
    t = ls.m.threads[0]
    assert t.halted, t.pc
    assert await spi.read_entries(syms["first"], 2) == [0x0000, entry]
    # the word is in memory from cycle wr (written at the end of the even
    # cycle wr - 1); thread 0 evaluates it first at slot wr + 1 (fetched in
    # wr), and 241 slots later it completes: the PC moves on at wr + 484
    wr = ui4.written
    assert wr is not None and wr % 2 == 1 and watch.log[wr][1] == syms["wait"], wr
    assert watch.first(lambda pc, run: pc == syms["wait"] + 1) - wr == 2 * 241 + 2


# ---------------------------------------------------------------- capture

@cocotb.test(skip=GL)
async def test_lockstep_capture_groups_and_idle_entries(dut):
    """SEMANTICS 14 (group nibble) and 14.4: the capture sees the group it
    is configured for, all eight values of the field (groups 6 and 7 read
    0; cap_prev, compared every cycle, holds the nibble of the previous
    cycle), and with nothing changing it writes an idle entry {4095, pins}
    exactly 4095 cycles after the previous entry.
    Kills capture-83a1619f, capture-4dbecbc0, capture-171896c0,
    capture-eea44176 (nibble of groups 1, 3, 5, 6-7) and capture-54bf47c3
    (idle entry at 4094)."""
    words, _ = asm("""
            set   uo4
            set   uo6           ; group 5 (uo20-23) reads 0b0101
            halt
    """)
    ui = UiDriver(0xA)                                       # group 3 (ui4-7) reads 0b1010
    seen = {}

    async def host(spi):
        await ClockCycles(dut.clk, 20)
        for g in range(8):
            await spi.cap_config(group=g, mask=0xF, tpat=0, tmask=0, base=0x80, length=2)
            await ClockCycles(dut.clk, 6)
        await spi.cap_config(group=0, mask=0xF, tpat=0, tmask=0, base=0x80, length=2)   # uio0-3: pull-ups
        await spi.cr_ctrl(0b0001)
        await ClockCycles(dut.clk, 4150)
        seen["status"] = await spi.cr_status()

    ls, spi = await lockstep_firmware(dut, words, 50, models=[ui], after_load=host)
    assert seen["status"] & 0x0F == 0b0110, seen            # triggered, done, no overflow
    assert await spi.read_entries(0x80, 2) == [0x000F, 0xFFFF]


@cocotb.test(skip=GL)
async def test_lockstep_capture_queue_while_the_port_is_busy(dut):
    """SEMANTICS 14.1, 14.2, 14.4: with both threads running the port is
    never free, so entries wait in the two-entry queue in order; a third
    entry is lost and sets cap_ovf; the two are written in order once the
    threads stop. A later ARM clears cap_trig, cap_ovf, cap_n and cap_w, and
    an ARM while two entries are queued empties the queue.
    Kills capture-e950d47c (queue full test), capture-e1e33a47,
    capture-e2c2b056, capture-e124e5ee (where an entry lands in the queue),
    capture-d42d84e3 (ARM leaves the old capture's state) and
    capture-1d332f5d (ARM leaves the queue)."""
    words, _ = asm("spin: bra spin")
    ui = UiDriver(0x1)
    seen = {}

    async def host(spi):
        await spi.cap_config(group=3, mask=0xF, tpat=0, tmask=0, base=0x80, length=8)
        await spi.cr_ctrl(0b0001)                            # ARM: trigger entry {0, 1}, queued
        await ClockCycles(dut.clk, 20)
        ui.nib = 0x3                                         # second entry: the queue holds two
        await ClockCycles(dut.clk, 40)                       # nothing may change in a full queue
        ui.nib = 0x7                                         # third entry: lost
        await ClockCycles(dut.clk, 20)
        seen["full"] = (await spi.cr_status(), await spi.read(R_LEVELS, 4))
        await spi.run(0)                                     # the port is free: both are written
        await ClockCycles(dut.clk, 10)
        seen["first"] = (await spi.cr_status(), await spi.cr_count(), await spi.read_entries(0x80, 2))
        await spi.run(0b11)
        await spi.cr_ctrl(0b0001)                            # ARM again: trigger entry {0, 7}
        await ClockCycles(dut.clk, 20)
        ui.nib = 0xF                                         # and {d, F}: two queued
        await ClockCycles(dut.clk, 20)
        await spi.cr_ctrl(0b0001)                            # ARM with two queued: the queue is emptied
        await ClockCycles(dut.clk, 20)                       # trigger entry {0, F}
        await spi.cr_ctrl(0b0010)                            # DISARM
        await spi.run(0)
        await ClockCycles(dut.clk, 10)
        seen["second"] = (await spi.cr_status(), await spi.cr_count(), await spi.read_entries(0x80, 2))

    ls, spi = await lockstep_firmware(dut, words, 50, models=[ui], run_mask=0b11, after_load=host)
    st, _ = seen["full"]
    assert st & 0x0F == 0b1011, seen                         # active (queue non-empty), triggered, overflow
    st, (cap_w, _), ent = seen["first"]
    assert st & 0x0F == 0b1110 and cap_w == 2, seen          # done, overflow, two written
    assert ent[0] == 0x0001 and ent[1] & 0xF == 0x3 and (ent[1] >> 4) > 1, seen
    st, (cap_w, _), ent2 = seen["second"]
    assert st & 0x0F == 0b0110 and cap_w == 1, seen          # done, no overflow, one written
    assert ent2 == [0x000F, ent[1]], seen
    assert ls.m.imem[0x80:0x82] == ent2


# ---------------------------------------------------------------- replay

@cocotb.test(skip=GL)
async def test_lockstep_replay_underrun_at_saturation(dut):
    """SEMANTICS 14.6, 14.7: thread 0 starts a replay and then thread 1, so
    the port is free in exactly one cycle: entry 0 is fetched and applied,
    entry 1 never arrives, rep_dt saturates and the replay underruns when it
    reaches 4095, 4095 cycles after the apply.
    Kills capture-965340e2 (underrun at 4094) and capture-f1c12798
    (rep_dt saturating at 4094)."""
    words, syms = asm("""
    t0:     ldi   r1, 4
            capc  r1            ; replay START
            start               ; thread 1 runs from the next cycle on
    spin0:  bra   spin0
    t1:     bra   t1
    """)

    class Rep:
        applied = under = None

        def on_cycle(self, m):
            if self.applied is None and m.cr.rep_k == 1:
                self.applied = m.cycle
            if self.under is None and m.cr.rep_under:
                self.under = m.cycle

    rep = Rep()

    async def setup(spi):
        await spi.write_entries(0x80, [(0 << 4) | 0x4, (1 << 4) | 0x0, (1 << 4) | 0x4])
        await spi.rep_config(group=4, mask=0x4, base=0x80, length=3)

    ls, spi = await lockstep_firmware(dut, words, 4300, models=[rep], pc1=syms["t1"], after_load=setup,
                                      run_after=True)
    assert rep.applied is not None and rep.under is not None, (rep.applied, rep.under)
    assert rep.under - rep.applied == 4095, (rep.applied, rep.under)
    await spi.run(0)
    assert (await spi.cr_status()) & 0x70 == 0x60                   # done with the underrun flag
    assert (await spi.cr_count())[1] == 1
    assert ls.m.uo_out & 0x04


# ---------------------------------------------------------------- reset

@cocotb.test(skip=GL)
async def test_lockstep_hard_reset_in_the_middle_of_activity(dut):
    """SEMANTICS 3.1 from a busy machine: both threads running (thread 0 in
    a DELAY, thread 1 blocked), pins driven in both modes, the timer
    ticking, FIFOs holding data, IRQEN set, a capture armed and a replay
    active, then rst_n: every register reads 0 again (compared with a fresh
    model from the first cycle after reset, and over SPI), and a program
    whose first instruction is DELAY 3 takes 4 slots."""
    words, syms = asm("""
    t0:     ldi   r0, 3
            sett  r0
            od    uio0
            oen   uio1
            set   uio1
            set   uo3
            ldi   r1, 0b0101    ; ARM the capture and START the replay
            capc  r1
            push  r0
            start
            delay 250
            halt
    t1:     wt1   ui6
            bra   t1
    """)

    async def setup(spi):
        await spi.write_entries(0x80, [(0 << 4) | 0xF, (2000 << 4) | 0x0, (2000 << 4) | 0xF])
        await spi.rep_config(group=5, mask=0xF, base=0x80, length=3)
        await spi.cap_config(group=1, mask=0xF, tpat=0, tmask=0, base=0xA0, length=8)
        await spi.write(R_INBOX0, [1, 2, 3])
        await spi.write(R_IRQEN, [0xFF])

    ls, spi = await lockstep_firmware(dut, words, 150, pc1=syms["t1"], after_load=setup, run_after=True)
    m = ls.m
    assert m.threads[0].delay_left and m.threads[1].running and m.threads[0].period == 3
    assert m.uio_oe and m.od_mask and m.uo_out and m.cr.rep_active and m.cr.cap_armed

    pads = ls.pads
    await reset(dut, pads, cycles=3)
    assert int(dut.uo_out.value) == 0 and int(dut.uio_oe.value) == 0
    ls2 = LockstepW(dut, pads)
    seen = {}
    words2, _ = asm("""
            delay 3
            set   uo2
            halt
    """)
    watch = Watch()
    ls2.models.append(watch)

    async def host(spi):
        for reg, n in ((R_CTRL, 1), (R_STAT, 1), (R_PC0, 2), (R_PC1, 2), (R_LEVELS, 4), (R_PINMODE, 1),
                       (R_IRQEN, 1), (R_PINOUT, 2), (R_CR_CTRL, 1), (R_CR_COUNT, 2), (R_CAP_CFG, 2),
                       (R_CAP_BUF, 2), (R_REP_CFG, 1), (R_REP_BUF, 2)):
            seen[reg] = await spi.read(reg, n)
        await spi.load_program(words2)
        await spi.run(0b01)
        await ClockCycles(dut.clk, 30)

    task = cocotb.start_soon(host(spi))
    while not task.done():
        await ls2.run(100)
    await ls2.run(20)
    assert all(v == [0] * len(v) for v in seen.values()), seen
    t = ls2.m.threads[0]
    assert t.halted and ls2.m.uo_out == 0x04
    s = watch.first(lambda pc, run: run)
    s += s % 2
    assert watch.first(lambda pc, run: pc == 1) - s == 7


# ---------------------------------------------------------------- program memory macro

@cocotb.test(skip=GL or FLOPS)
async def test_program_memory_macro_static_inputs(dut):
    """The SRAM macro's static inputs have the values its vendor model
    requires (macro/RM_IHPSG13_1P_256x16_c2_bm_bist): A_DLY = 1 (the model's
    own check prints an error and calls $stop, which does not end a cocotb
    run, and its FUNCTIONAL core ignores A_DLY; in the transistor netlist
    A_DLY selects the internal pulse through three extra delay cells),
    A_BIST_EN = 0 (functional port), A_MEN = 1, A_BM all ones.
    Kills imem-e380ce49 (A_DLY tied to 0)."""
    await start(dut)
    sram = dut.user_project.u_imem.u_sram
    await ClockCycles(dut.clk, 2)
    got = {name: int(getattr(sram, name).value) for name in ("A_DLY", "A_BIST_EN", "A_MEN", "A_BM")}
    assert got == {"A_DLY": 1, "A_BIST_EN": 0, "A_MEN": 1, "A_BM": 0xFFFF}, got
