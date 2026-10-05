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


## 2026-10-02 (golden model, SEMANTICS v0.3 section 14: capture and replay)

### Q10. The fetch for cycle 0 and threads started before the first `step()`

Section 2.1 says the fetch is invalid in cycle 0, and v0.3 drops the
sentence that let the model equate "executes" with "running". In silicon no
thread runs during cycle 0 (`running` resets to 0), so the rule cannot be
observed there. In the model, however, a thread started by `host_run()`
before the first `step()` is running during cycle 0. The ISS tests rely on
it executing at cycle 0: `test_slot_interleave_and_instruction_cost`
expects retirements at 0, 2, 4, 6, and the timer, DELAY, synchroniser and
`test_replay_timing_and_done` tests depend on the same offset.

**Chosen in `tools/keyersim.py`:** host calls made before the first step
land at the end of a cycle -1 whose fetch is valid, so such a thread
executes at cycle 0. The lockstep harness starts threads over SPI long
after cycle 0, so it cannot see the difference. Proposed wording for 2.1:
"... and it is invalid in cycle 0 (no thread runs in cycle 0 after
`rst_n`; the model treats host calls made before its first step as
landing at the end of a cycle -1 with a valid fetch)."

### Q11. Does the head applied in cycle c count as "held" for the fetch in c? (blocks one test)

Section 14.6 fetches in a free cycle c if "fewer than two entries are held
or in flight". Under the observe-then-commit convention that is
`pf_count(c) + inflight(c) < 2`, and an entry applied in c is still held
during c. Consequence: even with every cycle free, the replay cannot apply
entries on three consecutive cycles. Two 1-cycle steps in a row (targets 1
and 1 after a longer gap) therefore underrun. `test_replay_timing_and_done`
(entries with deltas 0, 3, 1, 0, 7, all cycles free) expects gaps
3, 1, 1, 7. Under this reading it underruns. START lands at the end of
cycle 8. Entry 1 is applied at 14 with two entries held, so the fetch of
entry 3 waits until 15. Entry 2 is applied at 15, entry 3 is due at 16 but
held only from the end of 16, and the underrun comes at 17 (`rep_k = 3`).
Capture, by contrast, sustains one entry per cycle when every cycle is free,
so under this reading a recording of edges one cycle apart cannot be
replayed.

**Chosen in `tools/keyersim.py`:** the literal reading, `pf_count(c) +
inflight(c) < 2`. It is the simplest hardware: registered counts only, and
no path from the 12-bit `rep_dt == target` comparator into the memory
enable. The alternative is "fewer than two entries will be held or in
flight after this cycle's apply": `pf_count(c) - apply(c) + inflight(c) <
2`. It sustains one entry per cycle when every cycle is free, and with it
all 34 tests in `tools/test_iss.py` pass (checked on a scratch copy). In
the model it is a one-token change in `Machine._cr_cycle` (`pf_seen` becomes
`pf_seen - int(applied)`). The choice belongs in 14.6 either way, because
the RTL must make the same one.

### Q12. Capture queue full during c while an entry leaves it in c

Section 14.4: a produced entry "enters a two-entry queue unless the queue
holds two entries". It does not say whether a write of the oldest entry in
the same (free) cycle makes room.

**Chosen in `tools/keyersim.py`:** no. The queue occupancy during c decides,
as for the FIFOs in section 8, so the entry is lost and `cap_ovf` is set
even though one entry leaves at the end of c. This is the simplest hardware
(registered full flag). Proposed wording: "unless the queue holds two
entries during c (an entry written in c does not make room)".

### Q13. The prefetch word in flight when the replay starts, stops or ends

Section 14.6 says a fetched word is held from the end of the cycle after
the fetch. Section 14.5 says START empties the prefetch buffer and STOP only
clears `rep_active` and sets `rep_done`. Neither says what happens to a word
in flight. `pf_count` is compared in lockstep, so both sides must agree.

**Chosen in `tools/keyersim.py`:** START also drops a word in flight,
including one fetched in the START cycle itself by a replay that was still
active. Otherwise the restarted replay would hold a stale entry from the old
`rep_f`. STOP, underrun and the last apply do not drop it: the word still
lands at the end of the next cycle, so `pf_count` can rise by one after the
replay stopped. Its value no longer matters then, and the next START
clears it. In hardware this is the START clear having priority over the
in-flight valid bit and the buffer, with no other clear. Proposed wording
for 14.5: "START ... the prefetch buffer is emptied, including a word in
flight. STOP and the end of a replay do not affect a word in flight."

### Q14. `od_mask` seen by a replay apply in the cycle the core changes the mode

Section 14.7 applies the replay's `pinwrite` "after the core's pin command
of the same cycle". If that command is `OD p` or `PP p` on a replayed pin,
the replay's `pinwrite` could use `od_mask` during c or the value the core
just wrote.

**Chosen in `tools/keyersim.py`:** sequential composition. The replay sees
the core's new `od_mask`, `uio_out` and `uio_oe`, just as PINMODE sees the
primed values in 5.3, so `uio_out & od_mask == 0` still holds after the
cycle. Proposed wording: "applied after the core's pin command of the same
cycle (using the drive state and `od_mask` that command produced)".

### Q15. ARM with `cap_len = 0` and START with `n_rep = 0`: the other fields

Section 14.4 says ARM with `cap_len = 0` is "done at once", and 14.5 says
START with `n_rep = 0` "sets `rep_done` and leaves `rep_active` at 0". It is
not stated whether the rest of the ARM/START resets still apply, or in which
cycle "done" becomes visible.

**Chosen in `tools/keyersim.py`:** the action does everything it normally
does, except that `cap_armed <= (cap_len != 0)` and `rep_active <= (n_rep
!= 0)`, `rep_done <= (n_rep == 0)`. The general `cap_done` rule then gives
`cap_done = 1` from the cycle after the ARM cycle (disarmed, queue empty,
`cap_was_armed = 1`), and `rep_n` latches 0. In hardware the only change is
one gate on each active bit.

### Resolutions 2026-10-02, third batch (coordinating session)

10. Model convention accepted: a host call before the first `step()` lands at
    the end of a cycle -1 with a valid fetch, so the Python tests may start a
    thread at cycle 0; in silicon nothing executes in cycle 0 (2.1).
11. Resolved the other way: an entry applied in cycle c does not count
    towards the prefetch limit in c, so a fully free port replays entries
    one cycle apart (SEMANTICS 14.6).
12. Resolved symmetrically: an entry written to memory in cycle c frees its
    queue slot in c (SEMANTICS 14.4).
13. Resolved: the prefetch holds nothing while `rep_active` is 0; a word in
    flight at STOP or underrun is discarded on arrival; START empties it
    (SEMANTICS 14.6).
14. Resolved as chosen: a replay apply sees the `od_mask` the core's
    same-cycle OD/PP produced (it is applied after the core's command, 14.7).
15. Resolved as chosen: ARM with `cap_len = 0` and START with `n_rep = 0`
    perform all their other resets.


## 2026-10-02 (RTL, SEMANTICS v0.3 section 14: `src/keyer_capture.v`)

### Q16. `rep_dt` saturating at 4095 hides a late entry whose delta is 4095

Section 14.7 saturates `rep_dt` at 4095 and underruns only when
`rep_dt(c) > target`. With `target = 4095` that comparison is never true, so
an idle entry (delta 4095) that arrives late is applied as if on time. It
happens whenever the port is not free for more than 4095 cycles in the
middle of a replay (both threads running, or the host draining). Scratch
testbench: entries {0, 1}, {4095, 2}; thread 0 issues `CAPC` START, then
`START`s thread 1, and both run for about 5,000 cycles. Entry 0 is applied
at cycle 1452 and entry 1 at cycle 6597, 5,145 cycles later, with
`rep_under = 0`. With delta 100 instead, the same run underruns as it
should. This breaks the guarantee at the end of 14.7 ("entry k is applied
exactly `max(delta_k, 1)` cycles after entry k - 1 when no underrun
occurs"). Formal property C3 in `formal/capture_props.sv` is therefore
proved only for `delta < 4095`.

**Implemented in the RTL:** the literal rule (saturate at 4095, no extra
underrun), because the spec wins and the harness compares `rep_dt`.
**Proposed fix, cheapest first:** (a) no new state: also underrun when
`rep_k >= 1`, `rep_dt(c) = 4095` and no head is present (no entry can be on
time any more). This adds one AND term and sets the underrun earlier than
the current rule in starved replays. (b) One extra flop: `rep_dt` is 13
bits and saturates at 4096, so `rep_dt > target` catches it. Either makes
C3 hold for every delta.

**Update 2026-10-02:** resolved as (a) (SEMANTICS 14.7, fourth bullet). The
RTL now underruns when `rep_k >= 1`, no head is present and `rep_dt = 4095`,
and C3 is proved for every delta. In the scenario above, the late entry
now underruns in cycle 5547, entry 0 + 4095.

### Q17. Two-byte configuration registers written with fewer or more than two bytes

Section 10.3 lists CAP_CFG, CAP_BUF and REP_BUF as "bytes 0, 1" and the
model API takes them as 16-bit words. It does not say what a transaction
that ends after byte 0 does, or what bytes 2 and up do.

**Chosen in the RTL:** the register is written once, at the end of byte 1,
with `{byte 1, byte 0}` (one `*_we` pulse, landing like RUN). A transaction
that ends after byte 0 changes nothing. Bytes 2 and up are ignored, as for
CTRL. This is the cheapest version: byte 0 is held in the IMEM_DATA low-byte
register the host already has, and one write enable per register.
Proposed wording for 10.3: "bytes 0, 1 (written together when byte 1
completes; a shorter transaction has no effect, further bytes are
ignored)".

### Q18. Bit 3 of CAP_CFG byte 0 and of REP_CFG

Section 14 stores `group[2:0]` and `mask[3:0]` from bits 2:0 and 7:4, and
says the registers "read back as written" (10.4: "the bytes last
written"). Bit 3 has no field.

**Chosen in the RTL:** bit 3 is not stored and reads 0. For example, CAP_CFG
written FF A5 reads F7 A5, and REP_CFG written 3C reads 34. Storing it
would cost two flops for no function. Proposed wording for 10.4: "the
fields last written (bit 3 of CAP_CFG byte 0 and of REP_CFG reads 0)".

### Q19. What `pf_count` counts, and ARM in a cycle in which the capture records

`pf_count` is compared in lockstep. **Chosen in the RTL:** it counts entries
held in the prefetch (0..2) and does not count the word in flight. The
in-flight word is a separate one-bit register `pf_fly`, which the harness
does not probe. It is 0 whenever `rep_active` is 0, after resolution 13.

ARM landing in a cycle in which the armed engine triggers or records:
section 14.2 lists the registers that ARM resets. **Chosen in the RTL:**
only those. `cap_last` and `cap_dt` still take that cycle's trigger or
recording update (`cap_last <= s(c)`, `cap_dt <= 1` or `d + 1`), and a
queued entry still goes to memory if the cycle is free (the write happens,
then `cap_w` and the queue are reset). Gating these with ARM would cost
logic for no visible benefit. Proposed wording for 14.2: "ARM resets
exactly the registers listed; the cycle's other capture effects
(`cap_last`, `cap_dt`, a memory write) still happen."

### Q20. Replay group 4 and pins 16, 17

A replay on group 4 names pins 16-19. **Chosen in the RTL:** pins 16 and 17
(MISO, IRQ) are not written (5.3: "no effect"); 18 and 19 drive `uo[2]`,
`uo[3]`. This is the existing `pinwrite` table, stated here for
completeness; no change proposed.

### Resolutions 2026-10-02, fourth batch (coordinating session)

16. Resolved with the no-new-state fix: when `rep_k >= 1`, no head is present
    and `rep_dt = 4095`, the replay underruns (SEMANTICS 14.7, fourth
    bullet). Both model and RTL implement it; C3 then covers every delta.
17. Resolved as chosen: a two-byte register is written once, when its byte 1
    completes; a transaction ending after byte 0 changes nothing (this is
    how the model's single `host_*` call per register already behaves).
18. Resolved as chosen: unused bit 3 of CAP_CFG byte 0 and of REP_CFG reads 0.
19. Resolved as chosen: ARM resets only the registers 14.2 lists; the
    cycle's `cap_last`/`cap_dt` update and a free-cycle write still happen.
20. Noted: a replay on group 4 cannot touch pins 16 and 17 (5.3).


## 2026-10-04 (RTL, SEMANTICS v0.4 section 15: `src/keyer_ser.v`)

Written by the rtl subagent from SEMANTICS 15 alone (the golden model was
not opened). Each question gives the reading the RTL implements; where
there was a choice it is the cheaper one in gates.

### Q21. Are `rx_c5ok` and `rx_cok` cleared when a frame starts?

15.5 lists what a frame start clears: `rx_valid`, `rx_end`, `rx_ovr`,
`rx_serr`, `rx_ferr` (and it loads `crc5`, `crc_m`). The two CRC verdicts
are not in the list. 15.7 says "the verdict and error bits describe the
last frame that ended and stand until the next frame starts (15.5) or
`SERCFG`", which can be read as "the next frame start clears them".

**Chosen in the RTL:** the list of 15.5, literally: a frame start does not
touch `rx_c5ok` or `rx_cok`; they are rewritten by the next `end()` in
DATA and cleared by `SERCFG`. During a frame, `SERST` therefore shows the
previous frame's verdicts with bit 3 (in a frame) set. Clearing them
would cost two gates; no firmware pattern needs it, because a verdict is
only meaningful with `rx_end`, which the start does clear. Proposed
wording for 15.7: "The error bits (7 to 9) are cleared when a frame
starts; the verdict bits (5, 6) are written when a frame ends; all stand
until `SERCFG`."

### Q22. The CRC registers while the receiver does not run

15.4 lists what a non-running receiver resets and what it keeps
(`rx_hold`, `rx_valid`, `rx_end`, the error and verdict flags); `crc5` and
`crc_m` are in neither list. **Chosen in the RTL:** they keep their values
(the transmitter still writes `crc_m` by 15.3). This is the default "a
register no rule names does not change"; stated so the model agrees.
Proposed wording for 15.4: add "`crc5` and `crc_m` are not changed by the
receiver".

### Q23. "The first symbol is on the pads in the cycle after the second tick at which `tx_full` was seen" (15.3)

At the start tick `tx_full` is 1 and the start clears it; at the next tick
(the first data bit) `tx_full` is 1 only if firmware has already queued
the second byte. Read literally, "the second tick at which `tx_full` was
seen" would be later than the first data bit for a one-byte frame.
**Chosen in the RTL (rules 2 and 4 decide it anyway):** the first symbol
is on the pads in the cycle after the tick that follows the start tick,
that is T + 1 cycles after the start tick (`test/ser_unit` checks this
for every frame). Proposed wording: "the first symbol is on the pads in
the cycle after the first tick that follows the tick at which the
transmitter started (rule 2)".

### Q24. An unread byte or frame end discarded by the next frame start

A frame start clears `rx_valid`, `rx_end` and `rx_ovr` (15.5). If
firmware has not taken the last byte of a frame, or its frame end, when
the next sync arrives, both disappear and no flag records it (the overrun
flag is cleared in the same step). **Chosen in the RTL:** as written. A
"lost" bit would cost one flop. Question for Ahan: is a silent loss
acceptable here (firmware that keeps up never sees it), or should a
frame start set `rx_ovr` when it clears a set `rx_valid` or `rx_end`?

### Q25. A transmitter start abandons a frame being received

The receiver runs only while `tx_state = IDLE` (15.4). If firmware queues
a byte while a frame is arriving, the next tick starts the transmitter and
from the following cycle the receiver is held in hunt: the frame in
progress ends with no `end()`, so `rx_end` is not set and no verdict or
error is written. **Chosen in the RTL:** as written (half duplex). Noted
because firmware cannot tell from the status that a frame was cut off;
proposed wording for 15.4: "a frame being received when the transmitter
starts is dropped without a frame end".
