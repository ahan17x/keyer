#!/bin/bash
# Runs every check. `quick` skips the slow cocotb suite and formal proofs.
# Exit status is non-zero if anything fails.
set -u
cd "$(dirname "$0")/.."
# the repo's virtualenv (scripts/setup_mac.sh) provides cocotb, pytest, yowasp-sby
if [ -d .venv ]; then
  export PATH="$PWD/.venv/bin:$PATH"
  # A venv's launchers carry the absolute path they were created at; after a
  # move of the repo they fail with "bad interpreter" (docs/SETUP.md).
  if ! .venv/bin/cocotb-config --version >/dev/null 2>&1; then
    echo ".venv is unusable (repo moved, or packages missing). Run: bash scripts/setup_mac.sh"
    exit 1
  fi
fi
status=0
run() { echo; echo "== $1"; shift; "$@" || { echo "FAILED: $*"; status=1; }; }

run "ISA table self-check and header freshness" bash -c '
  python3 tools/keyer_isa.py >/dev/null &&
  python3 tools/keyer_isa.py --vh | diff -q - src/keyer_isa.vh >/dev/null ||
  { echo "src/keyer_isa.vh is stale: run python3 tools/keyer_isa.py --vh > src/keyer_isa.vh"; exit 1; }'
run "Python tests (assembler, ISS, firmware on the ISS)" python3 -m pytest tools/ -q
run "Firmware assembles" bash -c 'for f in fw/*.s; do python3 tools/keyerasm.py "$f" -o /dev/null >/dev/null || exit 1; done; echo ok'
run "Verilator lint" verilator --lint-only -Wall -Wno-DECLFILENAME -Wno-UNUSEDSIGNAL -Isrc src/RM_IHPSG13_1P_256x16_c2_bm_bist.v src/keyer_fifo.v src/keyer_imem.v src/keyer_pins.v src/keyer_core.v src/keyer_host.v src/keyer_capture.v src/tt_um_ahan17x_keyer.v --top-module tt_um_ahan17x_keyer
run "Icarus compile" iverilog -g2005 -I src -o /dev/null src/RM_IHPSG13_1P_256x16_c2_bm_bist.v src/keyer_fifo.v src/keyer_imem.v src/keyer_pins.v src/keyer_core.v src/keyer_host.v src/keyer_capture.v src/tt_um_ahan17x_keyer.v

if [ "${1:-}" != "quick" ]; then
  run "cocotb (host interface + lockstep)" bash -c 'cd test && rm -f results.xml && make clean >/dev/null 2>&1; make > make.log 2>&1; grep -E "\*\* test|TESTS=" make.log; if [ ! -f results.xml ]; then echo "make did not produce results.xml; last lines of make.log:"; tail -25 make.log; exit 1; fi; grep -q "<failure" results.xml && exit 1; [ "$(grep -o "<testcase " results.xml | wc -l | tr -d " ")" = "32" ]'
  if command -v yowasp-sby >/dev/null; then
    run "formal: pins" bash -c 'cd formal && rm -rf pins && yowasp-sby -f pins.sby 2>&1 | grep -E "DONE"'
    run "formal: fifo" bash -c 'cd formal && rm -rf fifo_prove fifo_bmc && yowasp-sby -f fifo.sby 2>&1 | grep -E "DONE" && grep -q DATA_CHECK fifo_bmc/model/design.ys || { echo "DATA_CHECK define did not reach the BMC model (vacuous proof)"; exit 1; }'
    run "formal: core (abc pdr)" bash -c 'cd formal && ./run_core_pdr.sh'
    run "formal: capture and replay (prove + cover)" bash -c 'cd formal && rm -rf capture_prove capture_cover && out=$(yowasp-sby -f capture.sby 2>&1); echo "$out" | grep -E "DONE"; [ "$(echo "$out" | grep -c "DONE (PASS")" -eq 2 ]'
  else
    echo "(formal skipped: yowasp-sby not installed)"
  fi
fi
echo
[ $status -eq 0 ] && echo "ALL CHECKS PASSED" || echo "SOME CHECKS FAILED"
exit $status
