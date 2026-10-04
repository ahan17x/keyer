# Handoff: state of the project and the next task

Updated 2026-10-03 (end of Claude Code session 2). This is the first file a
session reads. Keep it short: what exists, what is decided, what is open,
what to do next.

## What exists (all tests green)

- GitHub: <https://github.com/ahan17x/keyer>, public, branch `master`;
  Actions and Pages on. Workflows: `check` (the project's own, runs
  `scripts/check_all.sh` in full), `test` (template; its
  `! grep failure results.xml` check now works because `test/Makefile`
  drops cocotb 2's `failures="0"` on a passing run, D-027), `docs`, `gds`
  (hardens only on changes to `src/**`, `info.yaml`, `macro/**`, or by
  dispatch; a newer push on the same branch cancels the run it supersedes,
  D-022). Hardening results are logged in `docs/AREA.md`.
- `docs/SEMANTICS.md` v0.3: the cycle-exact contract (section 14: capture
  and replay). It wins over `docs/isa.md` (ISA v0.2, 86 instructions).
  `docs/spec-questions.md`: 20 questions raised by the subagents, all
  resolved into SEMANTICS. `docs/CAPTURE.md`: the capture-and-replay
  programmer's view and sizing decision (D-024).
- `tools/keyer_isa.py`, `keyerasm.py`, `keyersim.py` (golden model, written
  from SEMANTICS without reading `src/`), `protomodels.py`,
  `tools/test_iss.py` + `tools/test_fw.py` (43 tests).
- `fw/uart.s`, `fw/spi_master.s`, `fw/i2c_master.s` (bytecode incl. 0x05
  "start the other thread"), `fw/capture_demo.s` (thread 1 records thread
  0's I2C transaction and replays it).
- `src/`: `keyer_fifo.v`, `keyer_imem.v` (behavioural array on master,
  macro under `KEYER_IMEM_SRAM`), `keyer_pins.v`, `keyer_capture.v`,
  `keyer_core.v` (NTHREADS), `keyer_host.v`, `tt_um_ahan17x_keyer.v`.
  Written from SEMANTICS without reading the model. Verilator `-Wall` clean.
- `test/`: cocotb, 16 tests: 5 host-interface, 2 pads-only firmware tests
  (UART loopback, capture/replay demo; these 7 also run at gate level),
  9 lockstep (ISS vs RTL every cycle from reset, host actions and the
  capture engines mirrored). `test/keyer_tb.py` is the harness.
- `formal/`: FIFO, pins, core (T1-T8, P3-P7), capture (10 properties by
  k-induction, 6 covers). `scripts/check_all.sh` runs everything (about four
  minutes).
- `docs/AREA.md`: 142,277 um^2 of logic with the macro black-boxed (8,336
  cells, 1,463 flops), 18.9% of the core with the macro.
- Branch `sram-macro` (D-025, not merged): the 256 x 16 SRAM macro in the
  flow (vendored macro, blackbox stub, config.json MACROS/PDN/Magic keys,
  `src/pdn_cfg.tcl` with the sibling entry's verified pdngen wrapper,
  attribution in headers). On that branch the cocotb suite simulates the
  vendored macro model and passes. **Local only: not pushed, not hardened**
  (it was conditional on the first run passing timing; see D-026).
- Not yet done: demo-board driver; FPGA; the stretch list in PLAN.md.

## Decisions (docs/DECISIONS.md)

Closed: D-017 to D-022, D-024, D-025, D-027 (which closes D-023 without
editing a template job). OPEN, waiting for Ahan: **D-026** (setup timing
fails at 20 ns in the slow corner; options and a recommendation).

## Next tasks, in order

1. **Decide D-026.** The first hardening run (docs/AREA.md) is clean on
   DRC, LVS, antenna, precheck and hold, but setup is -0.59 ns at the slow
   corner on the path instruction register -> decode -> ALU -> register
   write. Recommended: push `sram-macro` and dispatch `gds` on it
   (`git push -u origin sram-macro && gh workflow run gds.yaml --ref
   sram-macro`), since the macro removes two thirds of the cells and moves
   the start of the path; then read its timing before touching RTL or the
   clock. `CLOCK_PERIOD` must not change without Ahan.
2. **Read the `gds` run that the push of master started** (the design with
   capture and replay, flop memory): log it in docs/AREA.md next to the
   first run, including whether `gl_test` now passes (7 tests run at gate
   level, 9 lockstep tests skip).
3. **Demo-board Python driver** (`tools/`): the register map of isa.md
   section 6, respecting the 16-deep inbox like `HostFeeder`, plus capture
   drain and replay load helpers mirroring `test/keyer_tb.py`.
4. **Datasheet** (`docs/info.md`): capture/replay section, a worked example
   of each firmware, the verification story in a paragraph.
5. Then the stretch list in PLAN.md (CRC engine, serializer, USB LS).

Rules that proved their worth: a lockstep harness cannot see a wrong
program load or a host action sent outside its loop, so keep functional
checks next to it; every spec question goes into `docs/spec-questions.md`
and is resolved by editing SEMANTICS.md; a push to a branch with a running
hardening cancels it (D-022), so hold pushes until a run you need has
finished; gate-level tests can only use the pads (`GATES=yes` skips the
lockstep tests).
