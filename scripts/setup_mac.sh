#!/bin/bash
# One-shot toolchain setup for macOS (Apple Silicon or Intel). Safe to re-run.
# Installs: git, gh, Claude Code, Icarus Verilog, Verilator, Yosys, z3,
# cocotb, pytest, yowasp-yosys (SymbiYosys for the formal proofs).
set -e
if ! command -v brew >/dev/null; then
  echo "Homebrew is missing. Install it first: https://brew.sh"; exit 1
fi
brew install git gh icarus-verilog verilator yosys z3
if ! command -v claude >/dev/null; then
  curl -fsSL https://claude.ai/install.sh | bash
fi
python3 -m pip install --user --upgrade cocotb pytest yowasp-yosys
echo
echo "Versions:"
for t in git gh claude iverilog verilator yosys z3; do
  printf "  %-10s %s\n" "$t" "$(command -v $t >/dev/null && ($t --version 2>&1 | head -1) || echo MISSING)"
done
python3 -c "import cocotb, sys; print('  cocotb    ', cocotb.__version__)"
command -v yowasp-sby >/dev/null && echo "  yowasp-sby ok" || echo "  yowasp-sby MISSING (check that pip's --user bin dir is on PATH)"
echo
echo "Next: gh auth login, then bash scripts/check_all.sh"
