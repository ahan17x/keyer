# RM_IHPSG13_1P_256x16_c2_bm_bist

IHP foundry-provided 256 x 16 single-port SRAM macro with bit mask and BIST
(512 bytes), the Keyer program memory (DECISIONS D-004, D-025). Vendored so
that the Tiny Tapeout `gds` workflow hardens against a fixed copy.

## Source and licence

- Repository: <https://github.com/IHP-GmbH/IHP-Open-PDK>, branch `dev`,
  commit `bf079026` (2026-10-02), path
  `ihp-sg13g2/libs.ref/sg13g2_sram/{gds,lef,cdl,lib,verilog}/`.
- Licence: Apache-2.0 (the IHP Open PDK licence). These files are unmodified.
- The cmos5l PDK carries no SRAM of its own: `ihp-sg13cmos5l/libs.ref/
  sg13cmos5l_sram` is a symlink to the SG13G2 macros, so this is the SG13G2
  macro re-exported. Tiny Tapeout's organisers confirmed (2026-09-28) that
  macros may go on the CMOS5L shuttle; a CMOS5L SRAM has not been taped out
  yet.

## Files

| File | What it is |
|---|---|
| `RM_IHPSG13_1P_256x16_c2_bm_bist.gds` | layout, streamed into the final GDS |
| `RM_IHPSG13_1P_256x16_c2_bm_bist.lef` | abstract: `SIZE 236.8 BY 118.78`, `CLASS BLOCK`, `SYMMETRY X Y R90` |
| `RM_IHPSG13_1P_256x16_c2_bm_bist.cdl` | transistor netlist, for LVS (blackboxed in Magic) |
| `RM_IHPSG13_1P_256x16_c2_bm_bist_{typ_1p20V_25C,fast_1p32V_m55C,slow_1p08V_125C}.lib` | timing corners |
| `RM_IHPSG13_1P_256x16_c2_bm_bist.v`, `RM_IHPSG13_1P_core_behavioral_bm_bist.v` | simulation model (wrapper + behavioural core); `test/Makefile` compiles both with `-DFUNCTIONAL` for the RTL and gate-level cocotb runs |

The port-only blackbox for synthesis and lint is `src/RM_IHPSG13_1P_256x16_c2_bm_bist.v`
(the `nl` entry of `MACROS` in `src/config.json`); it is not in `info.yaml`.

## Flow recipe

`src/config.json` (`MACROS`, `PDN_MACRO_CONNECTIONS`, `PDN_CFG`, the Magic
waivers and the `FP_PDN_V*` stripe keys) and `src/pdn_cfg.tcl` follow the
recipe that took the sibling 512 x 16 macro through hardening, precheck and
gate-level test on the CMOS5L flow: Thomas Gilbert's `tt_um_loom`
(<https://github.com/thomasgilbert481/tt_um_loom>, Apache-2.0),
`docs/tt_cmos5l_facts.md` section 11 and `src/pdn_cfg.tcl`. The 256 x 16
macro has the same width and the same Metal4 power-column positions as the
512 x 16 one (columns 2.81 um wide on an 11.24 um same-net pitch, the
VDD!/VDDARRAY! split at local y 38.825..45.465), so the stripe geometry is
reused unchanged; only the macro height (118.78 um) differs.
