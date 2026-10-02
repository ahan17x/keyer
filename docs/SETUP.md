# Setting up on macOS (Apple Silicon)

Everything runs locally except the chip layout (GDS), which runs in GitHub
Actions on every push that touches `src/`, `info.yaml` or `macro/`.

## 1. Tools

```sh
# Homebrew, git, GitHub CLI
brew install git gh
gh auth login

# Claude Code (native installer; or: brew install --cask claude-code)
curl -fsSL https://claude.ai/install.sh | bash

# Simulators and synthesis
brew install icarus-verilog verilator yosys

# Python side: cocotb (RTL tests), pytest, yowasp (SymbiYosys + a current Yosys for formal), z3
python3 -m pip install cocotb pytest yowasp-yosys
brew install z3
```

Alternative to the brew packages: the OSS CAD Suite
(https://github.com/YosysHQ/oss-cad-suite-build, darwin-arm64 build) bundles
yosys, sby, iverilog, verilator, z3 and more; `source <suite>/environment`
puts them on PATH.

Optional: Surfer or GTKWave to view `test/tb.fst` (`make DUMP=1`).
Docker and LibreLane are not needed; hardening runs on GitHub.

## 2. Repo

```sh
unzip loom.zip && cd loom          # or git clone your GitHub repo
python3 -m pytest tools/ -q        # 28 tests, seconds
cd test && make && cd ..           # 9 cocotb tests, about a minute
```

Create the GitHub repo (public; the competition requires open source), push,
and enable Actions and Pages. The `test` workflow runs the cocotb suite; the
`gds` workflow hardens the design.

## 3. Claude Code

```sh
cd loom
claude
```

`CLAUDE.md` is read automatically at the start of every session and points at
`docs/HANDOFF.md`, which carries the project state between sessions. The
kickoff prompt for the first session is at the bottom of `docs/HANDOFF.md`.
`claude --continue` resumes the last session in this directory; `/compact`
summarises a long session (CLAUDE.md has compact instructions).

Subagents in `.claude/agents/` enforce that whoever writes the golden model
cannot read the RTL and vice versa. Ask for them by name ("use the
golden-model subagent to ..."). Check `/permissions` once to confirm the
deny rules took effect.

## 4. Session habits that worked for the parallel entry

- Start every session by reading the handoff, not by re-explaining in chat.
- One topic per session; end green; log the session in `WORKLOG.md`.
- Every bug in `docs/BUGS.md`, every decision in `docs/DECISIONS.md`.
- Hardware changes are expensive (hours of hardening); batch them.
