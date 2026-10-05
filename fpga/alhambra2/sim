#!/bin/bash
# Runs the host-interface and pads-only cocotb tests on the post-synthesis
# iCE40 netlist (see Makefile). Exit status is non-zero if a test fails.
set -euo pipefail
cd "$(dirname "$0")"
rm -f build/sim/results.xml
make sim > build/sim.log 2>&1 || { tail -30 build/sim.log; exit 1; }
grep -E "\*\* test|TESTS=" build/sim.log
! grep -q "<failure" build/sim/results.xml
