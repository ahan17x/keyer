# Handoff: state of the project and the next task

Updated 2026-10-05 (end of Claude Code session 6). This is the first file a
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
  carry-save `WAITD` (D-034), the serializer engine (D-036, D-037) and
  D-039.** Hardened on branch `d039` as run 37339746749: 15,085 cells plus
  the macro, 27.1% utilisation, setup +12.39 / +8.25 / +0.99 ns (fast /
  typical / slow), no violation at any corner, hold clean, LVS and antenna
  0, no max-slew entry (D-041), one max-cap entry (see below), precheck
  9/9, gl_test passing. The run that the merge started on master should
  equal it; log it (see "Next tasks").
- `docs/SEMANTICS.md` v0.5 is the contract (section 15: the serializer);
  `docs/SERIALIZER.md` is the serializer's design note and programmer's
  guide; `docs/VERIFICATION.md` describes the nine verification layers.
  ISA version 3.
- `tools/`: `keyer_isa.py`, `keyerasm.py` (with `--check-timing`,
  `keytiming.py`: every `WAITD` against the slots that can precede it),
  `keyersim.py` (golden model), `protomodels*.py` (11 protocol models,
  the USB and Ethernet ones checked against published vectors in
  `test_protomodels.py`), `ser_scenarios.py`, `keyerhost.py` (holds CS_n
  high across every reset), `mutate.py` with `mutate_equivalents.md`; 473
  pytest cases.
- `fw/`: twelve programs. New: `usb_ls_device.s` (253 of 256 words, one
  thread, clock 48 MHz; enumerates against the USB host model, responses
  3.7 to 4.7 bit times after the host's packet) and `eth_10bt_tx.s` (88
  words, one thread, clock 40 MHz or 20 MHz; link pulses and frames with
  CRC-32 checked by the 10BASE-T receiver model). Nothing has run against
  a real device and no hardware bring-up is planned (D-035).
- `test/`: cocotb, 75 tests in 14 modules; `test/ser_unit` (plain Verilog
  bench of the serializer, no model).
- `formal/`: FIFO, pins, core, capture, serializer (`ser.sby`, ten tasks),
  `equiv_core.sh`.
- `fpga/alhambra2/`: synthesis and post-synthesis simulation only (D-035).
- Mutation testing: the full campaign on the present design (run
  37339747331, 2,118 mutants): 2,021 killed, 97 equivalent, no survivor,
  no error.

## Decisions (docs/DECISIONS.md)

Closed up to D-042, except **D-040, open for Ahan**: moving to 512 words
of program memory. The entry has the work list, the PDK's numbers and the
recommendation (no: the larger macro costs 1.26 of the 1.37 ns of
slow-corner margin on the path every worst endpoint shares). D-038 is
resolved, D-039 is the serializer's overrun bit and abort rule, D-041 the
slew repair margin in `config.json`.

Also for Ahan, smaller: one max-cap entry in run 37339746749 (the macro's
`A_DOUT[7]`, 68.4 fF against 64, all corners; reported, not fatal). The
key that would repair it, `DESIGN_REPAIR_MAX_CAP_PCT`, is a `config.json`
edit his slew instruction did not cover.

## Schedule to submission (deadline Monday 2027-01-18, two weeks of margin)

| Date | Milestone |
|---|---|
| until Fri 2026-12-04 | Feature work on branches (PLAN.md's stretch list, the serializer follow-ups below), each merged by the hardening rule of D-037: setup met at all corners, utilisation under 40%, precheck 9 of 9, `gl_test` passing. Nothing is started after this date that cannot be merged by the freeze. |
| Mon 2026-12-07 to Fri 2026-12-11 | Freeze week. On the candidate commit: the full `check` suite on the runner, a full mutation campaign (`mutation` workflow, every survivor processed), a master hardening run logged in AREA. **RTL freeze Fri 2026-12-11**: tag `rtl-freeze`; from then on nothing under `src/`, `info.yaml` or `macro/` changes. |
| Sat 2026-12-12 to Wed 2026-12-30 | Docs only: `docs/SUBMISSION.md` and `docs/info.md` final (every number from the freeze run and the freeze campaign), README, the datasheet render checked on the `docs` workflow, VERIFICATION and AREA refreshed. Tools, firmware and tests may still change if the datasheet stays true; they do not touch silicon. An RTL fault found in this period reopens the freeze: fix, re-run the freeze week's checks, re-tag, and the docs period restarts from the new run; the last date on which that still fits is Mon 2026-12-21. |
| Thu 2026-12-31 | The final hardening run, started by hand (`gds` workflow, dispatch) on the exact commit to be submitted (tag `submission`), logged in AREA with `scripts/gds_report.py`; it must equal the freeze run in every number. `gl_test` and precheck green on it. |
| Mon 2027-01-04 | Submission: the Jane Street form (text from SUBMISSION.md) and the Tiny Tapeout submission of that commit. Two weeks remain to 2027-01-18 for anything the organisers ask for, a failed upload or a repeat of the final run. |

## Next tasks, in order

1. Log the master `gds` run that the merge of `d039` started (see
   `gh run list --workflow gds --branch master`) in docs/AREA.md and
   WORKLOG.md with `scripts/gds_report.py`; it should equal 37339746749.
   It is run 37363511492: its first attempt was cancelled by GitHub after
   15 minutes ("The job was not acquired by Runner of type hosted"), as
   was the `docs` run; both were started again at the end of session 6.
   Check that the second attempt finished.
   The script downloads about 300 MB per run and the account has an
   egress limit: once per run.
2. After Ahan's answer on D-040 and on the max-cap entry: act on them.
3. Serializer follow-ups worth weighing: the receiver needs all eight SYNC
   bits (check what a low-speed hub may drop); the round-trip proof covers
   one message byte.
4. Gate-level coverage of the new firmware if wanted (their tests are
   lockstep; thirteen tests are pads-only).
5. Run `--check-timing` on each program at its smallest supported divider
   (`-D NAME=VALUE`), not only at the defaults.
6. Then the rest of PLAN.md's stretch list (CRC engine as a
   firmware-visible unit, Hardcaml port) and the datasheet polish before
   the freeze.

Rules that proved their worth: a lockstep harness cannot see a wrong
program load or a host action sent outside its loop, so keep functional
checks next to it; a check script must test the verdict, not that a verdict
was printed, and must print why when it fails; in firmware, `SETD 0` goes
directly before the `WAITD` it arms (BUGS 20, 21); a mutation kill is a
reported test failure and nothing else; at most two subagents at a time,
each in its own worktree; hold pushes that touch `src/` until a hardening
run you need has finished; stimulus models start with the thread, not with
the program load (BUGS 31); a proof that passes locally is not a proof
that passes on the runner until it has (BUGS 44, 50); a mutant that only
a random program kills is not killed: give it a directed test; a vector
written from memory is checked by an independent computation before it
goes into a test.
