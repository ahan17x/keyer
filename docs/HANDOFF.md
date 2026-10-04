# Handoff: state of the project and the next task

Updated 2026-10-04 (end of Claude Code session 3). This is the first file a
session reads. Keep it short: what exists, what is decided, what is open,
what to do next.

## What exists (all tests green)

- GitHub: <https://github.com/ahan17x/keyer>, public, branch `master`.
  Workflows: `check` (the project's own: `scripts/check_all.sh` in full,
  with a current z3 from pip), `test` (template), `docs`, `gds` (hardens
  only on changes to `src/**`, `info.yaml`, `macro/**`, or by dispatch; a
  newer run on the same branch cancels the one it supersedes, D-022).
  `scripts/gds_report.py RUN_ID` summarises a run for `docs/AREA.md`.
- **Master is the macro design** (D-029): the program memory is the
  RM_IHPSG13_1P_256x16 SRAM macro (vendored in `macro/`, flow keys in
  `src/config.json`, PDN wrapper in `src/pdn_cfg.tcl`, attribution in the
  headers); `KEYER_IMEM_FLOPS` selects the flop fallback. Hardened on
  `sram-macro` (run 37169889955): 13,203 cells plus the macro, 24.7%
  utilisation, setup +11.20 / +6.40 / -1.94 ns (fast / typical / slow),
  hold clean, LVS and antenna 0, precheck 9/9, gl_test passing. Tiny
  Tapeout signs off at the typical corner (D-028); the slow corner is
  tracked in `docs/AREA.md`.
- Branch `decode-onehot` (pushed, **not merged**): the same design with a
  registered one-hot thread select replicated per consumer; equivalence
  with the `sram-macro` core proven (776 of 776 points), full suite green,
  hardened (run 37176010222, all four jobs green, precheck 9/9): setup
  +11.85 / +7.41 / -0.13 ns, one slow-corner endpoint left
  (`u_core.deadline[0][15]`).
- `docs/SEMANTICS.md` v0.3 is the cycle-exact contract (it wins over
  `docs/isa.md`, ISA v0.2, 86 instructions); `docs/CAPTURE.md` describes
  capture and replay; `docs/spec-questions.md` holds 20 resolved questions.
- `tools/`: `keyer_isa.py`, `keyerasm.py`, `keyersim.py` (golden model,
  written from SEMANTICS without reading `src/`), `protomodels.py`,
  `keyerhost.py` (host driver: one API on the demo board under MicroPython
  and on the simulation; the board transport has not run on hardware),
  and their tests (52 pytest cases).
- `fw/`: `uart.s`, `spi_master.s`, `i2c_master.s`, `capture_demo.s`.
- `src/`: `keyer_fifo.v`, `keyer_imem.v`, `keyer_pins.v`, `keyer_capture.v`,
  `keyer_core.v`, `keyer_host.v`, `tt_um_ahan17x_keyer.v`, the macro
  blackbox. Written from SEMANTICS without reading the model.
- `test/`: cocotb, 20 tests in two modules: `test.py` (5 host-interface,
  2 pads-only firmware tests, 9 lockstep) and `test_host.py` (the driver's
  self-tests alone and under lockstep, a UART loopback and the datasheet's
  capture example). 10 of them also run at gate level (`GATES=yes`); the
  lockstep tests skip there.
- `formal/`: FIFO, pins, core, capture. `scripts/check_all.sh` runs
  everything (about five minutes).
- `docs/info.md`: the datasheet, with a "How to test" section built on the
  driver. `docs/AREA.md`: synthesis numbers and the four hardening runs.

## Decisions (docs/DECISIONS.md)

All closed up to D-029. Waiting for Ahan: whether to merge `decode-onehot`.

## Next tasks, in order

1. **Decide on `decode-onehot`.** It is equivalence-proven, better at
   every corner, takes the slow-corner miss from 277 endpoints to one, and
   its run passed every job including the precheck. If Ahan agrees, merge
   it into master (a new DECISIONS entry) and log the master run the push
   starts.
2. **Log the master `gds` run started by the merge of `sram-macro`**
   (37181323699, in progress when the session ended) in `docs/AREA.md`:
   `python3 scripts/gds_report.py 37181323699`. It re-hardens the design of
   run 37169889955 on master.
3. **The last slow-corner endpoint** (`deadline[0][15]`, WAITD): the target
   `DEADLINE + k` and the test `NOW - target >= 0` are two chained 16-bit
   carry chains after the macro's 5.4 ns access time. A three-input
   carry-save step followed by one carry chain computes the same test; no
   change to SEMANTICS. Use the rtl subagent, prove equivalence as for
   `decode-onehot`, harden.
4. **Two formal properties to add** (found by the equivalence check, not
   by the existing properties): a blocked timeout-form wait never writes C;
   START and STOP never act on the executing thread itself.
5. **Run the driver on hardware** when an FPGA build (`KEYER_IMEM_FLOPS`)
   or silicon exists: `python3 tools/keyerhost.py selftest`; adjust the pin
   numbers in `BoardTransport` if the board revision differs.
6. Then the stretch list in PLAN.md (CRC engine, serializer, USB LS).

Rules that proved their worth: a lockstep harness cannot see a wrong
program load or a host action sent outside its loop, so keep functional
checks next to it; every spec question goes into `docs/spec-questions.md`
and is resolved by editing SEMANTICS.md; a push to a branch with a running
hardening cancels it (D-022), so hold pushes until a run you need has
finished; gate-level tests can only use the pads; keep the solver in CI as
current as the local one.
