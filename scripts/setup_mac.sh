#!/bin/bash
# One-shot toolchain setup for macOS. Safe to re-run.
# Installs: git, gh, Claude Code, Icarus Verilog, Verilator, Yosys, z3,
# Homebrew Python 3.13, and a Python virtualenv in the repo (.venv) with
# cocotb, pytest and yowasp-yosys (SymbiYosys for the formal proofs). The venv
# avoids macOS/Homebrew Python's refusal of system-wide pip installs and PATH
# problems with --user installs.
#
# A venv is tied to the directory it was created in: its launchers
# (.venv/bin/cocotb-config, yowasp-sby, pytest, pip) carry an absolute #! path.
# If the repo has been moved since, they fail with "bad interpreter", so the
# venv is recreated here. See docs/SETUP.md, "Pitfalls".
set -e
cd "$(dirname "$0")/.."
if ! command -v brew >/dev/null; then
  echo "Homebrew is missing. Install it first: https://brew.sh"; exit 1
fi
brew install git gh icarus-verilog verilator yosys z3 python@3.13
if ! command -v claude >/dev/null; then
  curl -fsSL https://claude.ai/install.sh | bash
fi
# Use Homebrew's Python explicitly: a bare `python3` may be Apple's 3.9 when
# /opt/homebrew/bin sits behind /usr/bin on PATH.
PY="$(brew --prefix python@3.13)/bin/python3.13"
[ -x "$PY" ] || PY=python3
if [ -d .venv ] && ! head -1 .venv/bin/pip 2>/dev/null | grep -qF "#!$PWD/.venv/bin/python"; then
  echo "Recreating .venv: it was created at a different path (repo moved?)"
  rm -rf .venv
fi
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip cocotb pytest yowasp-yosys
echo
echo "Versions:"
for t in git gh claude iverilog verilator yosys yosys-abc z3; do
  printf "  %-11s %s\n" "$t" "$(command -v $t >/dev/null && ($t --version 2>&1 | head -1) || echo MISSING)"
done
printf "  %-11s %s\n" python "$(.venv/bin/python --version)"
printf "  %-11s %s\n" cocotb "$(.venv/bin/cocotb-config --version 2>/dev/null || echo BROKEN)"
printf "  %-11s %s\n" pytest "$(.venv/bin/pytest --version 2>/dev/null || echo BROKEN)"
printf "  %-11s %s\n" yowasp-sby "$(.venv/bin/yowasp-sby --help >/dev/null 2>&1 && echo ok || echo BROKEN)"
echo
echo "Next: gh auth login, then bash scripts/check_all.sh"
