/*
 * Port-only blackbox for the IHP 256x16 SRAM macro (Keyer program memory).
 * SPDX-License-Identifier: Apache-2.0
 *
 * This file exists only so that synthesis and lint have a module declaration
 * for the hard macro: it is the `nl` entry of MACROS in src/config.json and
 * is NOT listed in info.yaml. The real behaviour comes from the macro
 * GDS/LEF/LIB in macro/RM_IHPSG13_1P_256x16_c2_bm_bist/, and the simulation
 * model there is what test/Makefile compiles. Do not add a body.
 *
 * Port list copied from the vendored model; keep them identical.
 */

`default_nettype none

/* verilator lint_off UNUSEDSIGNAL */
/* verilator lint_off UNDRIVEN */
module RM_IHPSG13_1P_256x16_c2_bm_bist (
    input  wire        A_CLK,
    input  wire        A_MEN,
    input  wire        A_WEN,
    input  wire        A_REN,
    input  wire [7:0]  A_ADDR,
    input  wire [15:0] A_DIN,
    input  wire        A_DLY,
    output wire [15:0] A_DOUT,
    input  wire [15:0] A_BM,
    input  wire        A_BIST_CLK,
    input  wire        A_BIST_EN,
    input  wire        A_BIST_MEN,
    input  wire        A_BIST_WEN,
    input  wire        A_BIST_REN,
    input  wire [7:0]  A_BIST_ADDR,
    input  wire [15:0] A_BIST_DIN,
    input  wire [15:0] A_BIST_BM
);
endmodule
/* verilator lint_on UNDRIVEN */
/* verilator lint_on UNUSEDSIGNAL */

`default_nettype wire
