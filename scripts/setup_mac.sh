#!/bin/bash
# One-shot toolchain setup for macOS. Safe to re-run.
# Installs: git, gh, Claude Code, Icarus Verilog, Verilator, Yosys, z3, and a
# Python virtualenv in the repo (.venv) with cocotb, pytest and yowasp-yosys
# (SymbiYosys for the formal proofs). The venv avoids macOS/Homebrew Python's
# refusal of system-wide pip installs and PATH problems with --user installs.
set -e
cd "$(dirname "$0")/.."
if ! command -v brew >/dev/null; then
  echo "Homebrew is missing. Install it first: https://brew.sh"; exit 1
fi
brew install git gh icarus-verilog verilator yosys z3
if ! command -v claude >/dev/null; then
  curl -fsSL https://claude.ai/install.sh | bash
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip cocotb pytest yowasp-yosys
echo
echo "Versions:"
for t in git gh claude iverilog verilator yosys z3; do
  printf "  %-11s %s\n" "$t" "$(command -v $t >/dev/null && ($t --version 2>&1 | head -1) || echo MISSING)"
done
printf "  %-11s %s\n" cocotb "$(.venv/bin/python -c 'import cocotb; print(cocotb.__version__)')"
printf "  %-11s %s\n" yowasp-sby "$( [ -x .venv/bin/yowasp-sby ] && echo ok || echo MISSING)"
echo
echo "Next: gh auth login, then bash scripts/check_all.sh"
