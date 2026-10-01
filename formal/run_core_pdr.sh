#!/bin/sh
# Proves the loom_core properties with abc's PDR engine. yowasp-sby's result
# parser crashes on the abc engine (tool bug), so sby is used only to build
# the AIGER model and abc is run directly. Expect "Property proved."
set -e
cd "$(dirname "$0")"
rm -rf core_pdr
timeout 300 yowasp-sby -f core_pdr.sby >/dev/null 2>&1 || true
test -s core_pdr/model/design_aiger.aig || { echo "model build failed"; exit 1; }
yosys-abc -c 'read_aiger core_pdr/model/design_aiger.aig; fold; strash; pdr' | grep -E "proved|failed|Output"
