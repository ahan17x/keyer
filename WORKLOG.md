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

## 2026-10-03, Claude Code session 3 (macro hardening, one-hot decode, host driver, datasheet)

- D-026 resolved by Ahan (D-028): harden `sram-macro` now; Tiny Tapeout
  signs off timing at the typical corner only (`TIMING_VIOLATION_CORNERS`
  `*typ*`), so the slow-corner miss is tracked, not blocking. Branch pushed,
  `gds` dispatched on it (run 37169889955).
- The `check` workflow had failed on GitHub for the last master pushes: the
  runner's apt z3 4.8 could not finish the pin-unit induction after the
  replay port was added (killed after 4.5 minutes; one second with a current
  z3). The workflow now installs z3 from pip; `check` and `test` pass on the
  branch.
- `decode-onehot` (branch from `sram-macro`, rtl subagent in its own git
  worktree, commit 399a889, not pushed): four registered one-hot thread
  selects (register file, timers, PC/flags, pins/FIFO strobes), AND-OR reads,
  pre-decoded register write enables; no change to the cycle contract. Yosys
  equivalence with the `sram-macro` core: 776 of 776 points proven. New
  formal properties S1 (each copy one-hot and equal to the decode of `tid`)
  and D1; 71 asserts by PDR in 3 s. Full check suite green, also after
  merging the driver work in. Synthesis: 8,182 cells, 1,470 flops, 141,004
  um^2 (154 cells and 1,273 um^2 fewer, 7 flops more); the register-file
  path loses two logic levels and no longer starts at the thread decode.
  The subagent also noted two faults the existing core properties would
  miss but the equivalence check catches (a blocked timeout-form wait
  writing C; START/STOP hitting its own thread): properties to add.
- Host driver `tools/keyerhost.py`: one `KeyerHost` API over two transports
  that share the same `BitBangSPI` (demo board under MicroPython; cocotb
  testbench pads). Coroutines throughout, `run_sync()` on the board, a
  command line that runs on the board and is forwarded from a PC through
  mpremote, and `selftest_*` bodies that run unchanged on both. Tests: 9
  pytest cases against a pin-level fake chip, 4 cocotb tests through the SPI
  pads (self-tests alone and under the lockstep harness, UART loopback with
  more data than a FIFO holds, the datasheet's capture-and-replay example).
  The board transport has not run on hardware. Two things found on the way:
  two files both named `test_keyerhost.py` made cocotb import the pytest one
  (renamed the cocotb module `test_host.py`), and MISO is X for a cycle in
  simulation after a read that empties a never-written FIFO (the pad
  sampler now reads X as 0; the transport looks at the MISO bit only).
- `docs/info.md` rewritten as the Keyer datasheet: deadline timer, timeout
  forms, capture and replay, firmware table, and a "How to test" section
  built on the driver. Its capture example had a wrong wait (thread 1 halts
  twice); fixed, and the example now runs as a test.
- `scripts/gds_report.py` added: summarises a `gds` run from its artifacts
  (jobs, cells, utilisation, slack per corner, routing, sign-off counts,
  precheck table, worst violators); checked against the first run's
  hand-logged numbers.
- Hardening results (docs/AREA.md has the tables and paths):
  - `sram-macro` (37169889955): all four jobs green. 13,203 cells plus the
    macro, 24.7% utilisation, routing 49 min. Setup +11.20 / **+6.40** /
    -1.94 ns (fast / typical / slow), hold clean, LVS and antenna 0,
    precheck 9/9, `gl_test` passing on the netlist with the macro model.
    The waivers hold up: every Magic DRC box is inside the macro; the ten
    overlaps are the four power stripes over the supply-split band; the PDN
    verifier found every stripe inside same-net columns. The slow corner
    got worse than with flops because the macro's access time is 5.4 ns at
    that corner against 0.6 ns for a flop.
  - Rule of D-028 applied: typical slack at least +5 ns and all sign-off
    checks pass, so `sram-macro` was merged into master (D-029).
  - `decode-onehot` (37176010222), dispatched because the slow corner still
    failed: setup +11.85 / **+7.41** / **-0.13** ns, one violating endpoint
    left (`deadline[0][15]`, the WAITD update); 13,317 cells, routing
    49 min, `gl_test` passing. Left for Ahan to merge.
  - Master before the merge, flops with capture (37169341232): 70.0%
    utilisation, +10.84 / +5.96 / -2.36 ns, `gl_test` passing (the first
    green gate-level run); its precheck was superseded by the merge push.
- End of session 3: `decode-onehot` run finished with all four jobs green
  (precheck 9/9). Master is 8dab03a (the merge of `sram-macro`) plus this
  docs commit; on GitHub its `check`, `test` and `docs` workflows pass and
  its own `gds` run (37181323699) is in progress. Locally the full check
  suite is green on master: 52 Python tests, 20 cocotb tests, lint, Icarus,
  four formal groups. Open for Ahan: merging `decode-onehot`.

## 2026-10-04, Claude Code session 4 (merge of decode-onehot, FPGA build, mutation tool, more protocols)

- D-030 (Ahan): `decode-onehot` merged into master and pushed. Master runs
  logged in docs/AREA.md: 37181323699 (the `sram-macro` merge, equal to the
  branch run) and 37228068179 (this merge: +11.85 / +7.41 / -0.13 ns, one
  slow-corner endpoint, all four jobs green, precheck 9/9).
- `fpga/alhambra2/`: board wrapper, pin file, `build.sh` (Yosys, nextpnr-ice40,
  icepack), `sim.sh` (the pads-only cocotb tests on the post-synthesis
  netlist, 10 of 10). 3,557 of 7,680 logic cells, 5 block RAMs (program
  memory and the four FIFOs), 40.8 MHz against 12 MHz. Not run on the board.
- `tools/mutate.py`, first version, with `tools/test_mutate.py`. BUGS 16-18.
- Checkpoint at 20:15 after the session hit its usage limit with seven
  subagents running; six of them were cut off. State of their work:
  - Committed, passing on the model and in lockstep: `jtag_master` (67
    words), `ws2812` (33 words), `ps2_host` (79 words). The WS2812 and PS/2
    agents were cut off before reporting, so their bug lists were not
    delivered; the JTAG agent's is in BUGS.
  - **Not committed, in the working tree:** `i2c_slave` (firmware 86 words,
    model, 35 model tests passing, but both lockstep tests fail an
    assertion: unfinished); `spi_slave` (firmware 54 words and model, no
    tests); `swd` (firmware 116 words only, no model, no tests).
  - **Not committed, in the worktree `.claude/worktrees/agent-abc7f6433b0067f3a`
    (branch `waitd-csa`, no commits yet):** the carry-save `WAITD` change in
    `src/keyer_core.v`, `formal/equiv_core.sh`, the two formal properties,
    and edits to `formal/run_core_pdr.sh` and `scripts/check_all.sh`. Its
    last step was making `run_core_pdr.sh` fail on a counterexample;
    nothing in it has been verified by the coordinating session.
  - The first mutation campaign was killed about an hour in, after a
    simulator process had been killed by hand; its partial results were
    discarded. Treated as not run.
- After the checkpoint (same day, evening):
  - `waitd-csa`: the rtl subagent's uncommitted work was checked by the
    coordinating session (equivalence 533 of 533 against master, a seeded
    off-by-one caught; T9 and P8 each fail on their seeded fault; lint;
    full suite) and committed as faaeee1, pushed; `gds` run 37247680638: setup +12.33 / +8.12 / +0.76 ns, no violation at any corner, all four jobs green.
    It also made the check scripts fail on a failed proof (BUGS 19).
  - The three unfinished protocol sets were finished by hand: I2C slave (a
    stale assertion in its lockstep test), SPI slave (tests written; a late
    MISO release fixed, BUGS 24, 25), SWD (model and tests written; three
    firmware faults found by the model and fixed, BUGS 21 to 23). All six
    are in the cocotb suite.
  - `tools/mutate.py` reworked to D-031 (JSONL with `--resume`, verdicts
    from `results.xml`, fastest check first, `--sample`, `--shard`,
    timeout) and `.github/workflows/mutation.yaml` added. A 150-mutant
    sample (seed 1, four workers, 35 minutes): 128 killed at first; the 22
    others became 13 kills (nine through seven new tests in
    `test/test_corners.py`) and 9 equivalents (4 by Yosys, 5 documented).
    One survivor exposed a silent spot in the spec (MISO idle level,
    D-033 OPEN).
  - D-032: the behavioural memory holds its read data in a write cycle
    (rtl subagent, branch `imem-bram`, merged): one block RAM and one LUT
    instead of 42 flops and 29 LUTs around it. FPGA build now 3,501 logic
    cells, 45.5 MHz; post-synthesis simulation 10 of 10.
  - `docs/VERIFICATION.md` written; datasheet, README and HANDOFF updated.
  - End of session: `bash scripts/check_all.sh` green on master (278
    pytest, 39 cocotb, lint, Icarus, four formal groups). The full
    mutation campaign has not run (workflow, by hand). Open for Ahan:
    merging `waitd-csa`, D-033.

## 2026-10-04, Claude Code session 5 (merge of waitd-csa, mutation campaign, serializer, stretch firmware)

- Ahan's decisions applied: D-034 (`waitd-csa` merged into master; full
  check suite green on the merged tree), D-033 resolved (SEMANTICS 10.1
  states that MISO is 0 while CS_n is high; the test stays), D-035 (no
  hardware bring-up; README, VERIFICATION and the FPGA README say the FPGA
  build is a synthesis and post-synthesis-simulation result only; the task
  is gone from HANDOFF).
- Master run 37252116397 (commit 1c6def3, the one-hot master with D-032's
  behavioural-memory change) finished green and is logged in docs/AREA.md:
  identical to 37228068179 (+11.85 / +7.41 / -0.13 ns, 24.7%). The merge
  was pushed only after it finished (a newer master push cancels a running
  one); that push started run 37260270799 and the `mutation` workflow was
  started on the merged master (run 37260274984).
- Master run 37260270799 (the `waitd-csa` merge, commit a56aae4) finished
  green and is logged in docs/AREA.md: +12.33 / +8.12 / +0.76 ns, no
  violation at any corner, 24.8%, precheck 9/9, gl_test passing; equal to
  the branch run.

- Serializer RTL (rtl subagent, branch of `serializer`, written from
  SEMANTICS 0.4 section 15 without opening the golden model):
  `src/keyer_ser.v` (all 15.1 registers by name and width, every
  "don't care" value as specified), decode / blocking / timeout forms /
  write data / Z and C in `src/keyer_core.v`, a third write port in
  `src/keyer_pins.v` (after replay, before PINMODE, push-pull pins of the
  pair only), instance `u_ser` in the top; file lists updated (info.yaml,
  test/Makefile, check_all.sh, synth, FPGA; `tools/mutate.py` still lacks
  keyer_ser.v: tools/ was out of bounds for this task).
  Verified: Verilator -Wall lint and Icarus compile (both check_all lines,
  and the core alone with NTHREADS = 4); formal pins (open-drain
  invariant with the serializer port free, plus property 6), fifo, core
  PDR (T9 extended to the serializer strobes), capture: all pass; new
  directed bench `test/ser_unit/run.sh` (8 phases: NRZI and Manchester
  transmit and receive, CRC-16/CRC-32 catalog check values, CRC-5 token,
  stuffing, overrun, stuffing error, frame not on a byte boundary, owner
  thread, open-drain pin in the pair, SERCFG mid-frame, blocking and
  timeout forms) passes, and 19 seeded RTL mutations each make it fail.
  Area (Yosys, typ liberty, macro black-boxed): 8,155 -> 9,489 cells,
  1,470 -> 1,598 flops, 141,595 -> 157,841 um^2; the engine alone 1,080
  cells, 128 flops, 15,253 um^2. Not run: the cocotb suite and pytest (the
  model does not implement the new instructions yet). Spec questions
  Q21-Q25 in docs/spec-questions.md. `formal/equiv_core.sh` against
  master no longer applies (the core gains ports on purpose): run
  check_all with EQUIV_REF=none on this branch.
- **Mutation campaign** (run 37260274984, started with `gh`, 16 shards, on
  the merged master before the serializer): 1,542 mutants, 1,365 killed,
  42 equivalent by Yosys, 0 errors, 135 not killed. Processed by two
  agents, one per half, each re-running only its mutants locally: 80
  killed by 19 new tests (`test/test_mut_host.py`, `test/test_mut_core.py`),
  41 new equivalence rows, 5 rows already documented, 9 unresolved because
  SEMANTICS is silent (D-038 OPEN). Score 1,445 of 1,454 non-equivalent,
  99.4%. No RTL or model bug. docs/VERIFICATION.md section 8 has the
  tables; rows in docs/mutation_full.jsonl.
- **Serializer** (D-036): docs/SERIALIZER.md and SEMANTICS section 15
  written first (shared engine, half duplex, symbol period from the owner's
  timer, 48 MHz for USB low speed and 40 MHz for 10BASE-T, ISA version 3
  with ten instructions in XFER sub-opcode 15 and `SERI`/`SERIC`). The
  golden-model agent and the rtl agent then worked from SEMANTICS alone in
  separate worktrees. First lockstep run of nine new tests: no mismatch on
  any of the 33 engine registers, three random seeds included; the four
  failures were in the test stimulus (BUGS 31, 32). The agents' spec
  questions (Q21, Q22, R21 to R25) were settled by quoting or clarifying
  SEMANTICS 15; none needed a change of behaviour on either side.
  Formal (`formal/ser.sby`, a third agent): stuffing and CRC proved
  unbounded, round trip bounded; it found the J precondition now stated in
  15.3 (BUGS 41). Yosys estimate for the engine 15,253 um^2; layout
  20,113 um^2.
- **Stretch firmware**: `fw/usb_ls_device.s` (253 words; enumerates against
  `tools/protomodels_usb_host.py`; 56 model tests, 2 lockstep) and
  `fw/eth_10bt_tx.s` (88 words; `tools/protomodels_eth_10bt.py`; 28 model
  tests, 2 lockstep). BUGS 33 to 40.
- **Hardening**: branch run 37266431182: +12.53 / +8.41 / +1.37 ns, no
  violation at any corner, 27.0%, precheck 9/9, gl_test passing. Merged
  into master as D-037.
- The `check` workflow failed on the branch although the suite passed
  locally: sby's abc engine against the runner's older system Yosys (BUGS
  44): fixed by `--yosys yowasp-yosys` and by `formal/run_ser.sh`, which
  runs the two PDR tasks with abc directly.
- End of session: `bash scripts/check_all.sh` green on the merged master
  (406 pytest, 71 cocotb, lint, Icarus, the serializer unit bench, core
  equivalence 547 of 547, five formal groups). Pushed. Open for Ahan:
  D-038.


## 2026-10-05, Claude Code session 6 (D-038, D-039, slew, model vectors, mutation campaign, timing check, D-040)

- **D-038 resolved** (Ahan): SEMANTICS 0.5 section 10 (MISO is 0 except
  while a read data byte is shifted out; CS_n high from two cycles before
  `rst_n` rises; a fourth PINS byte of 0; IMEM_DATA while a thread runs
  unspecified), datasheet and isa.md. Tests:
  `test_miso_is_low_except_while_read_data_is_shifted_out`, the PINS read
  to eight bytes, `test_host_reset_through_the_driver`.
  `tools/keyerhost.py` parks the SPI pins around every reset in both
  transports, refuses a reset inside a transaction and a transaction under
  reset (two pytest cases, one with a fake MicroPython board). The ten
  mutants concerned, run again: five killed, five equivalent;
  `host-c6659575` is not a kill under the new rule and keeps its row with
  the reason rewritten. The lockstep harness's instruction-word comparison
  is fixed (BUGS 45) and `LockstepW` is gone. BUGS 48, 49.
- **D-039** (Ahan; cycle-exact form by Claude): `rx_drop`, status bit 10;
  a `SERCFG` that aborts a frame writes the idle state of the pair it
  leaves (NRZI: released with J in the output registers; Manchester: both
  low); a receive is abandoned when the transmitter starts. Model and RTL
  by the two subagents from SEMANTICS alone, in separate worktrees; the
  first lockstep run after merging had no mismatch. 27 model tests, five
  new phases in the unit bench, the stuffing proof without its J
  precondition plus properties S4 (every frame starts from idle), S5 (the
  abort) and later S6 (`rx_drop`). New scenario `drop_then_abort` on the
  model and in lockstep. Neither agent had a spec question; two sentences
  of 15.2 and 15.3 were clarified on the model agent's remarks.
- **Step 1.** Master run 37286790227 logged: success, equal to branch run
  37266431182. The 13 max-slew entries are 13 pins on two nets at the slow
  corner (the `SERCFG` strobe, inbox 1's pointer comparison): real
  violations of the library limit that the flow only reports. Fixed by
  `DESIGN_REPAIR_MAX_SLEW_PCT` 50 (D-041; not by RTL, the entry says why).
- **Step 2.** `tools/test_protomodels.py`, 10 cases: USB CRC-5 and CRC-16
  white-paper examples and 8.3.5 residuals, CRC-32 check value, the
  fpga4fun UDP frame with FCS B3 31 88 1B, hand-built SETUP and DATA0
  packets as J/K strings, the two models' decoders on them. The models
  agreed with every vector. Two vectors I first wrote from memory were
  wrong (a fourth CRC-5 example, the frame's FCS); both were caught by an
  independent computation before any test was written, and the frame was
  then checked against the published page.
- **Step 4.** `keyerasm.py --check-timing` (`tools/keytiming.py`, 24
  pytest cases): 38 `WAITD` sites in the twelve programs, none late, none
  unbounded; three behind a blocking instruction, each with a
  `; timing:` waiver in the source. Its count for the USB response path
  (15 slots of 48) equals the one derived by hand in the firmware header.
- **Step 5.** D-040 (OPEN) on 512 words, recommendation no: the 512 x 16
  macro adds 1.26 ns clock-to-output at the slow corner on the path all
  twelve worst endpoints share.
- **Hardening, branch `d039`**, run 37339746749: +12.39 / +8.25 / +0.99 ns,
  no violation at any corner, 27.1%, 0 max-slew (D-041 confirmed), one
  max-cap entry on the macro's `A_DOUT[7]` (68.4 fF against 64), precheck
  9/9, gl_test passing. Merged into master by the rule of step 6.
- **Step 3.** Mutation campaign 37339747331 on the same commit: 2,118
  mutants, 2,014 killed, 95 equivalent, 7 not killed, 2 errors. Processed:
  the 7 killed (pin 8 boundary: three lines in an existing test; `rx_drop`:
  one new lockstep test and formal property S6), the 2 errors proved
  equivalent by Yosys locally. 2,021 of 2,021 non-equivalent.
- The `check` workflow failed twice on the branch with the suite green
  locally: `yowasp-yosys` crashing in a model build when sby ran the
  serializer tasks side by side (BUGS 50); `run_ser.sh` now runs one task
  at a time and retries a task that ends without a verdict.
- GitHub refused artifact downloads for a while ("Egress is over the
  account limit", five of fourteen shards); they went through half an hour
  later. `scripts/gds_report.py` pulls about 300 MB per run: two runs were
  logged this session.
- End of session: `bash scripts/check_all.sh` green on master (473 pytest,
  75 cocotb, lint, the unit bench, five formal groups). Pushed.
- After the merge the master `gds` and `docs` runs (37363511492,
  37363511559) were cancelled by GitHub: no hosted runner took the jobs.
  Both re-run; not logged yet (HANDOFF, next task 1).

## 2026-10-06 (session 7)

- Ahan's answers: D-040 closed as no (256 words stay); D-043
  `DESIGN_REPAIR_MAX_CAP_PCT` 30 on branch `d043` (hardening run started;
  merged or reverted by the rule in the entry). Housekeeping: the eleven
  subagent worktrees under `.claude/worktrees/` and their branches, plus
  `d039` and `waitd-csa`, deleted locally and on GitHub (all were merged;
  `git branch --no-merged master` was empty); `.gitattributes` marks `.v`
  and `.vh` as Verilog and `macro/` as vendored for GitHub's language
  statistics; the attribution of the block facts and the macro recipe
  taken from thomasgilbert481/tt_um_loom now lives in D-025, D-040 and the
  head of docs/AREA.md, and `docs/review-of-tt_um_loom.md` is deleted with
  every link to it.
- **Step 1.** Master run 37363511492 (71d8a6d) logged: success, equal to
  branch run 37339746749 in every number (15,085 cells, 27.1%, +12.39 /
  +8.25 / +0.99 ns, 0 max-slew, 1 max-cap, precheck 9/9, gl_test passing).
  The `check` and `docs` runs of the last commit of session 6 had also been
  dropped by GitHub for want of a runner; both were started again (`docs`
  green; `check` noted below).
- **Step 3** (subagent, Opus, in the main tree): `keyerhost.py capture read |
  listing | decode` (UART, SPI, I2C, USB low speed, 10BASE-T Manchester)
  on the board, from a PC (listing and decode run on the PC from the
  forwarded `capture read`, or from a saved file) and in the simulation,
  which calls the same `command()`. The I2C slave model is a slave, not a
  decoder (on a read it substitutes its own memory for the wire), so a
  passive `I2cDecoder` joined `tools/protomodels.py`. Two pads-only cocotb
  tests capture the I2C master's pointer write, repeated START and
  one-byte read (102 entries; a two-byte read would need 116 to 128, more
  than the 117 words above the 139-word firmware) and eight UART bytes,
  and check the decodes against what the firmware was told to send and
  the entry deltas against the pad edges. `load FILE.s -D NAME=VALUE`.
  Found on the way: the board entry point reset the chip on every command
  (BUGS 51). 484 pytest, 77 cocotb, `check_all.sh` green.
- **Step 4.** `docs/SUBMISSION.md` (the form text and the datasheet's long
  form, with the numbers of run 37363511492 and the present suite).
  info.md's "How to test" now needs only the demo board and the driver:
  self-test; UART through the RP2350's own UART1 (`ui[3]` is GP20, UART1
  TX; `uo[4]` is GP37, UART1 RX; the firmware is loaded with `-D TX=20 -D
  BAUD_DIV=434`); a capture and decode of the chip's own UART transmitter
  (thread 0 alone, so the engine has the memory port); then the I2C
  capture, which needs pull-ups and an EEPROM, with the simulation's real
  output; "External hardware" says none for the first three. Stale facts
  fixed on the way: 98 instructions (not 86), ISA version 3 in the
  self-test's expected line, `uart.s` is 37 words (VERIFICATION's table
  had the sizes of three programs in the wrong rows).

