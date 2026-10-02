# Protocol emulator ASIC (Jane Street competition): instructions for Claude Code sessions

Ahan Shah owns this project. Deadline: Monday 2027-01-18. Target: IHP 130 nm
CMOS5L through Tiny Tapeout, 6x4 tiles, March 2027 shuttle. Named Keyer
(DECISIONS D-017; it was "Loom" until 2026-10-02).

## Read order, every session

1. `docs/HANDOFF.md`: where the project stands and the next task.
2. `docs/DECISIONS.md`: the last ten entries, so decisions are not reopened.
3. `docs/isa.md`: the ISA and timing rules. Until `docs/SEMANTICS.md` exists,
   this is the contract; once it exists, SEMANTICS wins.
4. `docs/BUGS.md` and `WORKLOG.md` (newest entries at the bottom).
5. `PLAN.md` for scope and milestones.

Do not read `docs/review-of-tt_um_loom.md` unless the task is about positioning
or the flow recipe; it is reference material about a parallel entry.

## Rules

- **The spec wins.** If the RTL, the simulator and the spec disagree, the spec
  is the reference. If the spec is wrong or silent, stop, write a DECISIONS
  entry proposing the change with the reason, and ask Ahan. Never quietly
  implement something else.
- **One encoding table.** `tools/keyer_isa.py` is the only place opcodes live.
  After editing it run `python3 tools/keyer_isa.py --vh > src/keyer_isa.vh`
  and commit the result. RTL never compares instruction bits that the header
  does not name.
- **Independence.** The golden model (`tools/keyersim.py`) and the RTL
  (`src/`) are written from the spec, not from each other. A session that
  edits one does not open the other. The subagents in `.claude/agents/` carry
  that restriction; use them for any rewrite of either side. Tests
  (`test/`, `tools/test_*.py`) may read both.
- **Every bug goes in `docs/BUGS.md`**: date, module, symptom, root cause,
  what found it, what now covers it. Bugs in tests and in the model count.
- **Decisions go in `docs/DECISIONS.md`**, append-only, numbered, with the
  alternatives rejected. Reversing one is a new entry.
- **Verilog subset:** Verilog-2005 that Icarus 12, Verilator 5 `-Wall` and
  Yosys accept. `default_nettype none` in every file. Synchronous active-low
  reset. No `initial` in synthesisable code (simulation-only under
  `ifndef SYNTHESIS`). One module per file, file name = module name.
- **Tiny Tapeout hygiene:** all outputs assigned; unused inputs in the
  `_unused` wire; `info.yaml` `source_files` and `test/Makefile`
  `PROJECT_SOURCES` kept in sync; pins documented in `info.yaml`.
- **Do not edit `src/config.json`** except `CLOCK_PERIOD` and
  `PL_TARGET_DENSITY_PCT`, and only with a DECISIONS entry. Never edit the
  jobs in `.github/workflows/*.yaml` from the template; add new workflow
  files instead.
- **Hardening runs on GitHub, not locally.** A `gds` run can take hours; a
  push that touches `src/`, `info.yaml` or `macro/` starts one. Docs and
  tool commits should not. Before submission, run `gds` by hand on the
  exact commit being submitted.
- **Commits:** small, one topic, message says what was verified. Do not push
  unless asked.
- **End every session green:** `python3 -m pytest tools/ -q` and
  `cd test && make` pass, generated files are fresh, `WORKLOG.md` has a
  dated entry, `docs/HANDOFF.md` says what the next session does.

## Commands

```sh
python3 -m pytest tools/ -q                 # assembler, ISS, firmware on the ISS (seconds)
cd test && make                             # cocotb: host interface + lockstep (about 1 min)
cd test && make 'COCOTB_TEST_FILTER=test_lockstep_uart_loopback'   # one test
cd test && make DUMP=1                      # also writes tb.fst for a waveform viewer
python3 tools/keyerasm.py fw/uart.s -l /tmp/uart.lst   # assemble with a listing
cd synth && yosys -q run_sram.ys            # area estimate (see synth/README.md for the liberty path)
cd formal && yowasp-sby -f pins.sby && yowasp-sby -f fifo.sby && ./run_core_pdr.sh
```

`docs/SETUP.md` has the macOS install steps.

## Compact instructions

When compacting, keep: the current task from `docs/HANDOFF.md`, any DECISIONS
entries written this session, the list of files changed, and test results.
Re-read `CLAUDE.md` afterwards.
