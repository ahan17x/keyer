# Handoff: state of the project and the next task

Updated 2026-10-04 (end of Claude Code session 4). This is the first file a
session reads. Keep it short: what exists, what is decided, what is open,
what to do next.

## What exists (all tests green)

- GitHub: <https://github.com/ahan17x/keyer>, public, branch `master`.
  Workflows: `check` (`scripts/check_all.sh` in full), `test` (template),
  `docs`, `gds` (hardens only on changes to `src/**`, `info.yaml`,
  `macro/**`, or by dispatch; a newer run on a branch cancels the older,
  D-022), and `mutation` (new; started by hand only, 16 shards, D-031).
  `scripts/gds_report.py RUN_ID` summarises a `gds` run for `docs/AREA.md`.
- **Master is the macro design with the one-hot thread select** (D-029,
  D-030). Hardened on master as run 37228068179: 13,317 cells plus the
  macro, 24.7% utilisation, setup +11.85 / +7.41 / -0.13 ns (fast / typical
  / slow), one slow-corner endpoint (`deadline[0][15]`), hold clean, LVS and
  antenna 0, precheck 9/9, gl_test passing. Sign-off is at the typical
  corner (D-028).
- Branch `waitd-csa` (pushed, **not merged**): the carry-save `WAITD`
  completion test (one compressor level and one carry chain instead of two
  chains), formal properties T9 and P8, `formal/equiv_core.sh` (533 of 533
  points against master; a seeded fault fails it), and check scripts that
  fail on a failed proof (BUGS 19). Full suite green on the branch.
  Hardened (run 37247680638, all four jobs green, precheck 9/9): setup
  +12.33 / +8.12 / +0.76 ns, **no violation at any corner**.
- `docs/SEMANTICS.md` v0.3 is the contract; `docs/VERIFICATION.md` (new)
  describes the eight verification layers, what each found and the
  commands.
- `tools/`: `keyer_isa.py`, `keyerasm.py`, `keyersim.py` (golden model),
  `protomodels.py` and one `protomodels_NAME.py` per added protocol,
  `keyerhost.py`, `mutate.py` (mutation testing) with
  `mutate_equivalents.md`; 278 pytest cases.
- `fw/`: `uart.s`, `spi_master.s`, `i2c_master.s`, `capture_demo.s`, and new:
  `spi_slave.s` (55 words), `i2c_slave.s` (86 plus a data table),
  `jtag_master.s` (67), `swd.s` (117), `ps2_host.s` (79), `ws2812.s` (33).
  Each has a protocol model, tests on the golden model and two lockstep
  tests. None has run against a real device.
- `test/`: cocotb, 39 tests in nine modules (26 lockstep, 13 through the
  pads only; `test_corners.py` holds the cases mutation testing asked for).
- `formal/`: FIFO, pins, core, capture; `equiv_core.sh` on `waitd-csa`.
- `fpga/alhambra2/`: Alhambra II build (3,501 of 7,680 logic cells, 5 block
  RAMs, 45.5 MHz against the board's 12 MHz) and a post-synthesis
  simulation (10 of 10). Not run on the board. `KEYER_IMEM_FLOPS` now holds
  its read data in a write cycle (D-032).
- Mutation testing: a 150-mutant sample is fully resolved (141 killed, 9
  equivalent). The full campaign of 1,535 mutants has **not** run.

## Decisions (docs/DECISIONS.md)

Closed up to D-032. **Open for Ahan:**

1. **Merge `waitd-csa`?** Recommended: its run closes the slow corner
   (+0.76 ns) and improves the typical one (+8.12 ns), it is
   equivalence-proven, and it carries the two new properties and the
   script fixes. Merging starts one more master `gds` run.
2. **D-033:** say in SEMANTICS 10.1 that MISO is 0 while CS_n is high
   (recommended; the RTL does it and a test now checks it), or declare the
   idle level undefined and drop the test.

## Next tasks, in order

1. Start the `mutation` workflow from the Actions tab (all inputs at their
   defaults). When it finishes, download `mutation-merged`, and for every
   survivor either write a test that kills it (`test/test_corners.py`) or
   add a row to `tools/mutate_equivalents.md`; errors are rerun, not
   counted. Log the score in `docs/VERIFICATION.md` section 8.
2. After Ahan's answers above: merge `waitd-csa` (new DECISIONS entry), log
   the master run; apply D-033.
3. Bring-up on the Alhambra II (Ahan): `bash fpga/alhambra2/build.sh`,
   program, wire four SPI lines, `python3 tools/keyerhost.py selftest` with
   `BoardTransport(pins=..., clock_hz=12000000)`. Check the items listed at
   the end of `fpga/alhambra2/README.md`.
4. Add the new firmware's lockstep-free variants to the pads-only set if
   gate-level coverage of them is wanted (today only UART and the capture
   demo run on the netlist).
5. Then the stretch list in PLAN.md (CRC engine, serializer, USB LS).

Rules that proved their worth: a lockstep harness cannot see a wrong
program load or a host action sent outside its loop, so keep functional
checks next to it; a check script must test the verdict, not that a verdict
was printed; in firmware, `SETD 0` goes directly before the `WAITD` it
arms (BUGS 20, 21); a mutation kill is a reported test failure and nothing
else; at most two subagents at a time, and they do not share a checkout
with a running campaign; hold pushes that touch `src/` until a hardening
run you need has finished.
