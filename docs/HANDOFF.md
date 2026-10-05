# Handoff: state of the project and the next task

Updated 2026-10-05 (end of Claude Code session 5). This is the first file a
session reads. Keep it short: what exists, what is decided, what is open,
what to do next.

## What exists (all tests green)

- GitHub: <https://github.com/ahan17x/keyer>, public, branch `master`.
  Workflows: `check` (`scripts/check_all.sh` in full), `test` (template),
  `docs`, `gds` (hardens only on changes to `src/**`, `info.yaml`,
  `macro/**`, or by dispatch; a newer run on a branch cancels the older,
  D-022), and `mutation` (by hand only, 16 shards, D-031).
  `scripts/gds_report.py RUN_ID` summarises a `gds` run for `docs/AREA.md`.
- **Master is the macro design with the one-hot thread select, the
  carry-save `WAITD` (D-034) and the serializer engine (D-036, D-037).**
  The serializer design was hardened on its branch as run 37266431182:
  14,859 cells plus the macro, 27.0% utilisation, setup +12.53 / +8.41 /
  +1.37 ns (fast / typical / slow), no violation at any corner, hold clean,
  LVS and antenna 0, precheck 9/9, gl_test passing. The run the merge
  started on master is logged in docs/AREA.md when it finishes (see "Next
  tasks").
- `docs/SEMANTICS.md` v0.4 is the contract (section 15: the serializer);
  `docs/SERIALIZER.md` is the serializer's design note and programmer's
  guide; `docs/VERIFICATION.md` describes the eight verification layers.
  ISA version 3.
- `tools/`: `keyer_isa.py`, `keyerasm.py`, `keyersim.py` (golden model),
  `protomodels*.py` (11 protocol models), `ser_scenarios.py`,
  `keyerhost.py`, `mutate.py` with `mutate_equivalents.md`; 406 pytest
  cases.
- `fw/`: twelve programs. New: `usb_ls_device.s` (253 of 256 words, one
  thread, clock 48 MHz; enumerates against the USB host model, responses
  3.7 to 4.7 bit times after the host's packet) and `eth_10bt_tx.s` (88
  words, one thread, clock 40 MHz or 20 MHz; link pulses and frames with
  CRC-32 checked by the 10BASE-T receiver model). Nothing has run against
  a real device and no hardware bring-up is planned (D-035).
- `test/`: cocotb, 71 tests in 14 modules; `test/ser_unit` (plain Verilog
  bench of the serializer, no model).
- `formal/`: FIFO, pins, core, capture, serializer (`ser.sby`, ten tasks),
  `equiv_core.sh`.
- `fpga/alhambra2/`: synthesis and post-synthesis simulation only (D-035).
- Mutation testing: the full campaign ran (1,542 mutants, the design before
  the serializer): 1,445 killed, 88 equivalent, 0 errors, 9 survivors
  waiting for D-038.

## Decisions (docs/DECISIONS.md)

Closed up to D-037. **Open for Ahan: D-038**, four questions on the host
interface that nine surviving mutants hang on (MISO during command and
write bytes; a transaction across the release of reset; the fourth byte of
a PINS read; an IMEM_DATA read while a thread runs), each with a
recommendation. D-038 also lists, undecided: the lockstep harness's
comparison of a word rewritten in the cycle it executes (BUGS 45), and
three serializer points (an unread byte dropped at the next frame start
without a flag; a frame being received abandoned by a transmitter start
without a flag; a frame after an aborted one may start from K).

## Next tasks, in order

1. Log the master `gds` run that the serializer merge started (see
   `gh run list --workflow gds --branch master`) in docs/AREA.md and
   WORKLOG.md with `scripts/gds_report.py`; it should equal 37266431182.
2. After Ahan's answers to D-038: write the tests or the equivalence rows
   for the nine mutants, and fix `test/keyer_tb.py` (BUGS 45) if he agrees.
3. Mutation campaign for what the serializer added:
   `gh workflow run mutation.yaml --ref master -f files="keyer_ser.v keyer_core.v keyer_pins.v tt_um_ahan17x_keyer.v keyer_isa.vh"`,
   then process survivors by the same rule (only mutants on lines the
   merge changed are new; the rest keep their ids and verdicts).
4. Serializer follow-ups worth weighing: the receiver needs all eight SYNC
   bits (check what a low-speed hub may drop); the round-trip proof covers
   one message byte; 13 max-slew warnings appeared in the serializer run.
5. Gate-level coverage of the new firmware if wanted (their tests are
   lockstep; `test_mut_host.py` added eight pads-only tests).
6. Then the rest of PLAN.md's stretch list (CRC engine as a firmware-visible
   unit, Hardcaml port) and the datasheet polish before the freeze.

Rules that proved their worth: a lockstep harness cannot see a wrong
program load or a host action sent outside its loop, so keep functional
checks next to it; a check script must test the verdict, not that a verdict
was printed, and must print why when it fails; in firmware, `SETD 0` goes
directly before the `WAITD` it arms (BUGS 20, 21); a mutation kill is a
reported test failure and nothing else; at most two subagents at a time,
each in its own worktree; hold pushes that touch `src/` until a hardening
run you need has finished; stimulus models start with the thread, not with
the program load (BUGS 31); a proof that passes locally is not a proof
that passes on the runner until it has (BUGS 44).
