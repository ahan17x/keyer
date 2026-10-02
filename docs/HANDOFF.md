# Handoff: state of the project and the next task

Updated 2026-10-02 (end of Claude Code session 1). This is the first file a
session reads. Keep it short: what exists, what is decided, what is open,
what to do next.

## What exists (all tests green)

- `docs/SEMANTICS.md` v0.2: the cycle-exact contract. It wins over
  `docs/isa.md` (programmer's reference, ISA v0.2, 85 instructions).
  `docs/spec-questions.md` logs the nine questions raised while implementing
  from it, all resolved into SEMANTICS. The model was re-derived from
  SEMANTICS alone after the RTL was written (D-012) and matched the RTL in
  every lockstep cycle on the first run; the only finding was BUGS 15.
- `tools/keyer_isa.py` (encoding table, generates `src/keyer_isa.vh`),
  `tools/keyerasm.py` (assembler), `tools/keyersim.py` (cycle-exact golden
  model, written from SEMANTICS without reading `src/`),
  `tools/protomodels.py` (UART/SPI/I2C models, host feeder),
  `tools/test_iss.py` + `tools/test_fw.py` (33 tests).
- `fw/uart.s` (full duplex on two threads), `fw/spi_master.s`,
  `fw/i2c_master.s` (bytecode from the host; repeated start; clock
  stretching with a timeout that reports status 0xFF). All verified against
  the protocol models, on the model and in lockstep with the RTL.
- `src/`: `keyer_fifo.v`, `keyer_imem.v` (behavioural + SRAM macro wrapper
  under `KEYER_IMEM_SRAM`), `keyer_pins.v`, `keyer_core.v` (NTHREADS
  parameter, default 2), `keyer_host.v`, `tt_um_ahan17x_keyer.v`. Written
  from SEMANTICS without reading the model. Verilator `-Wall` clean.
- `test/`: cocotb, 11 tests: 4 host-interface (ID/registers, PC read-back
  and soft reset, 256-word program load, FIFO round trip), 7 lockstep (ISS vs
  RTL every cycle from reset, host actions mirrored: ALU/branches, pins and
  timer, UART loopback, SPI master, I2C master with stretching, I2C stuck-SCL
  timeout, constrained-random programs). `test/keyer_tb.py` is the harness.
- `formal/`: FIFO (induction + BMC), pins (induction; open-drain never
  drives high), core (timer T1-T8 and control P3-P7, abc pdr, about 2 s).
- `synth/`: Yosys against the CMOS5L liberty. Last CMOS5L numbers are from
  before D-018 (6,410 cells, 1,190 flops, 114,169 um^2 + 28,127 um^2 SRAM
  macro); the timer added about 62 flops to the core. Re-run after the next
  RTL change (see synth/README.md for the liberty path).
- `scripts/check_all.sh` runs everything (about three minutes);
  `scripts/setup_mac.sh` installs the tools and the venv. `docs/SETUP.md`
  lists the pitfalls met on the Mac.
- Not yet done: GitHub push and first `gds` run; SRAM macro flow config
  (`src/config.json` MACROS + PDN); demo-board driver; FPGA; the
  differentiator and the stretch list in PLAN.md.

## Decisions (docs/DECISIONS.md, all closed up to D-021)

Name Keyer (D-017). NOW/DEADLINE timer with timeouts on every blocking
instruction (D-018, implemented). Two threads, NTHREADS parameter (D-019,
implemented). Differentiator: capture-and-replay plus staying small; Hardcaml
out (D-020, not started). Keep and evolve the core (D-021).

## Next tasks, in order

1. **Design capture-and-replay (D-020)** as a proposal first, not code:
   write `docs/CAPTURE.md` with the programmer's view (which pins, trigger
   condition, timestamp format and width, buffer size and where it lives,
   how the host drains it, how playback is started and timed, what happens
   on overflow), the ISA or host-register additions, an area estimate
   against the budget in PLAN.md section 4, and the test plan (lockstep plus
   a capture of the chip's own UART/SPI/I2C firmware). Stop for Ahan's
   review before any RTL or model change. Then: SEMANTICS section, model
   (golden-model subagent) and RTL (rtl subagent) in parallel, tests.
2. **Push to GitHub, get the first `gds` run** at 6x4 with the flop memory,
   then with the macro. Record cells, utilisation, timing and routing time
   in WORKLOG.md. (The parallel entry's `docs/tt_cmos5l_facts.md` section 11
   documents a working macro placement recipe for this flow, Apache-2.0;
   Tiny Tapeout is also preparing an official macro template.)
3. Re-run the CMOS5L synthesis (`synth/`) and record the post-D-018 numbers.
4. Demo-board Python driver (`tools/`), mirroring `HostFeeder` (respect the
   16-deep inbox) and the register map in isa.md section 6.
5. Then the stretch list in PLAN.md.

Rules that proved their worth this session: run `bash scripts/check_all.sh`
on a fresh checkout before calling a commit green (BUGS 11); a lockstep
harness cannot see a wrong program load, so keep functional checks next to
it (BUGS 14); every spec question goes into `docs/spec-questions.md` and is
resolved by editing SEMANTICS.md.
