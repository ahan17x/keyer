#!/bin/sh
# Proves the loom_core properties with abc's PDR engine. Some yowasp-sby
# versions crash in their result parser on the abc engine (tool bug), so sby
# is used only to build the AIGER model and abc is run directly. Expect
# "Property proved."
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
yosys-abc -c 'read_aiger core_pdr/model/design_aiger.aig; fold; strash; pdr' | grep -E "proved|failed|Output"
