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
