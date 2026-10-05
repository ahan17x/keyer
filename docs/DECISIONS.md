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

## D-023 2026-10-02 OPEN: the template's `test` workflow fails on a passing run

`.github/workflows/test.yaml` (Tiny Tapeout template) ends its test step with
`! grep failure results.xml`. cocotb 2 writes `failures="0"` into every
results file, so the step fails whenever the suite passes (first CI run on
GitHub, 2026-10-02: 11 of 11 tests passed, step exit code 1). The rule in
CLAUDE.md is not to edit the template's jobs, so this entry proposes the
fix and waits for Ahan: change that line to `! grep '<failure' results.xml`
(one token; the template's intent is "no <failure> element"). Alternative,
rejected: pin cocotb 1.x (the harness uses the cocotb 2 API). Meanwhile the
project's own `check` workflow (renamed from `lint`, now the full
`scripts/check_all.sh` including cocotb and formal) is the CI signal to
trust, and the `test` badge stays red until this is decided.

## D-024 2026-10-02 Claude (within the mandate of D-020 and the 25% area rule): capture entries live in the program memory; engines use only a stopped thread's fetch slots

Capture and replay (docs/CAPTURE.md) store 16-bit entries
`{delta[11:0], pins[3:0]}` in the program memory at a host-assigned base and
length, and use the memory's single port only in a cycle whose fetch would
serve a thread that is not running, with the host always first. Why: a
dedicated flop buffer of 32 entries would already take 23% of the core and
hold one I2C byte, 64 entries would exceed the 25% limit; the program
memory holds 120 entries beside the I2C firmware and 255 alone, the engines
cost about 185 flops, and a running thread's timing is never disturbed.
Four-pin groups with a watch mask, a 12-bit delta with idle entries, a
transition-into-match trigger, a two-entry write queue with a sticky
overflow flag, a two-entry prefetch with a sticky underrun flag. Rejected:
a 32- or 64-entry flop buffer (area, capacity); a second SRAM macro (fits,
but doubles the macro-flow risk before the first macro is through; kept as
a later option); stealing fetch slots from a blocked running thread (would
make firmware timing depend on the capture).

## D-025 2026-10-02 Claude (branch sram-macro, within the mandate of step 5 of Ahan's instructions; not merged): the program memory is the RM_IHPSG13_1P_256x16 macro in the flow

On branch `sram-macro` the macro is the default implementation in
`src/keyer_imem.v` (`KEYER_IMEM_FLOPS` selects the flop fallback), the macro
files are vendored in `macro/RM_IHPSG13_1P_256x16_c2_bm_bist/` (IHP Open PDK
`dev` at bf079026, Apache-2.0), and `src/config.json` gains, against the
rule of CLAUDE.md and only on this branch: a `MACROS` block (instance
`u_imem.u_sram`, FS at (12, 40)), `PDN_MACRO_CONNECTIONS`, `PDN_CFG`
(`src/pdn_cfg.tcl`), the Magic waivers `ERROR_ON_MAGIC_DRC`,
`ERROR_ON_ILLEGAL_OVERLAPS`, `MAGIC_EXT_ABSTRACT_CELLS`,
`MAGIC_MACRO_STD_CELL_SOURCE`, and the stripe keys `FP_PDN_VPITCH 67.44`,
`FP_PDN_VSPACING 3.52`, `FP_PDN_VOFFSET 26.36`. The recipe is the one that
took the sibling entry's 512 x 16 macro through hardening, precheck and
gate-level test (thomasgilbert481/tt_um_loom, Apache-2.0,
`docs/tt_cmos5l_facts.md` section 11 and `src/pdn_cfg.tcl`; attribution in
the file headers). Tiny Tapeout had published no macro template on
2026-10-02. The cocotb suite now simulates the vendored macro model instead
of the behavioural array, which also checks the wrapper's enable polarity.
Rejected: waiting for the official template (unknown date); a second
branch per experiment (one branch, one hardening run, then decide).

## D-026 2026-10-03 OPEN: setup timing fails at 20 ns in the slow corner (first hardening run)

Run 37073185698 (commit d979e82, program memory as flops, before capture and
replay): setup slack +11.46 ns fast, +7.09 ns typical, **-0.59 ns slow**
(1.08 V, 125 C), 12 endpoints, all bits of `u_core.regs[15]`. Critical path:
instruction register (the program memory's read-data flop) -> major-opcode
decode -> ALU and result selection -> register-file write-enable and data
distribution -> thread 1's r7; about 20.06 ns of logic and wire, with 1.6 to
2.1 ns slews on high-fanout nets driven by size-1 cells, at 66.9%
utilisation. Details in docs/AREA.md. `CLOCK_PERIOD` was not changed.
Options for Ahan:

- (a) Harden the `sram-macro` branch first (prepared, committed locally, not
  pushed: D-025). The macro removes about 300,000 um^2 of memory flops and
  muxes (utilisation falls to roughly 20%), so the decode and write-enable
  nets get short, and the path then starts at the macro's output. It is the
  intended tapeout configuration anyway; the flop memory was only ever the
  fallback. No RTL or clock change. Recommended first step.
- (b) Keep 20 ns and restructure the RTL without changing SEMANTICS:
  duplicate or pre-decode the opcode decode, cut the fanout of the register
  write enables, re-balance the ALU result mux. Cheap to try, verified by
  the lockstep suite, another three to five hours per hardening run.
- (c) Flow knobs in `src/config.json` beyond the two keys CLAUDE.md allows
  (synthesis for delay, a fanout constraint, resizer slack margins): needs
  Ahan's explicit agreement.
- (d) Relax `CLOCK_PERIOD` (22 ns = 45 MHz would cover this path): costs the
  60 MHz goal of PLAN.md and the UART/USB divisors chosen for it.
- (e) A pipeline register between decode and execute: changes the cycle
  contract (SEMANTICS section 2) and every timing test; the last resort.

## D-027 2026-10-03 Claude: the test Makefile makes cocotb 2 result files pass the Tiny Tapeout check (closes D-023 unless Ahan prefers the template edit)

The `gl_test` job runs the same `! grep failure results.xml` as the template
`test` workflow, inside Tiny Tapeout's action, which cannot be edited at
all. So instead of editing a template job (D-023), `test/Makefile` (the
project's own file) drops the `failures="0"` attribute from `results.xml`
after a run that has no `<failure>` element. A failing test still writes
`<failure>` and still fails both checks; `scripts/check_all.sh` looks for
`<failure` and the test count and is unaffected. Rejected: pinning cocotb
1.x (the harness uses the cocotb 2 API); leaving `test` and `gl_test` red.

## D-028 2026-10-03 Ahan: D-026 resolved: harden `sram-macro` now; the slow-corner miss is tracked, not blocking

Tiny Tapeout's cmos5l flow signs off timing at the typical corner only:
`TIMING_VIOLATION_CORNERS` is `*typ*` and `IHPTech.tt_corner` is
`nom_typ_1p20V_25C` on the `ihp-sg13cmos5l` branch of tt-support-tools; the
slow and fast corners are reported but never fail the flow. Source: the
parallel entry's `docs/tt_cmos5l_facts.md` (sections 1 and 12, read from the
tool source), and it agrees with our own run 37073185698, whose `gds` job
succeeded with -0.59 ns at the slow corner. So the slow corner is logged in
docs/AREA.md for every run and tracked, but it does not block a merge.

Acceptance for merging `sram-macro` into master: typical setup slack of at
least +5 ns and every sign-off check passing (DRC, LVS, antenna, precheck,
gl_test). If the slow corner still fails on the macro run, the
`decode-onehot` branch (registered one-hot thread select replicated per
consumer: register file, timers, PC/flags, pins; no change to the cycle
contract) is hardened next and both runs are reported. `CLOCK_PERIOD` stays
20 ns. Not taken: relaxing the clock; a pipeline register; flow keys beyond
the two CLAUDE.md allows.

## D-029 2026-10-04 Claude, applying Ahan's rule of D-028: `sram-macro` is merged into master; the SRAM macro is the program memory

Run 37169889955 on `sram-macro` met the acceptance rule: typical setup slack
+6.40 ns (at least +5 ns required); hold clean at all corners; routing DRC,
LVS and antenna 0; the precheck's nine checks pass, including the KLayout
SG13CMOS5L sign-off DRC over the merged GDS with the macro; `gl_test` passes
on the gate-level netlist with the vendored macro model. The two flow
waivers were checked against the run: all 29,294 Magic DRC boxes lie inside
the macro's bounding box, and the 10 illegal overlaps are the four POWER
stripes over the macro's VDD!/VDDARRAY! split band. This supersedes the
"not merged" of D-025: `src/config.json` on master now carries the MACROS,
PDN and Magic keys, the macro is the default in `src/keyer_imem.v`, and the
flop memory remains as the `KEYER_IMEM_FLOPS` fallback (FPGA, area
comparison). The slow corner (-1.94 ns, 277 endpoints, from the macro's
5.4 ns slow-corner access time) is tracked, not blocking (D-028). Because it
still failed, `decode-onehot` was hardened as well (run 37176010222: typical
+7.41 ns, slow -0.13 ns on one endpoint); merging that branch is left to
Ahan.

## D-030 2026-10-04 Ahan: `decode-onehot` is merged into master

The registered one-hot thread select (four copies, one per consumer group,
D-028) becomes the master design. Why: its core is proven equivalent to the
`sram-macro` core (776 of 776 points), the full suite is green on it, and
its hardening run 37176010222 is better at every corner (setup +11.85 /
+7.41 / -0.13 ns against +11.20 / +6.40 / -1.94 ns), takes the slow-corner
miss from 277 endpoints to one, and passed all four jobs including the
precheck. It meets the acceptance rule of D-028 (typical slack at least
+5 ns, every sign-off check passing). The remaining slow-corner endpoint
(`deadline[0][15]`, the `WAITD` update) is worked on next on its own branch.
Rejected: staying on the `sram-macro` core (1.0 ns less typical slack and
277 slow-corner endpoints for 7 flops fewer).

## D-031 2026-10-04 Ahan: mutation testing rules; the full campaign runs on GitHub, by hand

`tools/mutate.py` decides a kill only from a cocotb `results.xml` that
reports a failed test, or from a proof that reports a counterexample. A
mutant that does not compile, times out, leaves no results file or dies is an
`error`, reported separately and never counted as killed. Checks run
fastest first and stop at the first kill; each result is appended to a JSONL
file as it completes and `--resume` continues a stopped run; `--sample N
--seed S` picks a seeded subset. The full campaign (about 1,500 mutants) is
not run on the development machine: `.github/workflows/mutation.yaml`,
started by `workflow_dispatch` only, runs it in 16 shards and merges the
results. Every survivor is either a test gap (a test is written that kills
it) or listed in `tools/mutate_equivalents.md` with a reason. Added by
Claude within these rules: a k-induction proof that merely stops closing
(sby `UNKNOWN`, no counterexample from reset) is not a kill, the remaining
checks decide; a mutant no check fails is called equivalent without a
manual entry only when Yosys proves every output and register input equal
to the original's. Why: the first version judged from process exit and log
text and ran everything locally, which made a killed simulator look like a
killed mutant and took the machine for hours. Rejected: counting lint
warnings or timeouts as kills; a full local campaign.

## D-032 2026-10-04 Claude (within Ahan's instruction for the FPGA build): the behavioural memory holds its read data in a write cycle; Alhambra II pin map

`KEYER_IMEM_FLOPS` path of `src/keyer_imem.v`: `rdata` no longer loads in a
write cycle (`if (we) ... else rdata_q <= mem[addr]`), exactly as the macro
path behaves (`A_REN = ~we`). Why: reading and writing one address in the
same cycle forced Yosys to wrap the iCE40 block RAM in 42 flops and 29 LUTs
of read-during-write emulation; gated, the memory is one SB_RAM40_4K and one
LUT. The read data of a write cycle is never used (SEMANTICS 2.1: the fetch
after any memory access is invalid; the host and the replay engine take
read data only after a read request), so SEMANTICS is unchanged and the
lockstep suite passes on this path. Board choices (`fpga/alhambra2/`): the
board's 12 MHz oscillator is the core clock, no PLL; host SPI on the
Arduino SPI positions (D13 SCK, D11 MOSI, D12 MISO, D10 CS_n); `uio` on
D0-D7; reset is a power-on counter plus DD5 (pulled up, low resets), not a
push button, because the buttons' polarity is unverified; IRQ on an LED
only; `ui[7]`/`uo[7]` on the USB serial port. Rejected: a separate FPGA
memory module (two descriptions of one memory); a PLL to 48 MHz (not needed
for bring-up; the build reaches about 40 MHz).

## D-033 2026-10-04 OPEN: SEMANTICS does not say what MISO does while CS_n is high

Mutation testing left a survivor that is not equivalent: the host's `miso`
register resetting to 1 instead of 0 while no transaction is active
(`host-de4cf058`). The RTL drives MISO (`uo[0]`) low whenever CS_n is high;
SEMANTICS 10.1 and 3.1 do not state it, and no test looked. Proposal for
Ahan: add to SEMANTICS 10.1 "MISO is 0 while CS_n is high (as seen through
the synchroniser) and after reset". Why: it is what the RTL does, it is
visible at a pad, and a defined idle level lets a board share or probe the
line. `test_miso_is_low_outside_a_transaction` (test/test_corners.py)
already checks the current behaviour and kills the mutant; nothing in the
RTL or the model changes either way. Alternative: declare the idle level
undefined and list the mutant as equivalent (then the test goes).

## D-033 (resolved) 2026-10-04 Ahan: MISO is 0 while CS_n is high

The proposal of the OPEN entry above is taken: SEMANTICS 10.1 now states
that MISO is 0 while CS_n is high (as seen through the synchroniser) and
after reset, and `test_miso_is_low_outside_a_transaction` stays as the test
of that sentence. Nothing changes in the RTL or the model (the model does
not drive the host pads). Rejected: declaring the idle level undefined and
listing mutant `host-de4cf058` as equivalent.

## D-034 2026-10-04 Ahan: `waitd-csa` is merged into master

The carry-save `WAITD` completion test becomes the master design. Why: its
core is proven equivalent to the previous master core (533 of 533 points,
`formal/equiv_core.sh`), the full suite is green on it, and its hardening
run 37247680638 meets setup at all three corners (+12.33 / +8.12 / +0.76 ns)
for 184 cells more, which removes the last slow-corner endpoint tracked
since D-026. It also brings formal properties T9 and P8 and check scripts
that fail on a failed proof (BUGS 19). Rejected: staying on the one-hot
master (-0.13 ns at the slow corner on `deadline[0][15]`).

## D-035 2026-10-04 Ahan: no hardware bring-up in this project

No board bring-up is planned. The Alhambra II build (`fpga/alhambra2/`) is
kept as a synthesis and post-synthesis-simulation result only: it shows the
design maps to an FPGA with the behavioural memory and that the pads-only
tests pass on that netlist; it has not been programmed into a board and
the documentation says so. The bring-up task is removed from HANDOFF.
Consequence for verification: no firmware runs against a real device, so
every protocol claim rests on the protocol models (docs/VERIFICATION.md).
Rejected: keeping bring-up as an open task nobody will do.

## D-036 2026-10-04 Claude (within Ahan's instruction for the serializer step): one shared serializer engine, clocked by the owner's timer; ISA version 3

SEMANTICS was silent on bit-level hardware; section 15 (version 0.4) and
`docs/SERIALIZER.md` now define it. The choices:

- **One engine shared by both threads**, half duplex: about 130 flops,
  estimated 12,000 to 13,000 um^2 (utilisation about 26.5%; two engines
  about 28.5%, so area would allow either). Shared because each engine adds
  a source to the register write-data selection, the path with +0.76 ns at
  the slow corner, and because neither USB nor 10BASE-T transmit uses two
  coded lines. Rejected: per-thread engines; full duplex.
- **The symbol period is the owning thread's timer period** (`SERCFG`
  names the owner): the transmitter advances on that timer's ticks, the
  receiver's counter reloads from the same `period`. Rejected: a private
  divider register.
- **Clocks: 48 MHz for USB low speed** (`period` 32) **and 40 MHz for
  10BASE-T** (`period` 2 per half-bit). Manchester half-bits are 2.5 cycles
  at 50 MHz and USB bits 33.3; 60 MHz divides both but is not signed off
  (20 ns stays the constraint, clean at the slow corner). Both clocks are
  below 50 MHz, so the hardened design covers them; the demo board's clock
  is programmable.
- **Encoding** (`tools/keyer_isa.py`, ISA version 3, the ID register's
  second byte): XFER sub-opcode 15 with a 4-bit function field in the
  formerly unused low bits (`SERCFG SERTX SERTXC SERRX SERST SERWT`, timeout
  forms through the existing T bit), and MISC sub-opcodes 10 and 11
  (`SERI n`, `SERIC n`: a byte from the instruction word, so constant
  tables cost one word per byte in a 256-word memory).
- **A frame ends by underrun** (holding register empty at a byte boundary);
  the engine appends the CRC of the marked bytes and the end of packet.
  **CRC-5 is receive-only.** The engine writes the pin registers through
  the pin unit after the replay engine and before a host PINMODE write, and
  respects open-drain mode.
- No host register: the engine is firmware's; the lockstep harness reads
  its registers by name (SEMANTICS 13).

## D-037 2026-10-05 Claude, applying Ahan's rule for step 4: `serializer` is merged into master

Ahan's condition was: merge if timing is clean at all corners and
utilisation is under 40%. Run 37266431182 on the branch: setup +12.53 /
+8.41 / +1.37 ns (fast / typical / slow), no violating endpoint, hold clean,
utilisation 27.0%, routing DRC, LVS and antenna 0, precheck 9 of 9,
`gl_test` passing. The branch's full check suite is green (406 pytest, 52
cocotb with every serializer register compared in lockstep, the unit bench,
five formal groups). Master now carries the serializer engine (D-036), ISA
version 3, `fw/usb_ls_device.s` and `fw/eth_10bt_tx.s`. The estimate of
D-036 was low: the engine costs 20,113 um^2 in layout, not 12,000 to
13,000. Rejected: nothing; the rule was met.

## D-038 2026-10-05 OPEN: four things SEMANTICS does not say about the host interface (nine surviving mutants)

The full mutation campaign left nine mutants that change behaviour at a pad
or in a host read which SEMANTICS does not define. No test was written for
them, because a test would invent the rule. For Ahan, one at a time, with
the recommendation first:

1. **MISO during the command byte and during the data bytes of a write**
   (`host-931f4e64`, `host-2b5da9fb`, `host-160d459e`, `host-4bbc0a41`).
   The RTL drives 0. Recommended: add to 10.1 "MISO is 0 except while a
   read data byte is shifted out"; one short pads-only test then kills all
   four. Alternative: leave it undefined and list the four as equivalent.
2. **A transaction in progress when reset is released** (`host-6c196bbf`:
   the input synchronisers not reset; it also decides the documented
   equivalent `host-c6659575`). Recommended: add to 10.1 the constraint
   "CS_n is high from two cycles before `rst_n` rises"; both are then
   equivalent, with that reason. Alternative: define the behaviour (the
   interface ignores everything until CS_n has been seen high) and test it.
3. **The fourth byte of a PINS read** (`host-b6056c12`). 10.4 says reads
   past the listed bytes follow the pattern modulo its period, but PINS has
   three bytes and the index wraps at four; the RTL returns 0. Recommended:
   say so in 10.4 ("PINS: a fourth byte of 0, then the pattern repeats")
   and extend the read-back test.
4. **IMEM_DATA read while a thread runs, before any read with both
   threads stopped** (`host-07c66559`, `host-5fe456e3`, `host-38125bb3`).
   10.4 says "stale data"; after reset the RTL returns 0. Recommended:
   keep "stale" as unspecified and list the three as equivalent (a host
   must not read the memory while a thread runs anyway). Alternative: state
   that the read-data register resets to 0 and test it.

Also for Ahan, raised by this session's agents and not decided here: the
lockstep harness compares the executing word against the model's memory
after the step, which is wrong in the cycle the capture engine overwrites
that word (BUGS 45; `test_mut_core.py` works around it in a subclass
because the agent's edit of `test/keyer_tb.py` was refused by the
permission system and the coordinating session did not make it on the
agent's behalf); and three serializer points (spec-questions R24, R25 and
the J precondition of 15.3): an unread byte or frame end is dropped at the
next frame start without a flag, a transmitter start abandons a frame being
received without a flag, and a frame after an aborted one may start from K.

## D-038 (resolved) 2026-10-05 Ahan: the four host-interface questions

1. **MISO is 0 at all times except while a read data byte is being shifted
   out** (SEMANTICS 10.1): after reset, while CS_n is high, during every
   command byte and during the whole of a write. Rejected: leaving the
   level undefined and listing the four mutants as equivalent.
2. **No transaction is in progress when reset is released**: CS_n must be
   high from two cycles before `rst_n` rises (SEMANTICS 10.1), and
   `tools/keyerhost.py` enforces it in every transport. Rejected: defining
   the behaviour of a transaction that straddles the release.
3. **The fourth byte of a PINS read is 0**, then the pattern repeats
   (SEMANTICS 10.4).
4. **IMEM_DATA read while a thread runs stays unspecified**; the three
   mutants are listed as equivalent. Rejected: stating that the read-data
   register resets to 0.

Also decided: the lockstep harness is fixed (BUGS 45) and the workaround
leaves `test/test_mut_core.py`. The three serializer points are D-039.

## D-039 2026-10-05 Ahan: serializer: a sticky overrun bit, abort returns the pair to idle, a transmitter start abandons a receive

Ahan's decisions, and the cycle-exact form Claude gave them in SEMANTICS
0.5 section 15:

- **A sticky receive-overrun bit in the status word.** New register
  `rx_drop`, status bit 10. Set when a frame start discards a byte or a
  frame end of an earlier frame that firmware has not taken (a `SERRX`
  completing in that very cycle takes its item, which is then not
  dropped); cleared when the status word is read (`SERST`, or `SERRX` /
  `SERRXT` returning the status word) and by `SERCFG`; if set and read
  coincide the set wins, so no event is lost. It replaces "discarded
  without a flag" (spec-questions R24). `rx_ovr` keeps its meaning (a byte
  lost inside a frame, cleared by the next frame start).
- **Abort returns the line to idle within one bit period, and every frame
  starts from idle.** A `SERCFG` that commits while the transmitter is not
  IDLE is an abort and writes the idle state of the old configuration's
  pair in that cycle: NRZI, both pins released with J in the output
  registers; Manchester, both pins driven low. That is exactly what each
  mode's tail leaves, so the coder's assumption (a frame is coded from J)
  now holds after reset, after a tail and after an abort. Why released
  rather than J driven in NRZI mode: a USB device aborts when the host
  takes the bus (a reset is SE0 driven by the host), and a driven J would
  fight it; the pull-up restores J. Rejected: the start tick driving J for
  one symbol (changes every frame on the wire and the 10BASE-T preamble
  timing); the abort running the whole tail (an aborted frame would end in
  a valid end of packet, and it takes four symbol periods).
- **A receive in progress is abandoned when the transmitter starts**, with
  no `rx_end`, verdict or flag (kept from R25; SEMANTICS 15.4 says so).

The encoding does not change, so the ISA version stays 3. The model and
the RTL are updated independently from SEMANTICS (subagents).


## D-040 2026-10-05 OPEN: moving the program memory to 512 words (not implemented; for Ahan)

The question left open since D-004. Nothing below is implemented.

**Why one would.** `fw/usb_ls_device.s` uses 253 of 256 words; the next
largest program uses 117. A capture buffer lives in program memory (120
entries next to a small program), and two threads share the 256 words.
512 words would let the USB device grow (string descriptors, a HID report
endpoint), or run a second protocol or a capture beside it.

**What changes in the ISA** (it would be ISA version 4, SEMANTICS 0.6):

- `PC[t]` and `LR[t]` grow from 8 to 9 bits; "every address is taken mod
  256" (2.4) becomes mod 512. The reset vector stays 0 for both threads.
- `JMP` and `CALL` keep their encoding: the field is already 11 bits and
  the machine would use the low 9 instead of the low 8. Relative branches
  (`Bcc` +-128, `BP0/BP1` +-32, `DJNZ` +-16) are unchanged.
- `JMPR` and `RET` take 9 bits of the register or `LR`; `RDLR` returns 9.
- `LDI rd, label` no longer reaches every label: a label at or above 256
  needs `LDW` (two words). `usb_ls_device.s` loads handler addresses with
  `LDI`; they would stay in the low half or cost a word each.
- Capture and replay: `cap_base` and `rep_base` are 8 bits and the write
  and fetch addresses wrap mod 256. Either the buffers stay in the low 256
  words (no change), or base gets a ninth bit from the unused bit 3 of
  CAP_CFG byte 0 and of REP_CFG.

**Host register map.** PC0, PC1 and IMEM_ADDR are already two-byte
registers whose second byte is ignored on write and reads 0: bit 0 of that
byte becomes address bit 8, so existing host code that writes 0 there keeps
working. The IMEM_DATA auto-increment becomes 9 bits. `tools/keyerhost.py`
(`load_program`, `read_program`), the assembler's image size, the golden
model's memory, the lockstep harness's address masks and the 256-word load
of the tests all change; the ID register reads version 4.

**config.json and PDN.** `MACROS` names the other macro
(`RM_IHPSG13_1P_512x16_c2_bm_bist`: gds, lef, three libs, blackbox,
spice), `PDN_MACRO_CONNECTIONS` keeps its two lines. The macro has the same
width (236.8 um) and the same Metal4 power columns as ours, which is why
`src/pdn_cfg.tcl` and the `FP_PDN_V*` keys were taken from the sibling
entry's 512 x 16 recipe in the first place: they stay as they are, the
stripe verifier in `pdn_cfg.tcl` checks it at run time. Placement
`[12, 40]` still fits: the macro is 191.34 um tall instead of 118.78 (die
710.64). These edits to `config.json` are outside the two keys CLAUDE.md
allows, so they would ride on this decision.

**The macro, from the PDK** (IHP-Open-PDK `dev` at bf079026, the commit our
macro was vendored from; `.lib` and `.lef` of both):

| | 256 x 16 (now) | 512 x 16 | difference |
|---|---|---|---|
| Size | 236.8 x 118.78 um | 236.8 x 191.34 um | +72.56 um height |
| Area | 28,127 um^2 | 45,309 um^2 | +17,182 um^2 |
| Clock to data out, slow 1.08 V 125 C | 4.99 to 5.13 ns | 6.25 to 6.39 ns | **+1.26 ns** |
| Clock to data out, typical | 2.98 to 3.06 ns | 3.73 to 3.82 ns | +0.76 ns |
| Clock to data out, fast | 1.83 to 1.89 ns | 2.29 to 2.35 ns | +0.46 ns |
| Address setup, slow / typ / fast | 0.74 / 0.42 / 0.24 ns | the same | 0 |

Utilisation would go from 27.0% to about 28.9% (17,182 um^2 of a
902,417 um^2 core, plus a handful of flops): area is not the question.

**Timing.** In master run 37286790227 the twelve worst slow-corner paths
all start at the macro's data output (instruction word into decode; the
worst has 5.29 ns in the macro and ends with +1.37 ns). The 512-word
macro adds 1.26 ns to every one of them before anything else changes:
expected slow-corner slack about **+0.1 ns**, less whatever the ninth
address bit, the taller macro's longer routes and D-039's logic cost, and
runs of the same RTL have differed by more than that (+0.76 to +1.37 ns
across the last three designs). Typical would go from +8.41 to about
+7.65 ns, fast from +12.53 to about +12.1 ns. So the design would still
sign off at the typical corner (D-028), but "clean at all corners", the
merge rule since D-037 and the reason for the carry-save `WAITD` (D-034),
would probably be lost or be a matter of placement luck.

**Verification cost.** A SEMANTICS change on both independent sides, the
host tests, the formal core proof (PC width), the FPGA build (a second
block RAM), a full mutation campaign and a hardening run: about what the
serializer step cost, without a new feature to show for it.

**Recommendation: no.** It spends essentially the whole slow-corner margin
(1.26 of 1.37 ns), on the one path every worst endpoint shares, to buy
memory that a single program is close to needing and none needs today. The
case changes if a firmware goal that does not fit 256 words is put on the
plan (a USB HID device with strings is the likely one) and Ahan accepts
either typical-only sign-off or a slower clock for it; the entry above is
then the work list, and a branch hardening run before any merge is the
first step. Alternatives if only a little room is wanted: shorter
descriptor tables in `usb_ls_device.s`, or the capture buffer's ninth bit
without the larger memory (no gain).

## D-041 2026-10-05 Claude (within Ahan's instruction for the slew step: a config.json change is allowed with a DECISIONS entry): `DESIGN_REPAIR_MAX_SLEW_PCT` 20 -> 50

The 13 max-slew entries of runs 37266431182 and 37286790227 are 13 pins on
two nets at the slow corner only, both in our logic (docs/AREA.md, run
37286790227): the `SERCFG` commit strobe from the core's decode to the
serializer (7 loads, 3.62 ns against a limit of 2.51) and inbox 1's
pointer comparison (4 loads, 2.57 ns). They are real violations of the
library's `max_transition` at that corner; the flow reports them and does
not fail, and the sign-off corner (typical, D-028) has none.

The fix is one key in `src/config.json`: `DESIGN_REPAIR_MAX_SLEW_PCT`
from its default 20 to 50. Why this and not the RTL, which Ahan prefers:

- Fanout is not the cause. The nets have 7 and 4 loads. Each is driven by
  a `nor4_1`, the weakest four-input gate, over a long route, and the flow
  repairs slew once, after global placement, at the typical corner, to 80%
  of the limit. A net it leaves at 2.0 ns there is at about 3.6 ns at the
  slow corner (the cells are 1.7 to 1.8 times slower), which is exactly
  the worst value seen. Earlier runs of other designs had 33, 14 and 0
  such pins: which nets land above the limit is placement luck.
- Duplicating a combinational net in RTL does not survive synthesis (Yosys
  merges identical cells and ABC restructures the cone), and the next
  placement would put other nets over the limit. Registering the strobe
  would change SEMANTICS 15.2.
- A margin of 50% holds every net to about 1.25 ns at the typical corner
  and about 2.2 ns at the slow one, under the limit with room for the
  difference between estimated and routed wires. Cost: more repair
  buffers; the run says how many.

This goes beyond the two keys CLAUDE.md lets a session edit
(`CLOCK_PERIOD`, `PL_TARGET_DENSITY_PCT`); it rests on Ahan's instruction
of 2026-10-05 for this step. Rejected: `MAX_TRANSITION_CONSTRAINT` (it
would also lower the limit the slow-corner check reports against, turning
compliant nets into reported ones); `RSZ_CORNERS` set to the slow corner
(it would also move hold repair away from the fast corner, where hold is
tightest: +0.075 ns); leaving it (the slow-corner timing of two paths
stays extrapolated). Confirmed or not by the branch run of `d039`
(docs/AREA.md).


## D-042 2026-10-05 Claude, applying Ahan's rule for step 6: `d039` is merged into master

The rule: merge if timing is clean at all corners and utilisation is under
40%. Run 37339746749 on the branch: setup +12.39 / +8.25 / +0.99 ns (fast /
typical / slow), no violating endpoint, hold clean, utilisation 27.1%,
routing DRC, LVS and antenna 0, no max-slew entry, precheck 9 of 9,
`gl_test` passing; the full check suite is green on the branch and the
mutation campaign on the same commit has no survivor. Master now carries
D-039 (model and RTL) and the slew margin of D-041. Not clean in the
reports and left for Ahan: one max-cap entry on the macro's `A_DOUT[7]`
(docs/AREA.md). Rejected: nothing; the rule was met.
