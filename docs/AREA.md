# Area and hardening log

Numbers from Yosys against the IHP CMOS5L typical liberty
(`sg13cmos5l_stdcell_typ_1p20V_25C.lib`, flop with reset 48.99 um^2), scripts
in `synth/` (`run.ys` from `synth.ys`, `run_sram_lib.ys` from `run_sram.ys`,
`core_only.ys` with NTHREADS substituted). Area is standard-cell area before
placement. Budget (PLAN.md section 4): the 6x4 block core is 902,417 um^2;
at the template's 60% placement density about 430,000 um^2 of cells fit.
Hardening results from the GitHub `gds` workflow are appended below.

The block's dimensions used before the first hardening run (6x4 block
1289.28 x 710.64 um, 916,214 um^2, core 902,417 um^2; signal routing on
Metal1 to Metal4 only; an IHP SRAM macro allowed on the shuttle, confirmed
by the organisers by email on 2026-09-28) were taken from the published
research notes of the parallel entry thomasgilbert481/tt_um_loom
(`docs/tt_cmos5l_facts.md` in that repository, Apache-2.0); every run below
confirms them from the flow's own `resolved.json` and `metrics.json`.

## Synthesis

| Date | Commit | What | Cells | Flops | Area (um^2) |
|---|---|---|---|---|---|
| 2026-10-01 | 090f90e | whole design, program memory as flops (before D-018) | 18,956 | 5,302 | 429,092 |
| 2026-10-01 | 090f90e | whole design, macro black-boxed (before D-018) | 6,410 | 1,190 | 114,169 |
| 2026-10-02 | d979e82 | whole design, program memory as flops | 21,210 | 5,365 | 433,110 |
| 2026-10-02 | d979e82 | whole design, program memory as the RM_IHPSG13_1P_256x16 macro (black box; add 28,127 um^2 for the macro) | 6,948 | 1,253 | 121,796 |
| 2026-10-02 | d979e82 | keyer_core alone, NTHREADS = 2 | 3,825 | 459 | 55,691 |
| 2026-10-02 | d979e82 | keyer_core alone, NTHREADS = 4 | 6,132 | 900 | 94,265 |
| 2026-10-02 | 6e02d4b | whole design with capture and replay, macro black-boxed (add 28,127 um^2 for the macro) | 8,336 | 1,463 | 142,277 |
| 2026-10-02 | 6e02d4b | whole design with capture and replay, program memory as flops | 24,559 | 5,575 | 450,369 |
| 2026-10-02 | 6e02d4b | keyer_capture alone (rtl subagent's run) | 1,152 | 203 | 18,548 |
| 2026-10-03 | 399a889 (`decode-onehot`) | whole design, macro black-boxed, registered one-hot thread select replicated per consumer | 8,182 | 1,470 | 141,004 |
| 2026-10-03 | 399a889 (`decode-onehot`) | keyer_core alone, NTHREADS = 2 (before: 3,866 cells, 459 flops, 55,735 um^2 on `sram-macro`) | 3,872 | 466 | 55,576 |

Capture and replay (D-024) cost 210 flops and 20,481 um^2; with the macro the
design is 170,404 um^2, 18.9% of the core (limit for the feature: 25%).
`decode-onehot` (D-028; equivalence with the `sram-macro` core proven on all
776 points): 154 fewer cells, 7 more flops, 1,273 um^2 less. The register
file is reached in 26 logic levels instead of 28 and its path no longer
starts at the thread decode (`cyc[0]`) but at the instruction word; the
link register and timer period drop from 27 levels to 9 and 7. Rough delay
estimates for the register-file path at the typical corner (no STA tool
locally): 13.9 ns to 11.3 ns with area mapping, 8.8 ns to 7.8 ns with delay
mapping. Not hardened yet.

The D-018 timer and timeouts cost 63 flops and about 7,600 um^2 over the
pre-D-018 core; the whole logic sits at 28% of the placeable area with the
macro (121,796 + 28,127 = 149,923 um^2 of 430,000). NTHREADS = 4 adds 441
flops and 38,600 um^2 to the core.

## Hardening (GitHub `gds` workflow)

### Run 37073185698, 2026-10-02/03, commit d979e82 (master, before capture and replay; program memory as flops)

Dispatched by hand. Jobs: `gds` success (3 h 00 min), `precheck` success
(2 h 17 min), `viewer` success, `gl_test` failure (see below). Numbers from
`runs/wokwi/final/metrics.json` of the `GDS_logs` artifact.

| Item | Value |
|---|---|
| Standard cells | 37,821 instances (5,365 flops, 21,944 combinational, 9,329 timing-repair buffers of which 5,548 hold buffers, 708 clock buffers and inverters, 188 antenna cells); 78,988 with fill |
| Cell area | 603,493 um^2 of the 902,417 um^2 core |
| Utilisation | 66.9% (the program memory as flops is about half of it) |
| Setup slack, 20 ns | fast (1.32 V, -40 C) **+11.46 ns**; typical (1.20 V, 25 C) **+7.09 ns**; slow (1.08 V, 125 C) **-0.59 ns, 12 violating endpoints** |
| Hold slack | fast +0.109 ns, typical +0.298 ns, slow +0.622 ns; no violations |
| Routing | detailed routing 1 h 59 min 48 s; 67 violations at iteration 0, 0 at iteration 5; wire length 1,554,847 um |
| Other long steps | Magic DRC 44 min 52 s; everything else under 2 min each |
| DRC | routing DRC 0; Magic DRC 0; illegal overlaps 0 (KLayout DRC is skipped by the template; the precheck runs it) |
| LVS | 0 errors |
| Antenna | 0 violating nets, 0 violating pins |
| Slew / cap / fanout | 33 max-slew violations (slow corner only), 2 max-cap, 378 max-fanout (reported, not fatal) |
| Clock skew (setup) | 0.34 ns fast, 0.36 typical, 0.41 slow |
| IR drop / power | worst IR drop 0.2 mV; 19.4 mW total at the typical corner |
| Precheck | all nine checks pass: KLayout SG13CMOS5L DRC, pin label overlap, zero area, KLayout checks, pin check, boundary check, layer check, cell name check, analog pin check |
| gl_test | **failed to compile**: `sg13cmos5l_stdcell.v` instantiates primitives (`ihp_mux2`, ...) defined in `sg13cmos5l_udp.v`, which the template's `test/Makefile` does not list. Fixed in the test configuration (the udp file added, a pads-only test mode for gate level); verified locally against this run's netlist |

**Timing fails at 20 ns in the slow corner.** Critical path (slow corner,
`55-openroad-stapostpnr/nom_slow_1p08V_125C/max.rpt`), data arrival 21.24 ns
against a required 20.65 ns:

1. Start: the instruction register, i.e. the program memory's read-data flop
   (`dbg_ir[13]`, a major-opcode bit; `dbg_ir[15]` for the four smallest
   violations), clock-to-Q 0.59 ns.
2. Major-opcode decode: a `nand4` into a `nor4` on high-fanout nets driven by
   size-1 cells, 1.6 to 2.1 ns slews, about 4 ns together.
3. Execute: sub-opcode decode and the ALU with its result selection, about
   6.5 ns (a chain of `a221oi`/`o21ai`/`xnor2`).
4. Register-file write: write-data and write-enable distribution through
   three fanout buffers, a `nand4` (1.66 ns), an `and2` and two more buffers,
   about 6.5 ns.
5. End: `u_core.regs[15][*]` (thread 1's r7); all 12 violating endpoints are
   bits of that register.

It is the single-cycle fetch-register to decode to execute to write-back
path, slowed by weak drivers on high-fanout decode and write-enable nets at
66.9% utilisation. `CLOCK_PERIOD` was not changed (Ahan's instruction);
options are listed in docs/HANDOFF.md for his decision.

### Run 37169889955, 2026-10-04, commit de17304 (`sram-macro`: program memory as the RM_IHPSG13_1P_256x16 macro, with capture and replay)

Dispatched by hand (D-028). Jobs: `gds` success (1 h 33 min), `gl_test`
**success**, `viewer` success, `precheck` success (34 min). Numbers from
`scripts/gds_report.py 37169889955`.

| Item | Value |
|---|---|
| Standard cells | 13,203 instances (1,463 flops, 2,876 timing-repair buffers) plus the macro; 72,012 with fill |
| Cell area | 194,522 um^2 of standard cells + 28,127 um^2 macro, of the 902,417 um^2 core |
| Utilisation | 24.7% (standard cells alone 22.2%); the flop-memory run was 66.9% |
| Setup slack, 20 ns | fast **+11.20 ns**; typical **+6.40 ns** (the sign-off corner, D-028); slow **-1.94 ns, 277 violating endpoints** |
| Hold slack | fast +0.092 ns, typical +0.270 ns, slow +0.593 ns; no violations |
| Routing | detailed routing 48 min 49 s; 55 violations at iteration 0, 0 at the end; wire length 560,364 um (1,554,847 um with the flop memory) |
| Other long steps | Magic DRC 34 min 31 s |
| DRC | routing DRC 0. Magic DRC 29,294 boxes, **every one inside the macro's bounding box** (x 12..248.8, y 40..158.78; SRAM rules Magic's cmos5l tech does not know; waived by `ERROR_ON_MAGIC_DRC`, the sign-off DRC is the precheck's KLayout deck). 10 "illegal overlap between obsm4 and metal4" boxes, all on the four POWER stripes (x 28.19, 95.63, 163.07, 230.51) within the macro's VDD!/VDDARRAY! split band (y 113.575..119.695): the one case `ERROR_ON_ILLEGAL_OVERLAPS` waives |
| PDN | the verifier in `src/pdn_cfg.tcl` passed: 4 VPWR stripes inside VDD!/VDDARRAY! columns, 4 VGND stripes inside VSS! columns, every macro supply carries 4 stripes, `check_power_grid` clean on both nets |
| LVS | 0 errors |
| Antenna | 0 violating nets, 0 violating pins |
| Slew / cap / fanout | 14 max-slew, 1 max-cap, 127 max-fanout (reported, not fatal) |
| IR drop / power | worst IR drop 0.2 mV; 5.5 mW total at the typical corner |
| Precheck | all nine checks pass: KLayout SG13CMOS5L DRC (the sign-off DRC, over the merged GDS with the macro), pin label overlap, zero area, KLayout checks, pin check, boundary check, layer check, cell name check, analog pin check |
| gl_test | success: the seven pads-only and host-interface tests pass on the gate-level netlist with the vendored macro model; the nine lockstep tests skip |

Slow-corner critical path (`nom_slow_1p08V_125C`, data arrival 22.42 ns
against 20.48 ns required): the macro's data output `u_imem.u_sram/A_DOUT[8]`
(266 of the 277 endpoints; `A_DOUT[9]` for the other 11), whose clock-to-output
is 5.38 ns at the slow corner against 0.59 ns for the flop it replaced, then
four fanout buffers (2.2 ns), the opcode decode, the ALU (about 7 ns) and the
register-file write-enable distribution (about 4.5 ns), ending in
`u_core.regs[11][*]` (thread 1's r3). The macro removed two thirds of the
cells and of the wire, which is worth about 3.4 ns on this path, but its own
access time costs 4.8 ns more than a flop, so the slow corner is 1.35 ns worse
than in the first run while the typical corner keeps +6.40 ns.

**Accepted and merged into master** (D-028, D-029): typical setup slack
+6.40 ns (at least +5 ns required) and every sign-off check passes.

### Run 37176010222, 2026-10-04, commit 23eb0dc (`decode-onehot`: the macro design with the registered one-hot thread select)

Dispatched because the slow corner still failed on the macro run (D-028).
Jobs: `gds` success (1 h 34 min), `gl_test` success, `viewer` success,
`precheck` success (30 min, all nine checks).

| Item | Value |
|---|---|
| Standard cells | 13,317 instances (1,470 flops, 2,989 timing-repair buffers) plus the macro; 72,309 with fill |
| Cell area / utilisation | 194,339 um^2 + 28,127 um^2 macro; 24.7% (standard cells 22.2%) |
| Setup slack, 20 ns | fast **+11.85 ns**; typical **+7.41 ns**; slow **-0.13 ns, 1 violating endpoint** |
| Hold slack | fast +0.123 ns, typical +0.317 ns, slow +0.657 ns; no violations |
| Routing | detailed routing 48 min 55 s; DRC 0; wire length 547,268 um |
| DRC / LVS / antenna | routing DRC 0; Magic DRC 29,294 and 10 illegal overlaps, the same macro-internal and stripe-over-OBS items as the `sram-macro` run; LVS 0; antenna 0 |
| Slew / cap / fanout | 0 max-slew, 5 max-cap, 129 max-fanout |
| gl_test | success |

Against the `sram-macro` run the one-hot select gains 1.0 ns at the typical
corner and 1.8 ns at the slow corner, and the slow-corner violations drop
from 277 endpoints to one: `u_imem.u_sram/A_DOUT[8]` to
`u_core.deadline[0][15]`, the `WAITD` deadline update (macro output 5.36 ns,
then two chained 16-bit additions, the completion test and the write
enable), 0.134 ns late. **Merged into master** on 2026-10-04 (D-030).

### Run 37169341232, 2026-10-04, commit 794dfac (master before the merge: capture and replay with the program memory as flops)

Started by the push of master. Jobs: `gds` success (2 h 34 min), `gl_test`
**success** (the first green gate-level run: the test-configuration fixes
of b7cc046 work in CI), `viewer` success; `precheck` was still running when
the merge of `sram-macro` superseded this run.

| Item | Value |
|---|---|
| Standard cells | 41,436 instances (5,575 flops, 10,093 timing-repair buffers); 81,664 with fill |
| Cell area / utilisation | 631,780 um^2; 70.0% |
| Setup slack, 20 ns | fast +10.84 ns; typical +5.96 ns; slow -2.36 ns, 32 violating endpoints (`dbg_ir[11]` to `u_core.regs[6][*]`) |
| Hold slack | fast +0.116 ns, typical +0.307 ns, slow +0.637 ns |
| Routing | detailed routing 1 h 38 min 40 s; DRC 0; wire length 1,663,147 um |
| DRC / LVS / antenna | Magic DRC 0, illegal overlaps 0, LVS 0, antenna 0 |

Kept for the record: the flop-memory configuration is now the
`KEYER_IMEM_FLOPS` fallback, not the tapeout configuration.

### Run 37181323699, 2026-10-04, commit 8dab03a (master: the merge of `sram-macro`)

Started by the push of the merge (D-029); it re-hardens the design of run
37169889955 on master. Jobs: `gds` success (59 min), `gl_test` success,
`viewer` success, `precheck` success (27 min, all nine checks). Numbers from
`python3 scripts/gds_report.py 37181323699`.

| Item | Value |
|---|---|
| Standard cells | 13,203 instances (1,463 flops, 2,876 timing-repair buffers) plus the macro; 72,012 with fill |
| Cell area / utilisation | 194,522 um^2 + 28,127 um^2 macro; 24.7% (standard cells 22.2%) |
| Setup slack, 20 ns | fast +11.20 ns; typical +6.40 ns; slow -1.94 ns, 277 violating endpoints |
| Hold slack | fast +0.092 ns, typical +0.270 ns, slow +0.593 ns; no violations |
| Routing | detailed routing 31 min 33 s; DRC 0; wire length 560,364 um |
| DRC / LVS / antenna | Magic DRC 29,294 and 10 illegal overlaps (the macro-internal and stripe-over-OBS items of the branch run); LVS 0; antenna 0 |
| Slew / cap / fanout | 14 max-slew, 1 max-cap, 127 max-fanout |
| gl_test | success |

Every number equals the branch run's (the flow is deterministic for the same
sources); only the wall-clock times differ with the runner. Master carried
this design until the merge of `decode-onehot` (D-030).

### Run 37228068179, 2026-10-04, commit 043091d (master: the merge of `decode-onehot`, D-030)

Started by the push of the merge. Jobs: `gds` success (1 h 05 min),
`gl_test` success, `viewer` success, `precheck` success (30 min, all nine
checks). Numbers from `python3 scripts/gds_report.py 37228068179`.

| Item | Value |
|---|---|
| Standard cells | 13,317 instances (1,470 flops, 2,989 timing-repair buffers) plus the macro; 72,309 with fill |
| Cell area / utilisation | 194,339 um^2 + 28,127 um^2 macro; 24.7% (standard cells 22.2%) |
| Setup slack, 20 ns | fast +11.85 ns; typical +7.41 ns; slow -0.13 ns, 1 violating endpoint (`A_DOUT[8]` to `deadline[0][15]`) |
| Hold slack | fast +0.123 ns, typical +0.317 ns, slow +0.657 ns; no violations |
| Routing | detailed routing 31 min 56 s; DRC 0; wire length 547,268 um |
| DRC / LVS / antenna | Magic DRC 29,294 and 10 illegal overlaps (macro-internal and stripe-over-OBS, as before); LVS 0; antenna 0 |
| Slew / cap / fanout | 0 max-slew, 5 max-cap, 129 max-fanout |
| gl_test | success |

Every number equals branch run 37176010222. This is the master design.

### Run 37247680638, 2026-10-04, commit faaeee1 (`waitd-csa`: carry-save `WAITD` completion test)

Started by the push of the branch. Jobs: `gds` success (1 h 25 min),
`gl_test` success, `viewer` success, `precheck` success (29 min, all nine
checks). Numbers from `python3 scripts/gds_report.py 37247680638`.

| Item | Value |
|---|---|
| Standard cells | 13,501 instances (1,470 flops, 3,012 timing-repair buffers) plus the macro; 72,445 with fill |
| Cell area / utilisation | 195,781 um^2 + 28,127 um^2 macro; 24.8% (standard cells 22.4%) |
| Setup slack, 20 ns | fast **+12.33 ns**; typical **+8.12 ns**; slow **+0.76 ns, no violating endpoint** |
| Hold slack | fast +0.110 ns, typical +0.295 ns, slow +0.625 ns; no violations |
| Routing | detailed routing 42 min 55 s; DRC 0; wire length 547,368 um |
| DRC / LVS / antenna | Magic DRC 29,294 and 10 illegal overlaps (macro-internal and stripe-over-OBS, as before); LVS 0; antenna 0 |
| Slew / cap / fanout | 0 max-slew, 1 max-cap, 131 max-fanout |
| gl_test | success |

Against master (37228068179) the carry-save step gains 0.7 ns at the
typical corner and 0.9 ns at the slow corner for 184 cells and 1,442 um^2
more, and **setup is met at all three corners for the first time**. The
core is proven equivalent to master's (533 of 533 points). Not merged:
Ahan decides (docs/HANDOFF.md).

### Run 37252116397, 2026-10-05, commit 1c6def3 (master before D-034: one-hot select, behavioural-memory change of D-032)

Started by the push of the session-4 commits, which touched `src/keyer_imem.v`
(only its `KEYER_IMEM_FLOPS` path, which the hardened design does not use).
Jobs: `gds` success (1 h 29 min), `gl_test` success, `viewer` success,
`precheck` success (31 min, all nine checks). Numbers from
`python3 scripts/gds_report.py 37252116397`.

| Item | Value |
|---|---|
| Standard cells | 13,317 instances (1,470 flops, 2,989 timing-repair buffers) plus the macro; 72,309 with fill |
| Cell area / utilisation | 194,339 um^2 + 28,127 um^2 macro; 24.7% (standard cells 22.2%) |
| Setup slack, 20 ns | fast +11.85 ns; typical +7.41 ns; slow -0.13 ns, 1 violating endpoint (`A_DOUT[8]` to `deadline[0][15]`) |
| Hold slack | fast +0.123 ns, typical +0.317 ns, slow +0.657 ns; no violations |
| Routing | detailed routing 46 min 56 s; DRC 0; wire length 547,268 um |
| DRC / LVS / antenna | Magic DRC 29,294 and 10 illegal overlaps (macro-internal and stripe-over-OBS, as before); LVS 0; antenna 0 |
| Slew / cap / fanout | 0 max-slew, 5 max-cap, 129 max-fanout |
| gl_test | success |

Every number equals run 37228068179: the macro path of the memory is
unchanged, as intended. This is the last run of the one-hot master; the
merge of `waitd-csa` (D-034) follows.

### Run 37260270799, 2026-10-05, commit a56aae4 (master: the merge of `waitd-csa`, D-034)

Started by the push of the merge. Jobs: `gds` success (1 h 28 min),
`gl_test` success, `viewer` success, `precheck` success (35 min, all nine
checks). Numbers from `python3 scripts/gds_report.py 37260270799`.

| Item | Value |
|---|---|
| Standard cells | 13,501 instances (1,470 flops, 3,012 timing-repair buffers) plus the macro; 72,445 with fill |
| Cell area / utilisation | 195,781 um^2 + 28,127 um^2 macro; 24.8% (standard cells 22.4%) |
| Setup slack, 20 ns | fast +12.33 ns; typical +8.12 ns; slow +0.76 ns; no violating endpoint at any corner |
| Hold slack | fast +0.110 ns, typical +0.295 ns, slow +0.625 ns; no violations |
| Routing | detailed routing 44 min 55 s; DRC 0; wire length 547,368 um |
| DRC / LVS / antenna | Magic DRC 29,294 and 10 illegal overlaps (macro-internal and stripe-over-OBS, as before); LVS 0; antenna 0 |
| Slew / cap / fanout | 0 max-slew, 1 max-cap, 131 max-fanout |
| gl_test | success |

Every number equals branch run 37247680638. This is the master design:
setup is met at all three corners.

### Run 37266431182, 2026-10-05, commit f0e0b96 (`serializer`: the serializer engine, D-036)

Started by the push of the branch. Jobs: `gds` success (2 h 17 min),
`gl_test` success, `viewer` success, `precheck` success (37 min, all nine
checks). Numbers from `python3 scripts/gds_report.py 37266431182`.

| Item | Value |
|---|---|
| Standard cells | 14,859 instances (1,600 flops, 3,256 timing-repair buffers) plus the macro; 72,820 with fill |
| Cell area / utilisation | 215,894 um^2 + 28,127 um^2 macro; 27.0% (standard cells 24.7%) |
| Setup slack, 20 ns | fast +12.53 ns; typical +8.41 ns; slow +1.37 ns; no violating endpoint at any corner |
| Hold slack | fast +0.075 ns, typical +0.254 ns, slow +0.569 ns; no violations |
| Routing | detailed routing 1 h 33 min (nine iterations); DRC 0; wire length 617,053 um |
| DRC / LVS / antenna | Magic DRC 29,294 and 10 illegal overlaps (macro-internal and stripe-over-OBS, as before); LVS 0; antenna 0 |
| Slew / cap / fanout | 13 max-slew, 0 max-cap, 140 max-fanout |
| gl_test | success |

Against master (37260270799) the engine costs 1,358 cells, 130 flops and
20,113 um^2 (2.2 points of utilisation); docs/SERIALIZER.md had estimated
12,000 to 13,000 um^2 and the Yosys estimate before layout was 16,246.
Setup slack did not fall: +8.41 ns typical and +1.37 ns slow (master: +8.12
and +0.76; the difference is placement, not a faster design). The new
items to watch are the 13 max-slew violations (none before) and detailed
routing, which took twice as long. Both conditions for the merge hold
(timing clean at all corners, utilisation under 40%): merged as D-037.

### Summary of the runs

| Run | Design | Cells | Utilisation | Setup fast / typical / slow (ns) | Routing | Precheck | gl_test |
|---|---|---|---|---|---|---|---|
| 37073185698 | flops, no capture (d979e82) | 37,821 | 66.9% | +11.46 / +7.09 / -0.59 (12) | 2 h 00 | 9/9 | compile error (fixed) |
| 37169341232 | flops + capture (794dfac) | 41,436 | 70.0% | +10.84 / +5.96 / -2.36 (32) | 1 h 39 | superseded | pass |
| 37169889955 | macro + capture (de17304, `sram-macro`) | 13,203 + macro | 24.7% | +11.20 / +6.40 / -1.94 (277) | 49 min | 9/9 | pass |
| 37176010222 | macro + capture + one-hot select (23eb0dc, `decode-onehot`) | 13,317 + macro | 24.7% | +11.85 / +7.41 / -0.13 (1) | 49 min | 9/9 | pass |
| 37181323699 | macro + capture (8dab03a, master after D-029; same design as 37169889955) | 13,203 + macro | 24.7% | +11.20 / +6.40 / -1.94 (277) | 32 min | 9/9 | pass |
| 37228068179 | macro + capture + one-hot select (043091d, master after D-030; same design as 37176010222) | 13,317 + macro | 24.7% | +11.85 / +7.41 / -0.13 (1) | 32 min | 9/9 | pass |
| 37247680638 | macro + capture + one-hot select + carry-save WAITD (faaeee1, `waitd-csa`) | 13,501 + macro | 24.8% | +12.33 / +8.12 / +0.76 (0) | 43 min | 9/9 | pass |
| 37252116397 | the same design as 37228068179 (1c6def3, master with the behavioural-memory change of D-032) | 13,317 + macro | 24.7% | +11.85 / +7.41 / -0.13 (1) | 47 min | 9/9 | pass |
| 37260270799 | macro + capture + one-hot select + carry-save WAITD (a56aae4, master after D-034; same design as 37247680638) | 13,501 + macro | 24.8% | +12.33 / +8.12 / +0.76 (0) | 45 min | 9/9 | pass |
| 37266431182 | the above + serializer engine (f0e0b96, `serializer`) | 14,859 + macro | 27.0% | +12.53 / +8.41 / +1.37 (0) | 1 h 33 | 9/9 | pass |
| 37286790227 | the same design as 37266431182 (da3e126, master after D-037) | 14,859 + macro | 27.0% | +12.53 / +8.41 / +1.37 (0) | 1 h 33 | 9/9 | pass |
| 37339746749 | the above + D-039 and the slew margin of D-041 (103b52d, `d039`) | 15,085 + macro | 27.1% | +12.39 / +8.25 / +0.99 (0) | 54 min | 9/9 | pass |
| 37363511492 | the same design as 37339746749 (71d8a6d, master after D-042) | 15,085 + macro | 27.1% | +12.39 / +8.25 / +0.99 (0) | 55 min | 9/9 | pass |
| 37534398328 | the same RTL with the capacitance repair margin of D-043 (19440d7, `d043`) | 15,109 + macro | 27.2% | +12.43 / +8.24 / +1.09 (0) | 55 min | 9/9 | pass |

### Run 37286790227, 2026-10-05, commit da3e126 (master after the serializer merge, D-037)

Started by the push of the merge. Jobs: `gds` success (2 h 12 min),
`gl_test` success, `viewer` success, `precheck` success (37 min, all nine
checks). Numbers from `python3 scripts/gds_report.py 37286790227`. **Every
number equals branch run 37266431182**: 14,859 cells (1,600 flops, 3,256
timing-repair buffers) plus the macro, utilisation 27.0%, setup +12.53 /
+8.41 / +1.37 ns (fast / typical / slow), hold +0.075 / +0.254 / +0.569 ns,
no violation at any corner, routing DRC 0, LVS 0, antenna 0, 13 max-slew,
0 max-cap, 140 max-fanout.

**The 13 max-slew entries** (`55-openroad-stapostpnr/nom_slow_1p08V_125C/
checks.rpt`; none at the typical or the fast corner). They are 13 pins on
two nets, both in our logic, both driven by a `sg13cmos5l_nor4_1`:

| Net | What it is | Loads | Slew at the slow corner (limit 2.507 ns) |
|---|---|---|---|
| `_02862_` (driver `_09913_`) | the `SERCFG` commit strobe: decode of the instruction word with `fetch_ok`, `running` and the thread select, from the core to the serializer's register resets | 7 | 3.618 ns (1.11 ns over) |
| `_02945_` (driver `_10000_`) | inbox 1's pointer comparison (`wr_ptr` against `rd_ptr`, low four bits) | 4 | 2.571 ns (0.06 ns over) |

Violations or warnings: they are real violations of the cell library's
`max_transition` at the slow corner (1.08 V, 125 C), which the flow reports
and does not fail on. The sign-off corner (typical, D-028) has none, and
setup is met at the slow corner with these slews in the calculation
(+1.37 ns), but a slew beyond the library's limit is outside the range the
cell delays were characterised for, so the slow-corner numbers of paths
through these two nets are extrapolated. Cause: the flow repairs slew once,
after global placement, at the typical corner, to 80% of the limit
(`DESIGN_REPAIR_MAX_SLEW_PCT` 20); a net left at up to 2.0 ns there is up
to about 3.6 ns at the slow corner, where the same cells are 1.7 to 1.8
times slower. Which nets end up above the limit is then a matter of
placement: earlier runs of other designs had 33, 14 and 0. Fix: D-041.

### Run 37339746749, 2026-10-05, commit 103b52d (branch `d039`: D-039 and the slew margin of D-041)

Started by the push of the branch. Jobs: `gds` success (1 h 37 min),
`gl_test` success, `viewer` success, `precheck` success (all nine checks).
Numbers from `python3 scripts/gds_report.py 37339746749`.

| Item | Value |
|---|---|
| Standard cells | 15,085 instances (1,601 flops, 3,287 timing-repair buffers) plus the macro; 73,033 with fill |
| Cell area / utilisation | 216,717 um^2 + 28,127 um^2 macro; 27.1% (standard cells 24.8%) |
| Setup slack, 20 ns | fast +12.39 ns; typical +8.25 ns; slow +0.99 ns; no violating endpoint at any corner |
| Hold slack | fast +0.104 ns, typical +0.286 ns, slow +0.610 ns; no violations |
| Routing | detailed routing 54 min (eight iterations); DRC 0; wire length 619,655 um |
| DRC / LVS / antenna | Magic DRC 29,294 and 10 illegal overlaps (as before); LVS 0; antenna 0 |
| Slew / cap / fanout | **0 max-slew**, 1 max-cap, 138 max-fanout |
| gl_test | success |

Against master (37286790227): +226 cells, +1 flop (`rx_drop`), +823 um^2,
+0.1 point of utilisation, for D-039's logic and the extra slew repair.

**The slew fix is confirmed** (D-041): no pin is above the library's
`max_transition` at any corner (13 at the slow corner before).
`resolved.json` shows `DESIGN_REPAIR_MAX_SLEW_PCT` 50.

Setup is met at all three corners; the slow corner has +0.99 ns where the
last run had +1.37. The worst path is the same one (macro data output into
decode, 12 of the 12 worst start at the macro); the difference is within
what placement alone has moved this figure between runs (+0.76 to +1.37).

**One max-cap entry, at all three corners**: the macro's output pin
`A_DOUT[7]` drives 68.4 fF against the 64 fF the macro's library allows
(7% over; the net goes to one buffer over a long route). The flow reports
it and does not fail. Runs before the serializer had 1 or 5 entries of
this kind; the last two had none. Not fixed here: the key that would do it
(`DESIGN_REPAIR_MAX_CAP_PCT`, the same mechanism as D-041) is not covered
by Ahan's instruction for the slew step. Open, see HANDOFF.

Merge rule (timing clean at all corners, utilisation under 40%): met.

### Run 37363511492, 2026-10-05, commit 71d8a6d (master after the merge of `d039`, D-042)

Started by the push of the merge. Its first attempt, and the `docs` run of
the same push, were cancelled by GitHub after 15 minutes ("The job was not
acquired by Runner of type hosted even after multiple attempts"); both were
started again by hand and finished. Jobs: `gds` success (1 h 39 min),
`gl_test` success, `viewer` success, `precheck` success (36 min, all nine
checks). Numbers from `python3 scripts/gds_report.py 37363511492`
(downloaded once, 2026-10-06). **Every number equals branch run
37339746749**: 15,085 standard cells (1,601 flops, 3,287 timing-repair
buffers) plus the macro, 73,033 instances with fill, 216,717 um^2 + 28,127
um^2 macro, utilisation 27.1%, setup +12.39 / +8.25 / +0.99 ns (fast /
typical / slow), hold +0.104 / +0.286 / +0.610 ns, no violation at any
corner, detailed routing 55 min with DRC 0, wire length 619,655 um, Magic
DRC 29,294 and 10 illegal overlaps (macro-internal, as before), LVS 0,
antenna 0, 0 max-slew, 1 max-cap (the macro's `A_DOUT[7]`: 68.4 fF at the
slow corner, 68.5 typical, 68.7 fast, against 64), 138 max-fanout, power
5.9 mW, worst IR drop 0.3 mV. `resolved.json`: `DESIGN_REPAIR_MAX_SLEW_PCT`
50, `DESIGN_REPAIR_MAX_CAP_PCT` 20 (the default). The max-cap entry is
D-043's.

### Run 37534398328, 2026-10-06, commit 19440d7 (branch `d043`: `DESIGN_REPAIR_MAX_CAP_PCT` 30, D-043)

Started by the push of the branch. Jobs: `gds` success (1 h 41 min),
`gl_test` success, `viewer` success, `precheck` success (42 min, all nine
checks). Numbers from `python3 scripts/gds_report.py 37534398328`
(downloaded once).

| Item | Value |
|---|---|
| Standard cells | 15,109 instances (1,601 flops, 3,292 timing-repair buffers) plus the macro; 73,054 with fill |
| Cell area / utilisation | 216,888 um^2 + 28,127 um^2 macro; 27.2% (standard cells 24.8%) |
| Setup slack, 20 ns | fast +12.43 ns; typical +8.24 ns; slow +1.09 ns; no violating endpoint at any corner |
| Hold slack | fast +0.104 ns, typical +0.285 ns, slow +0.607 ns; no violations |
| Routing | detailed routing 55 min (ten iterations); DRC 0; wire length 617,009 um |
| DRC / LVS / antenna | Magic DRC 29,294 and 10 illegal overlaps (as before); LVS 0; antenna 0 |
| Slew / cap / fanout | 0 max-slew, **0 max-cap**, 139 max-fanout |
| gl_test | success |

Against master (37363511492): +24 cells (five more repair buffers), +171
um^2, +0.1 point of utilisation; the max-cap entry on the macro's
`A_DOUT[7]` is gone at all three corners (`resolved.json`:
`DESIGN_REPAIR_MAX_CAP_PCT` 30), and the slow-corner setup margin is +1.09
ns instead of +0.99 (placement). D-043 is confirmed at its first value;
merged as D-044. The sign-off checks now report nothing: no setup or hold
violation at any corner, no slew, capacitance or routing DRC entry, LVS and
antenna clean.

