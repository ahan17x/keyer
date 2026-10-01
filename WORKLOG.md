# Loom work log

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
- Next: cocotb testbench — SPI host register tests, then cycle-by-cycle lockstep of RTL against the ISS from reset, with every host action mirrored from the RTL into the ISS.
