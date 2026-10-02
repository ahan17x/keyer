# Setting up on macOS (Apple Silicon)

Everything runs locally except the chip layout (GDS), which runs in GitHub
Actions on every push that touches `src/`, `info.yaml` or `macro/`.

## 1. Tools

The one-shot script installs everything and is safe to re-run:

```sh
bash scripts/setup_mac.sh
```

What it does, if you prefer to do it by hand:

```sh
# Homebrew, git, GitHub CLI
brew install git gh
gh auth login

# Claude Code (native installer; or: brew install --cask claude-code)
curl -fsSL https://claude.ai/install.sh | bash

# Simulators, synthesis, SMT solver, a current Python
brew install icarus-verilog verilator yosys z3 python@3.13

# Python side, in a virtualenv inside the repo: cocotb (RTL tests), pytest,
# yowasp-yosys (SymbiYosys + a current Yosys for the formal proofs)
"$(brew --prefix python@3.13)/bin/python3.13" -m venv .venv
.venv/bin/pip install --upgrade pip cocotb pytest yowasp-yosys
```

`scripts/check_all.sh` and `test/Makefile` put `.venv/bin` on PATH
themselves; nothing needs activating.

Alternative to the brew packages: the OSS CAD Suite
(https://github.com/YosysHQ/oss-cad-suite-build, darwin-arm64 build) bundles
yosys, sby, iverilog, verilator, z3 and more; `source <suite>/environment`
puts them on PATH.

Optional: Surfer or GTKWave to view `test/tb.fst` (`make DUMP=1`).
Docker and LibreLane are not needed; hardening runs on GitHub.

Versions known to pass the full check suite (2026-10-02): Python 3.13.8,
cocotb 2.1.0, pytest 9.1.1, yowasp-yosys 0.69.0.0.post1233, Icarus Verilog
13.0, Verilator 5.052, Yosys 0.69, z3 5.1.0, Apple's GNU make 3.81.

## 2. Repo

```sh
git clone <your GitHub repo> && cd <repo>
bash scripts/setup_mac.sh          # tools + .venv
bash scripts/check_all.sh          # every check, about two minutes
bash scripts/check_all.sh quick    # skips cocotb and formal, seconds
```

Create the GitHub repo (public; the competition requires open source), push,
and enable Actions and Pages. The `test` workflow runs the cocotb suite; the
`gds` workflow hardens the design.

## 3. Pitfalls seen on this Mac (2026-10-02)

All found by `bash scripts/check_all.sh` on the first Claude Code session,
after the repo had been moved from `~/Claude/loom` to `~/Desktop/loom`.

1. **The virtualenv breaks when the repo is moved.** A venv's launchers
   (`.venv/bin/cocotb-config`, `yowasp-sby`, `pytest`, `pip`) carry the
   absolute path of the Python they were created with in their `#!` line.
   After the move every one of them failed with
   `bad interpreter: /Users/.../Claude/keyer/.venv/bin/python3: no such file`.
   Symptoms: cocotb reported `make: cocotb-config: Command not found` and
   `Makefile.sim: No such file or directory`; all three formal checks
   failed, the FIFO one with the misleading "DATA_CHECK define did not reach
   the BMC model" (the guard fires because the run never happened). The
   Python tests still passed because `.venv/bin/python3` is a symlink and
   `python3 -m pytest` does not go through a launcher. Fix: delete `.venv`
   and recreate it. `scripts/setup_mac.sh` now detects the stale path and
   does this itself, and `scripts/check_all.sh` fails fast with a clear
   message instead of three confusing ones.
2. **macOS has no `timeout`.** `formal/run_core_pdr.sh` wrapped sby in
   `timeout 300 ...`; the command was not found, the `|| true` swallowed
   it, and the script reported "model build failed". It now uses `timeout`
   or coreutils' `gtimeout` when present and runs without one otherwise.
3. **`python3` is Apple's 3.9.6.** In the shell Claude Code gets,
   `/opt/homebrew/bin` sits at the end of PATH, so `python3` resolves to
   `/usr/bin/python3` (Python 3.9, end of life). The original venv had been
   built on it. The setup script now uses `brew --prefix python@3.13`
   explicitly.
4. Apple's GNU make 3.81 is fine for cocotb 2.1's makefiles; no `gmake`
   needed. The current yowasp-sby also handles the `abc pdr` engine without
   the parser crash noted in `formal/README.md`; the script keeps calling
   abc directly, which works either way.
5. **`formal/fifo_props.sv` was missing from the repo.** Commit 0b2d0af
   (the BUGS 10 fix) deleted it while adding a generated
   `formal/fifo/status.sqlite`, so the FIFO proof could not run at all and
   failed with the same misleading DATA_CHECK message as pitfall 1. Restored
   from commit 090f90e; both FIFO tasks pass (BUGS.md row 11). Lesson:
   "green at hand-off" must mean green on a fresh checkout of the commit.
6. Two generated files had been committed by mistake (`formal/fifo/status.sqlite`,
   an empty `formal/core_pdr.aig`); removed. `.DS_Store` and `formal/fifo/`
   (sby's status database) are now ignored.

## 4. Claude Code

```sh
cd <repo>
claude
```

`CLAUDE.md` is read automatically at the start of every session and points at
`docs/HANDOFF.md`, which carries the project state between sessions. The
kickoff prompt for the first session is in `docs/KICKOFF_PROMPT.md`.
`claude --continue` resumes the last session in this directory; `/compact`
summarises a long session (CLAUDE.md has compact instructions).

Subagents in `.claude/agents/` enforce that whoever writes the golden model
cannot read the RTL and vice versa. Ask for them by name ("use the
golden-model subagent to ..."). Check `/permissions` once to confirm the
deny rules took effect.

## 5. Session habits that worked for the parallel entry

- Start every session by reading the handoff, not by re-explaining in chat.
- One topic per session; end green; log the session in `WORKLOG.md`.
- Every bug in `docs/BUGS.md`, every decision in `docs/DECISIONS.md`.
- Hardware changes are expensive (hours of hardening); batch them.
