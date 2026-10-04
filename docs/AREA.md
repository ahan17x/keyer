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
