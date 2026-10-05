# Area and hardening log

Numbers from Yosys against the IHP CMOS5L typical liberty
(`sg13cmos5l_stdcell_typ_1p20V_25C.lib`, flop with reset 48.99 um^2), scripts
in `synth/` (`run.ys` from `synth.ys`, `run_sram_lib.ys` from `run_sram.ys`,
`core_only.ys` with NTHREADS substituted). Area is standard-cell area before
placement. Budget (PLAN.md section 4): the 6x4 block core is 902,417 um^2;
at the template's 60% placement density about 430,000 um^2 of cells fit.
Hardening results from the GitHub `gds` workflow are appended below.

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

### Summary of the runs

| Run | Design | Cells | Utilisation | Setup fast / typical / slow (ns) | Routing | Precheck | gl_test |
|---|---|---|---|---|---|---|---|
| 37073185698 | flops, no capture (d979e82) | 37,821 | 66.9% | +11.46 / +7.09 / -0.59 (12) | 2 h 00 | 9/9 | compile error (fixed) |
| 37169341232 | flops + capture (794dfac) | 41,436 | 70.0% | +10.84 / +5.96 / -2.36 (32) | 1 h 39 | superseded | pass |
| 37169889955 | macro + capture (de17304, `sram-macro`) | 13,203 + macro | 24.7% | +11.20 / +6.40 / -1.94 (277) | 49 min | 9/9 | pass |
| 37176010222 | macro + capture + one-hot select (23eb0dc, `decode-onehot`) | 13,317 + macro | 24.7% | +11.85 / +7.41 / -0.13 (1) | 49 min | 9/9 | pass |
| 37181323699 | macro + capture (8dab03a, master after D-029; same design as 37169889955) | 13,203 + macro | 24.7% | +11.20 / +6.40 / -1.94 (277) | 32 min | 9/9 | pass |
| 37228068179 | macro + capture + one-hot select (043091d, master after D-030; same design as 37176010222) | 13,317 + macro | 24.7% | +11.85 / +7.41 / -0.13 (1) | 32 min | 9/9 | pass |
