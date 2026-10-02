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

