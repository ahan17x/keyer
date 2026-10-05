#!/bin/sh
# Proves the keyer_core properties with abc's PDR engine. Some yowasp-sby
# versions crash in their result parser on the abc engine (tool bug), so sby
# is used only to build the AIGER model and abc is run directly. Prints abc's
# verdict ("Property proved." or "Output N of miter ... was asserted in frame
# F." for a counterexample) and exits non-zero unless the property is proved.
set -e
cd "$(dirname "$0")"
rm -rf core_pdr
# macOS ships no `timeout`; use coreutils' gtimeout if present, else none.
if command -v timeout >/dev/null 2>&1; then TIMEOUT="timeout 300"
elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT="gtimeout 300"
else TIMEOUT=""
fi
$TIMEOUT yowasp-sby -f core_pdr.sby >/dev/null 2>&1 || true
test -s core_pdr/model/design_aiger.aig || { echo "model build failed"; exit 1; }
out=$(yosys-abc -c 'read_aiger core_pdr/model/design_aiger.aig; fold; strash; pdr' 2>&1) || true
echo "$out" | grep -E "proved|failed|Output" || echo "$out" | tail -5
echo "$out" | grep -q "Property proved" || { echo "core PDR: NOT PROVED"; exit 1; }
