# Open questions on docs/SEMANTICS.md

Questions found while implementing one side (model or RTL) from the
contract. Each entry says which reading the implementation chose and why.
Resolve each one by editing SEMANTICS.md (with a DECISIONS entry if behaviour
changes), then mark the entry resolved here.

## 2026-10-02 (golden model, D-018 timer)

### Q1. Does a stopped thread's timer tick?

Section 2.3, last bullet: "A thread that is not running does nothing at its
slots and holds all its state." Section 6.1: "At the end of every cycle c,
for each thread t, unless t commits `SETT` or receives a soft reset in that
cycle: ..." These conflict for `prescale` and `NOW` of a thread that is
stopped or halted.

**Chosen in `tools/keyersim.py`:** the timer ticks every cycle whether or not
the thread is running (6.1 read literally; same as the pre-D-018 timer).
It is the simplest hardware too: the counter has no run enable. Proposed
wording for 2.3: "... holds all its state except the timer, which keeps
counting (section 6.1)."

### Q2. A soft reset in the same cycle as a commit by the same thread

A CTRL write with RSTn = 1 and RUNn = 1 can assert the soft reset of a
running thread in a cycle in which that thread commits an instruction that
writes a register the reset clears (`SETT`, `SETD`, `WAITD` for the timer;
any flag-writing instruction; `CALL` for LR; `POP`/`PUSH` for the FIFOs;
the `DELAY` count). Section 3.2 says those registers "become 0" and 6.1
excludes a soft-reset cycle from the tick rule, but neither says which wins
against a same-cycle instruction write. Section 8 settles it for FIFOs only
("a push in the same cycle is lost"), and 2.3 settles it for RUN (the host
wins).

**Chosen in `tools/keyersim.py`:** the soft reset wins for every register it
clears. The model applies `Thread.soft_reset()` after `step()` for that
cycle, which is how `test/keyer_tb.py` already calls it. In hardware the
synchronous clear has priority over the load enable. Proposed wording for
3.2: "If the thread commits an instruction in the same cycle, the reset
values win for every register listed here."

### Resolutions 2026-10-02 (coordinating session)

1. Resolved as chosen: the timer ticks whether or not the thread runs.
   SEMANTICS 2.3 now says "holds all its state except the timer".
2. Resolved as chosen: the soft reset wins over a same-cycle commit for the
   state it clears; SEMANTICS 3.2 now says so.

## 2026-10-02 (RTL, D-018 timer and D-019 NTHREADS)

### Q3. The T bit on other sub-opcodes of majors C and E

`docs/isa.md` section 3 shows the `t` field in every major-C (`1100 ffff t xx
ppppp`) and major-E (`1110 rrr ffff t xxxx`) word, but SEMANTICS 7.4 gives it
a meaning only for `WT0 WT1 WTR WTF PUSH POP`. SEMANTICS 2.2 calls an
encoding undefined only when its sub-opcode is not in the table, so `SET p`
with bit 7 set, or `RDS rd` with bit 4 set, is covered by neither rule.

**Chosen in `src/keyer_core.v`:** the T bit is ignored outside the six waits;
such a word executes as its base sub-opcode (`SET`, `RDS`, ...). This needs
no decode, so it is the cheapest. The golden model must do the same, or the
random-program lockstep test must not generate these words. Proposed wording
for 7.4: "On any other sub-opcode of majors C and E the T bit is ignored."

### Q4. Status word and "the other thread" for more than two threads

SEMANTICS is two-thread. For the area-study variant (`keyer_core` with
`NTHREADS` > 2, not instantiated by the top) the RTL defines "the other
thread" of `START`, `STOP` and RDS bit 5 as thread (tid + 1) mod NTHREADS, as
the D-019 task asked, and returns the thread id in RDS bits 6 and up (bit 6
alone for two threads, bits 7:6 for four). Nothing to decide until a
four-thread ISA is on the table; recorded so the choice is not mistaken for
part of the two-thread contract.
3. Resolved as chosen: the T bit is ignored by every PIN/XFER instruction
   other than the six waits (the model's decoder already does this, since
   only those six carry a fixed T value). SEMANTICS 7.4 now says so.
4. Noted; NTHREADS > 2 is a synthesis experiment, not part of the two-thread
   contract (DECISIONS D-019).

## 2026-10-02 (golden model rewritten from SEMANTICS v0.2, D-012)

### Q5. `level2` for pins 18-23 in cycles 2 and 3

Section 5.2: "`level2(c)[p] = level(c - 2)[p]` for all 24 pins (0 for cycles
0 to 3)". For pins 18-23, `level(1)[p] = uo_out(1)[p - 16]`, which is 1 if
thread 0 commits `SET uo2` at cycle 0, so the formula gives `level2(3)[18] =
1` while the parenthesis says 0. (For pins 0-15 the two agree, since
`level` is 0 in cycles 0 and 1.)

**Chosen in `tools/keyersim.py`:** the formula: `level2` is a two-stage
delay of the whole `level` vector, reset to 0, so it is 0 in cycles 0 and 1
only. That is the simplest hardware (no special case for the uo pins).
Proposed wording: "(0 for cycles 0 and 1, and for pins 0-15 also in cycles 2
and 3)".

### Q6. Pin indices 24-31

The 5-bit pin field of majors A, C and D can name pins 24-31, which do not
exist (the assembler rejects them, but the encoding allows them). Section 5
is silent.

**Chosen in `tools/keyersim.py`:** every read of such a pin (`RDC TSTP INR
BP0 BP1` and the waits) sees 0, and every write or mode change (`SET CLR WRC
OUTR OEN OEF OD PP`) has no effect. This is the simplest hardware: the
24-bit level vector is zero-extended, and the write decoder has no case for
these indices. Proposed wording for 5.1: "Pin indices 24-31 read 0; writes to
them have no effect."

### Q7. Host RUN write in the same cycle as a core START, STOP or HALT

Section 2.3 says the host's RUN value wins for `running` when a CTRL write
and a core-initiated change land in the same cycle. It does not say what
happens to the core change's other effects in that cycle: the `DELAY` count
cleared by `STOP` (the host writes RUN = 1 for the stopped thread), the
`halted` bit cleared by `START` (the host writes RUN = 0), and the `halted`
bit set by `HALT` (the host writes RUN = 1 for the halting thread).

**Chosen in `tools/keyersim.py`:** only `running` takes the host's value.
The `DELAY` count is cleared by any stop event (the core's `STOP` included,
even though the host keeps the thread running). `halted` is cleared by
`START`, and by a host RUN = 1, which has priority over a same-cycle `HALT`.
So `HALT` + RUN = 1 gives running = 1 and halted = 0; `START` + RUN = 0 gives
running = 0 and halted = 0; `STOP` + RUN = 1 gives running = 1 and a `DELAY`
that starts over. In hardware the clears are ORs of their events and the
host's write enable has priority on `running` and `halted`. These cycles
cannot be reached from firmware alone; they need a CTRL write timed to the
cycle.

### Q8. Full and empty for a host FIFO operation in the cycle the thread uses the FIFO

Section 8 says that a push on a full FIFO and a pop on an empty FIFO are
ignored, and that a same-cycle push and pop both take effect "provided
neither is ignored". It does not say explicitly that "full" and "empty" mean
the occupancy during the cycle. The question is whether a host push into an
inbox that is full during cycle c is ignored when the thread pops it in c,
and whether a host pop of an outbox that is empty during c is ignored when
the thread pushes in c.

**Chosen in `tools/keyersim.py`:** yes, both are ignored. Full and empty
are the registered occupancy during the cycle (observe, then commit; this is
also what 7.3 says for the thread side). The flags need no bypass, which is
the simplest hardware. In the model, a host call between `step(c)` and
`step(c + 1)` checks the occupancy during cycle c plus its own earlier calls
in that window. Consequence for test writers: right after a step in which
the thread pushed into an empty outbox, `host_outbox_pop` returns None
although `outbox` holds the byte, so a loop `while m.threads[t].outbox:
m.host_outbox_pop(t)` must not directly follow such a step. Proposed wording
for 8: "Full and empty are the occupancy during the cycle: a push on a FIFO
that is full during cycle c is ignored even if it is popped in c, and a pop
of a FIFO that is empty during c is ignored even if it is pushed in c."

### Q9. Pad bits for `uo[0]` and `uo[1]` in the model

Section 5.1 says the pad level of `uo` is `uo_out(c)`, and 5.3 makes
`uo_out` the firmware register `uo_out[7:2]`. The physical pads `uo[0]` and
`uo[1]` carry MISO and IRQ.

**Chosen in `tools/keyersim.py`:** `Machine.pad()` returns 0 in bits 16 and
17. The model has no SPI slave, and `irq()` reports the IRQ separately.
Firmware cannot see the difference, because `level` reads 0 on pins 16 and
17. If a protocol model ever needs the IRQ pad, bit 17 should become
`irq()`.

### Resolutions 2026-10-02, second batch (coordinating session)

5. Resolved by the formula: `level2` is a two-stage delay of `level`, reset
   to 0; SEMANTICS 5.2 reworded.
6. Resolved as chosen: pin indices 24-31 are reserved, read 0 and are ignored
   by writes, waits and mode changes (SEMANTICS 5.1). The RTL aliased writes
   to 26-31 onto uo[2..7]: BUGS 15, fixed; the random lockstep test now
   generates pin indices 0-31.
7. Resolved as chosen (matches the RTL's assignment order); SEMANTICS 2.3.
8. Resolved as chosen (registered occupancy, no bypass); SEMANTICS 8.
9. Resolved as chosen: `pad()` bits 16 and 17 are 0 in the pin unit's view;
   SEMANTICS 5.1.

