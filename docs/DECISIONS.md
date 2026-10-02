# Decision log

Append-only. One entry per decision that changes the architecture, the ISA,
the verification plan or the schedule. Newest at the bottom. Format: id,
date, who, decision, why, alternatives rejected. Entries marked OPEN are
waiting for Ahan.

## D-001 2026-10-01 Claude (Cowork session), accepted by default: Verilog-2005, Tiny Tapeout CMOS5L template unchanged

Why: the template requires a Verilog top and runs the whole flow in GitHub
Actions. Rejected: Hardcaml as the primary language (OCaml toolchain Ahan
does not know; reconsidered as a differentiator in D-016).

## D-002 2026-10-01 Claude: two barrel-interleaved hardware threads, two-stage pipeline

Thread 0 executes on even core clocks, thread 1 on odd. Every instruction
takes one slot of its thread; taken branches cost nothing; no hazards because
a thread's next fetch follows its own write-back. Why: full-duplex protocols
become two straight-line programs; a thread acts every 2 clocks, so firmware
edges dither by at most one clock. Rejected: a single thread (full duplex
needs interleaving by hand); four threads (halves the per-thread rate and
doubles edge dither; reopened in D-015).

## D-003 2026-10-01 Claude: 16-bit datapath, 16-bit instructions, eight registers per thread, register field always at bits [11:9]

Why: bytes are the common case and 16 bits hold counters and CRC-16; a fixed
register field position simplifies the decoder. Rejected: 32-bit
instructions (memory is the dominant area item).

## D-004 2026-10-01 Claude: program memory is the 256x16 IHP SRAM macro, with a flop model for simulation

Evidence: the CMOS5L liberty gives 49 um^2 per flop; a flop memory of this
size synthesised to ~275,000 um^2 (the whole usable budget), the macro is
28,127 um^2. The CMOS5L SRAM library is a symlink to the SG13G2 macros.
Confirmed by the organisers to another entrant on 2026-09-28 that macros may
go on the shuttle. Open: 256 vs 512 words (512x16 is 45,309 um^2).

## D-005 2026-10-01 Claude: SPI slave host port on ui[0..2] + uo[0], IRQ on uo[1], one clock domain

Why: five pins, every host has SPI, sampling SCK keeps one clock domain.
Rejected: parallel bus (eats pins), SCK as a clock (CDC). Consequence:
SCK <= clk/8. Note: ui[4..6]/uo[7] would match the RP2040 demo board's
hardware SPI0 pins; revisit if bit-banged host SPI proves too slow.

## D-006 2026-10-01 Claude: 24-pin flat pin space with per-pin open-drain mode on uio

In OD mode the pad is never driven high; writing 0 drives low, writing 1
releases. Proved as a formal property (formal/pins.sby).

## D-007 2026-10-01 Claude: per-thread 16-bit timer with a sticky one-bit tick; WAITT, SETT, DELAY

Why: bit edges land on timer ticks, not instruction counts. Known
limitations (see D-014): a one-bit tick loses ticks when a thread falls more
than one period behind; multi-period waits need loops; no timeouts.

## D-008 2026-10-01 Claude: per-thread inbox/outbox FIFOs, 16 x 8, blocking PUSH/POP plus non-blocking forms

## D-009 2026-10-01 Claude: RDLR/JMPR added for nested subroutines

Finding: writing the I2C firmware (byte routine calling bit routines) needed
to save the link register, and nothing could read it. Rejected: a hardware
return stack (RDLR/JMPR is cheaper and also gives computed jumps).

## D-010 2026-10-01 Claude: DJNZ added; the carry-flag hazard documented

Finding: DEC writes C and clobbered the data bit waiting for WRC in the UART
loop. DJNZ leaves flags alone and saves one slot per bit.

## D-011 2026-10-01 Claude: verification is a cycle-exact Python model run in lockstep with the RTL from reset, plus protocol models and formal proofs

The lockstep harness mirrors every host action seen at the RTL host interface
into the model in the same cycle. Weakness acknowledged: both sides were
written by the same session (see D-012).

## D-012 2026-10-02 Claude, proposed: model and RTL to be re-derived independently from the spec

The next rewrite of either side is done by a session or subagent that cannot
read the other (`.claude/agents/`). Tests may read both.

## D-013 2026-10-02 OPEN: rename the project

A friend's entry (thomasgilbert481/tt_um_loom) is also called Loom, with the
same tool names, because both were built by the same model family. Two Loom
entries from friends is a bad look. Rename before the repo goes public.

## D-014 2026-10-02 OPEN: timeouts on waits, and the timer model

Proposal A (small): a timeout bit in the spare field of WT0/WT1/WTR/WTF and
POP/PUSH, meaning "or give up when the tick is pending", with C = 1 on
timeout. Proposal B (larger): replace the sticky tick with a NOW/deadline
pair (WAITD k advances the deadline by k ticks; SETD re-anchors), which also
removes lost ticks and multi-period loops. Cost of B: about 25 flops per
thread and a comparator. Needed either way: the I2C firmware hangs forever on
a stuck SCL today.

## D-015 2026-10-02 OPEN: two threads or four

Four threads cost one more register file per pair (~256 flops) and halve the
per-thread rate (15 MIPS at 60 MHz) and double edge dither (0-3 clocks).
They allow three or four protocols at once, which fits the debugging use
case. The RTL indexes thread state by tid, so the count can be a parameter;
synthesise both before deciding.

## D-016 2026-10-02 OPEN: the differentiator

Candidates: (a) capture and replay (timestamped edge recording with
triggers, waveform playback), the logic-analyser half of Jane Street's
stated use, which no entry has; (b) stay deliberately small (routes in
minutes, every module readable); (c) a Hardcaml implementation of the core.
At least one must be chosen; a design that only repeats the parallel entry
with less verification loses.

## D-017 2026-10-02 Ahan: the project is named Keyer (closes D-013)

A telegraph keyer turns a program into precisely timed marks and spaces on a
wire, which is what this chip does; it is not a weaving metaphor and is
distinct from the sibling entry. Renamed in one commit: project name, top
module `tt_um_ahan17x_keyer`, every `loom_` module and file, the tools
(`keyer_isa.py`, `keyerasm.py`, `keyersim.py`) and their imports, tests, docs,
scripts, info.yaml, README. The ID register byte changes from 'L' (0x4C) to
'K' (0x4B). History (DECISIONS, WORKLOG entries before this date) keeps the
old names. Rejected: Baud, Morse, Edgewise, Tempo; renaming only the public
name and keeping `loom_` inside (half a rename, still looks like the sibling).

## D-018 2026-10-02 Ahan: per-thread NOW/deadline timer with timeouts on every blocking instruction (closes D-014, proposal B)

The sticky tick goes. Each thread keeps a tick period (`SETT rs`, which
restarts the timer), a tick counter NOW that advances once per period, and a
DEADLINE. `SETD k` sets DEADLINE = NOW + k; `WAITD k` blocks until NOW has
reached DEADLINE + k and then advances DEADLINE by k, so a late loop catches
up without losing ticks and a multi-period wait is one instruction. Every
blocking instruction (`WT0 WT1 WTR WTF POP PUSH`) gets a timeout variant that
also completes when the deadline is reached and reports C = 1 on timeout,
C = 0 on success. `WAITT`, `CLRT` and `BTP` are replaced (WAITD, SETD, BDR);
`RDT` reads NOW - DEADLINE; status bit 4 means "deadline reached". `DELAY` is
unchanged. Instruction count and encodings change as little as possible; the
exact cycle rules are in `docs/SEMANTICS.md`. Cost: about 32 more flops per
thread and two 16-bit adders. Why: the I2C firmware hangs forever on a stuck
SCL today, and the sticky tick loses ticks when a loop falls more than one
period behind. Rejected: proposal A (timeout bit on the sticky tick: keeps
lost ticks and multi-period loops); a separate "advance deadline" instruction
before each wait (halves the peak bit rate); deadlines in core cycles against
CYC (needs a multiply by the period).

## D-019 2026-10-02 Ahan: two threads; the thread count becomes a module parameter (closes D-015)

The ISA and firmware stay two-thread. The core takes an `NTHREADS` parameter
(default 2) so a four-thread variant can be synthesised and compared later;
no other change. Rejected: switching to four now (halves the per-thread rate,
doubles edge dither, about 256 more flops, and no firmware needs it yet).

## D-020 2026-10-02 Ahan: the differentiator is capture-and-replay plus staying small; Hardcaml is out of scope (closes D-016)

Timestamped edge recording on a pin mask into a buffer the host drains, with
a trigger condition, and playback of a recorded waveform: the logic-analyser
half of Jane Street's stated use. Designed after SEMANTICS.md exists, in a
later session, not this one. Staying small (routes in minutes, every module
readable) is the second half. Rejected: a Hardcaml port (a toolchain nobody
on the team knows; it would duplicate the core rather than add to it);
repeating the sibling entry's feature list with less verification.

## D-021 2026-10-02 Ahan: keep the current core and evolve it; no blank-page redesign

The core passes lockstep, formal and protocol tests; the timer change (D-018)
and the differentiator (D-020) are additive. Rejected: a redesign from a
blank page on the same test infrastructure (weeks of schedule for an
unproven gain, three and a half months before the deadline).

## D-022 2026-10-02 Ahan: the gds workflow hardens only on hardware changes, and a newer push cancels the run it supersedes

The trigger block of `.github/workflows/gds.yaml` (never its jobs, which
stay as the Tiny Tapeout template wrote them) gets a `paths` filter
(`src/**`, `info.yaml`, `macro/**`, the workflow file itself) plus
`workflow_dispatch`, and a top-level concurrency group per branch with
`cancel-in-progress`. Why: a hardening run takes hours of shared CI; docs and
tool commits must not start one, and two pushes in quick succession should
not harden both. Manual runs on a chosen commit use the dispatch. Rejected:
the template trigger (every push hardens); a second workflow file (the
template's jobs would still run on every push).
