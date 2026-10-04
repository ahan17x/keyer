# Keyer work log

(The project was called Loom until 2026-10-02, DECISIONS D-017; entries before that date use the old names.)

Newest entries at the bottom. Times are US Eastern.

## 2026-10-01

- 00:30 Cloned the CMOS5L Tiny Tapeout template (`cmos5l` branch). The GDS action uses `pdk: ihp-sg13cmos5l`; default clock period 20 ns; tiles set in `info.yaml`. Note: the template's info.yaml comment still lists sky130 tile sizes; ignore it, Jane Street says 6x4.
- 00:35 Sparse-cloned the IHP open PDK `dev` branch. CMOS5L has an 84-cell standard library. Key areas: DFF w/ reset 49.0 um^2, mux2 18.1, NAND2 7.3, latch 30.8.
- 00:40 Found that `sg13cmos5l_sram` is a symlink to the SG13G2 SRAM macros. `RM_IHPSG13_1P_256x16` is 28,127 um^2 (< 1 tile). Flip-flop IMEM of the same size would be ~275,000 um^2. Decision: SRAM macro for program memory, flop model for simulation.
- 00:45 Wrote PLAN.md (architecture decisions, scope, milestones, risks, what needs Ahan).
- 01:10 Wrote `docs/isa.md` (ISA v0.1 draft): 2 barrel threads, 16-bit regs, 76 instructions, 24-pin space with open-drain mode, per-thread sticky-tick timer, inbox/outbox FIFOs, SPI host register map. Changed the draft so the register field is always at bits [11:9] (simpler decoder).
- 01:25 `tools/loom_isa.py`: encoding table, encode/decode/disasm, generates `src/loom_isa.vh`. Self-check: all 76 instructions round-trip, no opcode collisions.
- 01:40 `tools/loomasm.py`: two-pass assembler (labels, expressions, .org/.word/.equ, pin names uio0/ui0/uo0, LDW pseudo-op, range checks). Assembled a UART-style program correctly.
- 02:05 `tools/loomsim.py`: cycle-exact ISS. Models the 2-flop input synchroniser, edge detection, timer, DELAY, FIFOs, open-drain pins, both threads.
- 02:20 `tools/test_iss.py`: 18 tests, all passing. Three of my own expected values were wrong at first (hand arithmetic), the simulator was right each time. One finding worth keeping: a late loop iteration completes its WAITT immediately on the sticky tick and the next on-time WAITT is back on the tick grid, so phase is preserved. Test now asserts exactly that.
- 02:50 Firmware for all three required protocols runs on the ISS against independent protocol models (`tools/protomodels.py`): UART decoder with bit-timing checks, UART stimulus with baud error, SPI mode-0 slave, I2C EEPROM-style slave with clock stretching. 8 firmware tests pass; 27 tests total.
- ISA changes forced by writing real firmware (all in `docs/isa.md`):
  - `RDLR rd` / `JMPR rs`: nested subroutines need to save the link register (I2C byte -> bit routines).
  - `DJNZ rd, off`: decrement-and-loop without touching flags. `DEC` writes C and silently clobbered the data bit waiting for `WRC` — a classic bit-bang hazard, now documented. DJNZ also saves one slot per bit in every loop.
- Firmware findings worth keeping for the datasheet:
  - Push-pull outputs are 0 after reset, so active-low selects should use uio pins with pull-ups (tri-stated at reset).
  - For minimum-duration phases (I2C) use `SETT`+`WAITT` per phase; for phase-locked bit streams (UART/SPI) use one `SETT` per byte and `WAITT` per bit. Sticky ticks preserve phase but not minimum width after a late iteration (seen on SPI: a 20-cycle period after a byte boundary until the per-byte resync was added).
  - RX sampling needs a calibrated constant (`1.5 * period - 8`) to centre the sample; verified to land within 4 cycles of mid-bit.
  - The host must respect inbox levels: 16-deep, writes drop when full. Tests use a feeder model now; the demo-board driver will do the same.
- Program sizes: UART 37 words, SPI 31, I2C 110 (of 256).
- Committed to a local git repo (`/home/claude/loom`, commit 8616b33).
- 03:50 RTL written, Verilog-2005: `loom_fifo.v`, `loom_imem.v` (behavioural + `LOOM_IMEM_SRAM` wrapper for `RM_IHPSG13_1P_256x16`), `loom_pins.v`, `loom_core.v` (barrel core, timers, decode/execute), `loom_host.v` (SPI slave + register map), `tt_um_ahan17x_loom.v`. Icarus compiles; Verilator `-Wall` lint clean.
- 04:05 First Yosys synthesis against the CMOS5L typical liberty:
  - Flop-based program memory: 18,956 cells, 429,092 um^2. That is the entire usable budget; 4,096 of the 5,302 flops and most of the 3,932 mux2 are the memory. Confirms the SRAM-macro decision.
  - SRAM macro black-boxed: 6,410 cells, 1,190 flops, 114,169 um^2 of standard cells, plus the 28,127 um^2 macro. About a third of the usable area.
  - Scripts in `synth/`. No timing numbers yet (needs the LibreLane flow).
- 04:40 cocotb testbench: `test/tb.v` models the bidirectional pads (uio_in follows the DUT's drive when enabled, else the test's `uio_ext`). `test/loom_tb.py`: pad driver, SPI master, and the `Lockstep` harness: from reset, every cycle it feeds the RTL's exact inputs to the ISS, compares pin drive registers, PCs, timers, running state, the executing instruction and whether it committed, and the register file after each commit; host effects seen at the RTL host interface (IMEM writes, run/stop, PC writes, FIFO pushes/pops, pin mode, FIFO clear, soft reset) are mirrored into the ISS the same cycle.
- 04:50 Host interface tests pass at SCK = clk/8, clk/16, clk/32: ID, STAT, PINMODE, LEVELS, PINS, IMEM write + read-back, FIFO round trip through a running thread.
- 05:10 Lockstep found one real bug, in the ISS not the RTL: the simulator's synchroniser gave 1 cycle of input latency instead of 2. UART/SPI tests were parity-insensitive and passed anyway; the I2C clock-stretch wait polls every slot and caught it. Fixed, and added a test that checks the latency on both thread parities.
- 05:20 All 9 cocotb tests pass: 3 host-interface, 6 lockstep (ALU/branches, pins/timers/delays with two threads, UART loopback T0->T1, SPI master vs slave model, I2C with repeated start + clock stretching + SPI-fed commands, constrained-random programs on both threads with random pin noise). ~5.5 million simulated ns, 53 s.
- Filled `info.yaml` (6x4, 50 MHz, pinout), `docs/info.md`, README. Committed (ad24a2d). Delivered `loom.zip` (repo without .git).
- 05:50 Formal (SymbiYosys via `yowasp-sby`, z3, abc), see `formal/README.md`:
  - `loom_fifo`: occupancy bookkeeping fully proved by k-induction; FIFO data order checked by BMC to depth 20 (DEPTH=4 instance; RTL is parameter-generic). z3 ran out of memory on the 16-deep instance past depth ~20; the small instance covers the same logic.
  - `loom_pins`: fully proved: open-drain pins are never driven high under any command/host sequence; uo[1:0] fixed; synchroniser latency exactly 2 cycles; edge history exactly 2 cycles.
  - `loom_core`: 7 properties (tick never lost, disabled timer never ticks, PC only moves on commit/host write, threads start only via host or START, halted implies stopped, thread parity, blocked threads hold PC) proved by abc PDR in 0.1 s. z3 OOMs on the core; yowasp-sby's parser crashes on the abc engine, so `formal/run_core_pdr.sh` runs abc directly.
- Committed 090f90e. Delivered updated `loom.zip`.

## Status at end of session 1

Done: plan, ISA v0.1 (79 instructions), assembler, cycle-exact ISS (28 Python tests), UART/SPI/I2C firmware verified against protocol models, complete RTL (lint clean), SPI host interface, 9 cocotb tests including cycle-by-cycle lockstep from reset, formal proofs on FIFO/pins/core, Yosys area estimate (114k um^2 logic + 28k um^2 SRAM macro of ~430k usable), info.yaml at 6x4, datasheet draft, README.

Needs Ahan next:
1. Create the public GitHub repo (from the zip; `git init` is already done, just add the remote and push) and enable Actions + Pages. The `gds` workflow is the first real test of the flow at 6x4; the `test` workflow runs the cocotb suite.
2. Decide: project name, 2 vs 4 threads, 256 vs 512 words, SPI host interface (all fine to keep).
3. Sign-up form; teammates.

Next for me: SRAM macro flow config (copy from Tiny Tapeout's `ttihp-sram-test`), the demo-board Python driver, timing report once the action runs, then stretch features (CRC engine, capture buffer, serializer, USB LS).

## 2026-10-02

- Reviewed Thomas Gilbert's `tt_um_loom` (friend's entry, same model lineage, started 2026-09-14, hardware declared complete). Key differences: 4 threads / 4-stage pipeline vs our 2 / 2-stage; deadline-register timing with fractional ticks and timeouts vs our sticky tick; per-thread bit engine (CRC, NRZI/Manchester, stuffing) built; 512x16 SRAM in the flow and precheck-clean; LD/ST into imem; debug port; 15 firmware programs incl. USB LS; mutation testing, thread-isolation miter, static deadline checker. 31k cells at 54.7% util, 4-5 h hardening runs; ours 6.4k cells.
- Facts corrected from their research: 6x4 block = 1289.28 x 710.64 um = 916,214 um^2 (core 902,417), routing on Metal1-4 only; organisers confirmed (email, 2026-09-28) SRAM macros are allowed and a TT reference macro template is coming; 8x4 exists in the tools since 2026-09-21 but organisers say design to 6x4.
- Open decisions for Ahan: rename (two "Loom" entries); wait timeouts; 2 vs 4 threads; deadline-style timer; capture/replay feature as our differentiator; whether to team up.
- Process changes to adopt regardless: decision log, bug ledger, independent re-derivation of model or RTL from the spec, mutation testing.
- Handoff to Claude Code prepared: `CLAUDE.md` (read order, rules, commands, compact instructions), `docs/HANDOFF.md`, `docs/DECISIONS.md` (D-001 to D-016, four OPEN), `docs/BUGS.md` (10 rows), `docs/SETUP.md`, `docs/KICKOFF_PROMPT.md`, `.claude/agents/golden-model.md` and `rtl.md` (deny rules so neither can read the other's code), `scripts/setup_mac.sh`, `scripts/check_all.sh`, `.github/workflows/lint.yaml`.
- Bug 10: the FIFO data-integrity BMC had been passing vacuously (the `-DDATA_CHECK` line was lost from the sby file). Fixed; real BMC to depth 20 passes in ~90 s; check_all now verifies the define reached the model.
- Full check suite green at hand-off: 28 Python tests, 9 cocotb tests, lint, 3 formal groups.

## 2026-10-02, Claude Code session 1 (Keyer)

- Environment on the Mac (BUGS 11, docs/SETUP.md section 3): the venv had
  been created at `~/Claude/loom` and the repo moved, so every launcher
  failed with "bad interpreter"; macOS has no `timeout`; `formal/fifo_props.sv`
  had been deleted by commit 0b2d0af, so the FIFO proof had not run since.
  Rebuilt the venv on Homebrew Python 3.13, made the scripts detect a moved
  venv, restored the property file, untracked two generated files. Full check
  suite green on this machine (commit 37f9213).
- Decisions D-017 to D-021 taken by Ahan: name Keyer; NOW/DEADLINE timer with
  timeouts (proposal B); two threads with an NTHREADS parameter; capture and
  replay plus staying small as the differentiator, Hardcaml out; keep the core.
- Renamed Loom to Keyer everywhere in one commit (a9dd6e3): modules, files,
  tools, tests, docs, info.yaml, top module `tt_um_ahan17x_keyer`, ID byte
  'K'. History and the review of the sibling entry keep the old name.
- Wrote `docs/SEMANTICS.md` v0.2, the cycle-exact contract (da573d9); isa.md
  aligned (85 instructions, ISA v0.2). Reading the RTL against the spec found
  BUGS 12 (soft reset did not clear FIFOs) and 13 (PC/IMEM_ADDR not readable).
- D-018 implemented (f713984) by two restricted subagents working in parallel
  from SEMANTICS alone: golden-model (no `src/`) and rtl (no model). The ISA
  table, header, firmware (`WAITT` -> `WAITD 1`, timing unchanged; I2C
  stretch timeout with status 0xFF) and tests were written by the
  coordinating session. The subagents raised four spec questions
  (`docs/spec-questions.md`); all resolved into SEMANTICS (timer ticks while
  stopped; soft reset wins a same-cycle commit; T bit ignored elsewhere;
  NTHREADS > 2 is not part of the contract).
- First lockstep run: 9 of 11 passed, model and RTL in agreement everywhere,
  but both I2C tests failed functionally. Traced to BUGS 14: the host's
  byte counter saturates at 255, so IMEM_DATA stopped writing after word 127
  and the 136-word I2C firmware ran off into zeros. The lockstep comparison
  cannot see a wrong program load; the functional checks did. Fixed in the
  host (separate low/high phase bit); `test_imem_write_read` now loads and
  reads back all 256 words.
- Formal: core properties T1-T8 for the timer (a reached deadline completes
  the wait in the same slot, exact completion rule, NOW/DEADLINE move only as
  specified, timeout forms set C = !base, timed-out waits have no side
  effects) plus P3-P7; abc pdr in about 2 s. The rtl subagent also ran 19
  mutations (all caught) and a Yosys equivalence check between the two-thread
  core before and after parameterisation.
- Area (generic Yosys cells, not the CMOS5L library): core 397 -> 459 flops
  for the timer and timeouts; NTHREADS = 4 about 7,200 cells vs 4,580.
- End of step 3: 33 Python tests, 11 cocotb tests, lint, Icarus, three
  formal groups green.
- Step 4, independent re-derivation (D-012): the golden-model subagent
  rewrote `tools/keyersim.py` from SEMANTICS.md alone (first action on the
  path was a full overwrite; it never saw the old model or `src/`). 978
  lines against the previous 582, a dispatch table with one handler per
  encoding-table entry. All 33 Python tests passed on its first run, and the
  lockstep suite passed 11 of 11 with zero cycles of disagreement against
  the RTL, so there is no mismatch to log. Its five spec questions (Q5-Q9:
  level2 at reset, reserved pin indices 24-31, RUN write against a
  same-cycle START/STOP/HALT, FIFO occupancy without bypass, MISO/IRQ in the
  pad view) were resolved into SEMANTICS. Q6 exposed BUGS 15: the RTL
  aliased pin writes 26-31 onto uo[2..7]; reserved in the spec, fixed in the
  pin unit, and the random lockstep test now draws pin indices from 0-31.
- End of session: everything green on `bash scripts/check_all.sh` (33
  Python tests, 11 cocotb tests, lint, Icarus, three formal groups). Commits
  this session: 37f9213 (environment), a9dd6e3 (rename), da573d9
  (SEMANTICS), f713984 (D-018/D-019, BUGS 12-14), f03f726 (model re-derived,
  BUGS 15), plus this docs commit. Nothing pushed. Next session: design
  capture-and-replay (docs/HANDOFF.md task 1).

## 2026-10-02, Claude Code session 2 (GitHub, area, capture and replay, macro branch)

- GitHub: public repo `ahan17x/keyer` created and pushed; Actions on, Pages
  set to the workflow build. The `gds` trigger block got a `paths` filter
  (`src/**`, `info.yaml`, `macro/**`, the workflow file) plus
  `workflow_dispatch` and a per-branch concurrency group (D-022); the first
  hardening was dispatched by hand and superseded the push-triggered run,
  as designed. The template's `test` workflow fails on every passing run
  because its `! grep failure results.xml` matches cocotb 2's
  `failures="0"` (D-023, OPEN for Ahan: one-token fix in a template job).
  The project's own `check` workflow (renamed from `lint`) now runs the full
  `scripts/check_all.sh`, formal included, and passes on GitHub.
- Synthesis after D-018 (docs/AREA.md): 121,796 um^2 of logic with the macro
  black-boxed (6,948 cells, 1,253 flops); core alone 55,691 um^2 at two
  threads, 94,265 at four.
- Capture and replay (D-020, D-024; docs/CAPTURE.md, SEMANTICS section 14).
  Of three sizings only a 32-entry flop buffer (23.0% of the core) and the
  in-memory option (18.8%) fit the 25% limit; the in-memory one holds 120
  entries beside the I2C firmware and costs 203 flops. Entries are
  `{delta[11:0], pins[3:0]}` on a 4-pin group; the engines use the memory
  port only in a stopped thread's fetch slots, so the firmware under test
  keeps its timing. Model and RTL were written in parallel by the two
  restricted subagents from the spec; they raised twenty spec questions,
  all resolved into SEMANTICS (the departing-entry rule for queue and
  prefetch, the saturated-counter underrun the RTL side found by proof).
  First lockstep run: 13 of 14, the one failure a test bug (a DISARM sent
  over SPI outside the lockstep loop). Formal: ten properties by k-induction
  plus six covers; 14 mutations all caught. The demo `fw/capture_demo.s`
  records thread 0's I2C transaction and replays it on the same pins; the
  slave model sees two identical transactions with identical edge timing,
  in lockstep with the model. Whole design with the engines: 142,277 um^2,
  18.9% of the core with the macro.
- Branch `sram-macro` (D-025, committed locally, not merged): the 256 x 16
  macro vendored from the PDK, blackbox stub, config.json MACROS/PDN/Magic
  keys and the pdngen wrapper from the sibling entry with attribution; the
  macro's power columns sit at the same x as the sibling's 512 x 16 so the
  stripe grid carries over. The cocotb suite now runs against the vendored
  macro model and passes 14 of 14; check_all green on the branch.
- `test/Makefile` finds `cocotb-config` in the venv, so a plain
  `cd test && make` works (the `$(shell ...)` ran before the PATH export).
- First hardening run (37073185698, commit d979e82, flop memory, before
  capture): `gds` 3 h 00 min, `precheck` all nine checks pass, routing DRC,
  Magic DRC, LVS and antenna all 0, utilisation 66.9% (37,821 cells), hold
  clean. **Setup fails at the slow corner: -0.59 ns** (typical +7.09, fast
  +11.46); critical path instruction register -> opcode decode -> ALU ->
  register-file write (`regs[15]`), details in docs/AREA.md. `CLOCK_PERIOD`
  untouched; D-026 OPEN lists the options (recommended: harden the
  `sram-macro` branch first). The branch is therefore prepared and
  committed locally but not pushed or dispatched: step 5 was conditional on
  step 4 passing.
- `gl_test` failed in that run because the template's `test/Makefile` omits
  `sg13cmos5l_udp.v` (the cell models instantiate `ihp_mux2` and other
  primitives from it). Added, plus a gate-level mode for the suite: the
  lockstep tests (which read RTL internals) skip under `GATES=yes`, the
  host-interface tests run, and two new pads-only tests
  (`test_pads_uart_loopback`, `test_pads_capture_replay_demo`) drive the
  protocol models from the pads alone, in both RTL and gate-level runs.
  RTL: 16 of 16. Gate level, locally against the hardened netlist with the
  PDK cell models: see the next entry for the result.
- Gate-level simulation run locally against the hardened netlist of the
  first run (the unpowered `tt_submission` netlist, PDK cell models with
  `sg13cmos5l_udp.v`, `GATES=yes`), 2 min 13 s: `test_id_and_registers`,
  `test_pc_readback_and_soft_reset`, `test_imem_write_read` (256-word load),
  `test_fifo_roundtrip_and_status` and `test_pads_uart_loopback` pass on
  the netlist; the two capture tests fail there only because that netlist
  predates the capture logic; nine lockstep tests skip. Two more fixes were
  needed for the compile: `test/tb.v` connected `VPWR`/`VGND` under
  `GL_TEST`, but the IHP submission netlist has no power ports (now under
  `USE_POWER_PINS`); and the action's `! grep failure results.xml` matches
  cocotb 2's `failures="0"`, so `test/Makefile` now drops that attribute on
  a passing run (D-027, which also makes the template `test` workflow report
  correctly and closes D-023 without editing a template job).
- Session ends with D-026 OPEN (slow-corner setup, -0.59 ns): no clock or
  RTL change made; master pushed (its push starts a hardening run of the
  design with capture and replay); `sram-macro` kept local.
