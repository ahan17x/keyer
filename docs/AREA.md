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

Capture and replay (D-024) cost 210 flops and 20,481 um^2; with the macro the
design is 170,404 um^2, 18.9% of the core (limit for the feature: 25%).
The D-018 timer and timeouts cost 63 flops and about 7,600 um^2 over the
pre-D-018 core; the whole logic sits at 28% of the placeable area with the
macro (121,796 + 28,127 = 149,923 um^2 of 430,000). NTHREADS = 4 adds 441
flops and 38,600 um^2 to the core.

## Hardening (GitHub `gds` workflow)

(filled in when the first run completes)
