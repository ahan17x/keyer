#!/bin/bash
# Builds the Keyer bitstream for the Alhambra II (iCE40 HX4K-TQ144) and prints
# the resource and timing summary.
#
#   bash fpga/alhambra2/build.sh            # synthesis, place and route, bitstream
#   iceprog fpga/alhambra2/build/keyer.bin  # program the board (not done here)
#
# Tools: yosys, nextpnr-ice40, icepack, icetime. They are taken from PATH,
# falling back to the copies apio installs (~/.apio/packages/tools-oss-cad-suite).
# The HX4K-TQ144 is the HX8K die in a smaller package: the open flow targets
# it as `--hx8k --package tq144:4k`, which makes all 7,680 logic cells of
# the die usable (the vendor tools limit the part to 3,520).
set -euo pipefail
cd "$(dirname "$0")"
APIO_BIN="$HOME/.apio/packages/tools-oss-cad-suite/bin"
[ -d "$APIO_BIN" ] && PATH="$PATH:$APIO_BIN"
for t in yosys nextpnr-ice40 icepack icetime; do
  command -v "$t" >/dev/null || { echo "$t not found (install oss-cad-suite, or apio and 'apio install oss-cad-suite')"; exit 1; }
done

SRC=../../src
FREQ=${FREQ:-12}          # MHz, the board's oscillator
mkdir -p build

# The design sources are the ASIC's, unchanged; KEYER_IMEM_FLOPS selects the
# behavioural program memory, which maps to one SB_RAM40_4K.
yosys -q -l build/synth.log -p "
  read_verilog -DKEYER_IMEM_FLOPS -I$SRC \
    $SRC/keyer_fifo.v $SRC/keyer_imem.v $SRC/keyer_pins.v $SRC/keyer_core.v \
    $SRC/keyer_host.v $SRC/keyer_capture.v $SRC/keyer_ser.v $SRC/tt_um_ahan17x_keyer.v keyer_alhambra2.v
  synth_ice40 -top keyer_alhambra2 -json build/keyer.json
  tee -o build/stat.txt stat"

nextpnr-ice40 --hx8k --package tq144:4k --pcf alhambra2.pcf --json build/keyer.json \
  --asc build/keyer.asc --freq "$FREQ" --seed "${SEED:-1}" --report build/report.json \
  -q -l build/pnr.log

icepack build/keyer.asc build/keyer.bin
icetime -d hx8k -P tq144:4k -p alhambra2.pcf -c "$FREQ" -mtr build/timing.rpt build/keyer.asc > build/icetime.log

python3 - <<'EOF'
import json, re
r = json.load(open("build/report.json"))
u = r["utilization"]
lc, ram, io = u["ICESTORM_LC"], u["ICESTORM_RAM"], u["SB_IO"]
stat = open("build/stat.txt").read()
def cells(name):
    m = re.search(r"^\s+(?:%s)\s+(\d+)\s*$|^\s+(\d+)\s+(?:%s)\s*$" % (name, name), stat, re.M)
    return int(next(g for g in m.groups() if g)) if m else 0
luts = cells("SB_LUT4")
ffs = sum(int(a or b) for a, b in re.findall(r"^\s+SB_DFF\w*\s+(\d+)\s*$|^\s+(\d+)\s+SB_DFF\w*\s*$", stat, re.M))
print("Synthesis: %d LUT4, %d flip-flops, %d carry cells, %d block RAM" % (
    luts, ffs, cells("SB_CARRY"), cells("SB_RAM40_4K")))
print("Placed:    %d of %d logic cells (%.0f%% of the die; the HX4K is sold as 3,520, of which this is %.0f%%)" % (
    lc["used"], lc["available"], 100.0 * lc["used"] / lc["available"], 100.0 * lc["used"] / 3520))
print("           %d of %d block RAMs, %d of %d I/O" % (ram["used"], ram["available"], io["used"], io["available"]))
for clk, f in r["fmax"].items():
    print("Timing:    %s: %.1f MHz achieved, %.1f MHz required (nextpnr)" % (clk.split("$")[0], f["achieved"], f["constraint"]))
m = re.search(r"Timing estimate: ([\d.]+) ns \(([\d.]+) MHz\)", open("build/icetime.log").read())
if m:
    print("           icetime: %s ns, %s MHz" % (m.group(1), m.group(2)))
EOF
echo "Bitstream: fpga/alhambra2/build/keyer.bin"
