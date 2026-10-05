#!/bin/bash
# Directed serializer testbench (no golden model): bash test/ser_unit/run.sh
set -eu
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd); B="$HERE/build"
mkdir -p "$B"
for f in "$HERE"/p*.s; do python3 "$ROOT/tools/keyerasm.py" "$f" -o "$B/$(basename "${f%.s}").hex" >/dev/null; done
S="$ROOT/src"
iverilog -g2005 -DKEYER_IMEM_FLOPS -I "$S" -o "$B/tb_ser_unit.vvp" "$S/keyer_fifo.v" "$S/keyer_imem.v" \
  "$S/keyer_pins.v" "$S/keyer_core.v" "$S/keyer_host.v" "$S/keyer_capture.v" "$S/keyer_ser.v" \
  "$S/tt_um_ahan17x_keyer.v" "$HERE/tb_ser_unit.v"
cd "$B" && vvp -n tb_ser_unit.vvp | tee sim.log
grep -q "^SER_UNIT PASS" sim.log
