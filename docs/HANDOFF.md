# Handoff: state of the project and the next task

Updated 2026-11-12 (Claude Code session 8). This is the first file a
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
  carry-save `WAITD` (D-034), the serializer engine (D-036, D-037), D-039
  and the repair margins of D-041 (slew) and D-043 (capacitance).**
  Hardened on branch `d043` as run 37534398328: 15,109 cells plus the
  macro, 27.2% utilisation, setup +12.43 / +8.24 / +1.09 ns (fast /
  typical / slow), no violation at any corner, hold clean, LVS and antenna
  0, 0 max-slew, 0 max-cap, precheck 9/9, gl_test passing: the sign-off
  checks report nothing. The master run that the merge started
  (37549369058) equals it in every number (logged in AREA.md, session 8).
- **`docs/FACTS.md`** (session 8): every external fact the project rests
  on, where it is used, its source, the source's words and a status
  (63 verified, 3 computed, 20 unverified and marked so where they are
  used). CLAUDE.md: no external fact without a row there from a fetched
  source. The competition post and the template changed after session 7
  (FACTS C3, T11): see "Decisions".
- `docs/SEMANTICS.md` v0.5 is the contract (section 15: the serializer);
  `docs/SERIALIZER.md` is the serializer's design note and programmer's
  guide; `docs/VERIFICATION.md` describes the nine verification layers.
  ISA version 3.
- `tools/`: `keyer_isa.py`, `keyerasm.py` (with `--check-timing`,
  `keytiming.py`: every `WAITD` against the slots that can precede it),
  `keyersim.py` (golden model), `protomodels*.py` (11 protocol models and
  a passive I2C decoder, the USB and Ethernet models checked against
  published vectors in `test_protomodels.py`), `ser_scenarios.py`,
  `keyerhost.py` (holds CS_n high across every reset; `capture read |
  listing | decode` for UART, SPI, I2C, USB low speed and 10BASE-T on the
  board, from a PC or in simulation; `load FILE.s -D NAME=VALUE`),
  `mutate.py` with `mutate_equivalents.md` and the `prove-equivalents`
  mode (reset-sequence miter); 485 pytest cases.
- `fw/`: twelve programs. New: `usb_ls_device.s` (253 of 256 words, one
  thread, clock 48 MHz; enumerates against the USB host model, responses
  3.7 to 4.7 bit times after the host's packet) and `eth_10bt_tx.s` (88
  words, one thread, clock 40 MHz or 20 MHz; link pulses and frames with
  CRC-32 checked by the 10BASE-T receiver model). Nothing has run against
  a real device and no hardware bring-up is planned (D-035).
- `test/`: cocotb, 77 tests in 14 modules (two decode captures of the I2C
  master and the UART through the driver); `test/ser_unit` (plain Verilog
  bench of the serializer, no model).
- `formal/`: FIFO, pins, core, capture, serializer (`ser.sby`, ten tasks),
  `equiv_core.sh`.
- `fpga/alhambra2/`: synthesis and post-synthesis simulation only (D-035).
- Mutation testing: the full campaign on the present RTL (run
  37339747331, 2,118 mutants): 2,021 killed, 97 equivalent (27 proved at
  the module level, 20 at the top level, 20 by the reset-sequence miter,
  30 on a written reason: 23 where the miter gave no verdict in 1,200 s
  and 7 inside the macro), no survivor, no error.
- `docs/SUBMISSION.md`: the text for the Jane Street form and the
  datasheet; info.md's "How to test" works with the demo board and the
  driver alone.

## Decisions (docs/DECISIONS.md)

Closed up to D-044. **Two are open for Ahan, both from changes outside the
repo found by the audit of 2026-11-12:**

- **D-046** (ask first): the Tiny Tapeout template's `main` branch now uses
  `tt-gds-action@ttihp27a` (LibreLane 3.1.0.dev4, the support tools' `main`
  with a tenth precheck); this repo's `gds` workflow is still on
  `@ihp-cmos5l`. Recommendation: move on a branch, harden, merge by the
  D-037 rule, before anything else under `src/`.
- **D-045**: the post now says the maximum is 8x4 tiles and "Set the tile
  size in info.yaml to 8x4"; the repo is 6x4 (27.2% used). Recommendation:
  e-mail the organisers whether a smaller block is acceptable and harden
  8x4 on a branch meanwhile (`info.yaml` only), after D-046.

Also for Ahan: `docs/SUBMISSION.md` section 5 (how AI tools were used; the
post asks for it) is a draft to review.

## Schedule to submission (deadline Monday 2027-01-18, two weeks of margin)

| Date | Milestone |
|---|---|
| until Fri 2026-12-04 | **First D-046 (the `ttihp27a` flow) and then D-045 (8x4), each one hardening run on a branch, as soon as Ahan answers; both must be merged or dropped before the freeze week.** Then feature work on branches (PLAN.md's stretch list, the serializer follow-ups below), each merged by the hardening rule of D-037: setup met at all corners, utilisation under 40%, precheck 9 of 9, `gl_test` passing. Nothing is started after this date that cannot be merged by the freeze. |
| Mon 2026-12-07 to Fri 2026-12-11 | Freeze week. On the candidate commit: the full `check` suite on the runner, a full mutation campaign (`mutation` workflow, every survivor processed), a master hardening run logged in AREA. **RTL freeze Fri 2026-12-11**: tag `rtl-freeze`; from then on nothing under `src/`, `info.yaml` or `macro/` changes. |
| Sat 2026-12-12 to Wed 2026-12-30 | Docs only: `docs/SUBMISSION.md` and `docs/info.md` final (every number from the freeze run and the freeze campaign), README, the datasheet render checked on the `docs` workflow, VERIFICATION and AREA refreshed. Tools, firmware and tests may still change if the datasheet stays true; they do not touch silicon. An RTL fault found in this period reopens the freeze: fix, re-run the freeze week's checks, re-tag, and the docs period restarts from the new run; the last date on which that still fits is Mon 2026-12-21. |
| Thu 2026-12-31 | The final hardening run, started by hand (`gds` workflow, dispatch) on the exact commit to be submitted (tag `submission`), logged in AREA with `scripts/gds_report.py`; it must equal the freeze run in every number. `gl_test` and precheck green on it. |
| Mon 2027-01-04 | Submission: the Jane Street form (text from SUBMISSION.md) and the Tiny Tapeout submission of that commit. Two weeks remain to 2027-01-18 for anything the organisers ask for, a failed upload or a repeat of the final run. |

## Next tasks, in order

1. Ask Ahan D-046, then D-045 (one at a time, each with its
   recommendation), and carry out the answers: a branch, a hardening run
   logged in AREA.md with `scripts/gds_report.py`, the merge rule of D-037.
   Fetch the post and the template again first (FACTS rule: they change).
   If the tile size changes, every "6x4" and utilisation figure in
   CLAUDE.md, README, PLAN, info.yaml, SUBMISSION and info.md follows.
2. The 23 documented equivalents without a miter verdict: session 8's
   campaign (abc variants, then PDR at 3,600 s) is recorded in WORKLOG and
   `tools/mutate_equivalents.md`. The lead that is left: the miter
   strengthened with "every register pair except the mutated one is
   equal" (prototype described in WORKLOG, session 8; bounded check clean,
   PDR needs invariants that count SCK edges). Not required: every reason
   stands and no row got a counterexample.
3. The unverified rows of FACTS.md that matter most for the datasheet:
   IEEE 802.3 (E1 to E6, E9 to E11; needs an IEEE account for the GET
   programme) and the SWD connect sequence (O5). If Ahan can supply the
   PDFs, check the rows and turn them to verified.
4. Serializer follow-up still open: the round-trip proof covers one message
   byte. (The SYNC question is closed: a hub may not shorten a low-speed
   SYNC, FACTS U3.)
5. Then the rest of PLAN.md's stretch list (CRC engine as a
   firmware-visible unit, Hardcaml port) within the schedule above, and
   the datasheet polish before the freeze: the `How to test` steps have
   never run on a board (D-035). The pin claim (GP20 UART1 TX on `ui[3]`,
   GP37 UART1 RX on `uo[4]`) is now confirmed against the SDK's
   `GPIOMapTTDBv3` and the RP2350 datasheet (FACTS T8, T9).

Notes for whoever logs a run: artifacts expire 90 days after the run
(FACTS G3; the October runs on 2027-01-04). "The account has an egress
limit", as session 7 wrote here, has no source (FACTS G4, unverified);
`GDS_logs` is 147 MB compressed and one download per run is still enough.

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
goes into a test; a formal flow is trusted only after its controls behave
(a fault that must fail and a change that must prove, both quick): four
broken flows this session each looked like "hard proofs" until a control
failed to prove in seconds (WORKLOG, step 2); a waiver or an allowance is
written for one cause and must not cover another (BUGS 55); an outside
fact is fetched, not remembered, and the post and the template are fetched
again before they are relied on (FACTS, session 8).
