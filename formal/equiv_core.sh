#!/bin/bash
# Sequential equivalence of keyer_core against a git reference, with Yosys.
#
#   formal/equiv_core.sh [GIT_REF [GATE_DIR]]
#
# gold: src/keyer_core.v and src/keyer_isa.vh at GIT_REF (default master),
#       extracted with git show into a temporary directory;
# gate: the same two files in GATE_DIR (default: the working tree's src/).
# Both are read without -DFORMAL, with NTHREADS at its default (2).
#
# What is proved: the two cores have the same state (the same flops, by
# name: the script fails if the lists differ) and, from equal state and
# equal inputs, every flop input and every output is equal. Internal wire
# names are hidden on both sides first, so only state and ports are
# matched: internal signals may legitimately differ where nothing
# observable depends on them. equiv_make puts an $equiv cell on every
# matched bit (output ports and flop outputs; a flop output's $equiv is
# proved through its input one cycle earlier), equiv_simple proves what it
# can from the matched signals, equiv_induct the rest by induction over
# all of them, and equiv_status -assert fails on any unproven bit.
#
# Prints "proven P of T" and exits non-zero unless P = T and T > 0.
# Run from anywhere; needs git and yosys (or yowasp-yosys). EQUIV_KEEP=1
# keeps the temporary directory (Yosys scripts and logs) and prints its path.
set -u
REF=${1:-master}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
GATE=${2:-$ROOT/src}
if command -v yosys >/dev/null 2>&1; then YOSYS=yosys
elif command -v yowasp-yosys >/dev/null 2>&1; then YOSYS=yowasp-yosys
else echo "equiv_core: no yosys or yowasp-yosys on PATH"; exit 2
fi
TMP=$(mktemp -d "${TMPDIR:-/tmp}/equiv_core.XXXXXX") || exit 2
if [ -n "${EQUIV_KEEP:-}" ]; then echo "equiv_core: work directory $TMP"
else trap 'rm -rf "$TMP"' EXIT
fi
mkdir "$TMP/gold" "$TMP/gate"

for f in keyer_core.v keyer_isa.vh; do
  git -C "$ROOT" show "$REF:src/$f" > "$TMP/gold/$f" 2>"$TMP/git.err" ||
    { echo "equiv_core: cannot read src/$f at '$REF':"; cat "$TMP/git.err"; exit 2; }
  cp "$GATE/$f" "$TMP/gate/$f" || { echo "equiv_core: no $GATE/$f"; exit 2; }
done

# One Yosys run per side: the two copies include different keyer_isa.vh
# files behind the same include guard, so they cannot share a session.
# The state list is written before the names are hidden.
for side in gold gate; do
  cat > "$TMP/$side.ys" <<EOF
read_verilog -mem2reg -I$TMP/$side $TMP/$side/keyer_core.v
hierarchy -check -top keyer_core
proc
opt_clean
tee -q -o $TMP/$side.state select -list t:\$*dff* %co:+[Q] w:* %i
select -set keep x:* t:\$*dff* %co:+[Q] w:* %i %u
rename -hide w:* @keep %d
opt_clean
rename keyer_core $side
write_rtlil $TMP/$side.il
EOF
  "$YOSYS" -q -l "$TMP/$side.log" -s "$TMP/$side.ys" >/dev/null 2>&1 ||
    { echo "equiv_core: Yosys failed on the $side side:"; tail -20 "$TMP/$side.log"; exit 2; }
done

if ! diff "$TMP/gold.state" "$TMP/gate.state" >"$TMP/state.diff"; then
  echo "equiv_core: the state differs between $REF (<) and $GATE (>):"
  cat "$TMP/state.diff"
  echo "proven 0 of 0 (state mismatch)"
  exit 1
fi

cat > "$TMP/equiv.ys" <<EOF
read_rtlil $TMP/gold.il
read_rtlil $TMP/gate.il
equiv_make gold gate equiv
hierarchy -top equiv
opt_clean
equiv_simple -seq 2
equiv_induct -seq 2
tee -o $TMP/status.txt equiv_status
equiv_status -assert
EOF
start=$(date +%s)
"$YOSYS" -q -l "$TMP/equiv.log" -s "$TMP/equiv.ys" >/dev/null 2>&1
rc=$?
secs=$(( $(date +%s) - start ))

line=$(grep -E "Of those cells [0-9]+ are proven and [0-9]+ are unproven" "$TMP/status.txt" | head -1)
total=$(grep -oE "Found [0-9]+ \\\$equiv cells" "$TMP/status.txt" | grep -oE "[0-9]+" | head -1)
proven=$(echo "$line" | grep -oE "[0-9]+ are proven" | grep -oE "[0-9]+")
nstate=$(wc -l < "$TMP/gold.state" | tr -d ' ')
if [ -z "$total" ] || [ -z "$proven" ]; then
  echo "equiv_core: no equiv_status result (Yosys exit $rc); last lines of the log:"
  tail -20 "$TMP/equiv.log"
  exit 2
fi
echo "keyer_core at $REF vs $GATE: $nstate state signals matched by name"
echo "proven $proven of $total (equivalence points: output and flop bits), ${secs} s"
if [ "$proven" != "$total" ] || [ "$total" -eq 0 ] || [ $rc -ne 0 ]; then
  echo "UNPROVEN points:"
  grep -E "Unproven \\\$equiv|^ *Unproven" "$TMP/status.txt" | head -40
  exit 1
fi
echo "EQUIVALENT"
