#!/bin/sh
# Runs the serializer proofs of ser.sby and prints one line per task:
#   SER <task> PASS | FAIL | UNKNOWN | ERROR
# Exit status 0 only if every task passes. With arguments, only those tasks.
#
# The two unbounded tasks (stuff, crc_any) use abc's PDR engine. sby calls it
# with a switch that older abc builds do not know (the GitHub runner's: the
# engine prints its usage and sby reports UNKNOWN), so, as in
# run_core_pdr.sh, sby only builds their AIGER model and abc is run
# directly. sby is told to use the Yosys it ships with: with an older system
# Yosys its abc engine crashes on the witness map (BUGS 44).
cd "$(dirname "$0")"
ALL="stuff crc_any crc16 crc32 rt_nrzi rt_manch stuff_cover crc_cover crc_any_cover rt_cover"
TASKS="${*:-$ALL}"
PDR=""; SBY=""
for t in $TASKS; do
  rm -rf "ser_$t"
  case $t in stuff|crc_any) PDR="$PDR $t";; *) SBY="$SBY $t";; esac
done
status=0
if [ -n "$SBY" ]; then
  yowasp-sby --yosys yowasp-yosys -f ser.sby $SBY >/dev/null 2>&1
  for t in $SBY; do
    v=$(grep -o "DONE ([A-Z]*" "ser_$t/logfile.txt" 2>/dev/null | tail -1 | sed "s/DONE (//")
    echo "SER $t ${v:-ERROR}"
    if [ "$v" != PASS ]; then
      status=1
      grep -v -E "engine_0: +[0-9]+ \+ :" "ser_$t/logfile.txt" 2>/dev/null | tail -15
    fi
  done
fi
for t in $PDR; do
  yowasp-sby --yosys yowasp-yosys -f ser.sby "$t" >/dev/null 2>&1 || true
  if [ ! -s "ser_$t/model/design_aiger.aig" ]; then echo "SER $t ERROR"; echo "model build failed"; status=1; continue; fi
  res=$(yosys-abc -c "read_aiger ser_$t/model/design_aiger.aig; fold; strash; pdr" 2>&1) || true
  if echo "$res" | grep -q "Property proved"; then echo "SER $t PASS"
  elif echo "$res" | grep -q -E "was asserted|Property failed"; then echo "SER $t FAIL"; echo "$res" | grep -E "asserted|failed" | head -3; status=1
  else echo "SER $t UNKNOWN"; echo "$res" | tail -5; status=1
  fi
done
exit $status
