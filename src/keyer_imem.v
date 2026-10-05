/*
 * Keyer: 256 x 16 program memory with a one-cycle synchronous read.
 *
 * Default: wraps the IHP RM_IHPSG13_1P_256x16_c2_bm_bist SRAM macro (the
 * CMOS5L SRAM library is the SG13G2 one). The macro's read data is
 * registered inside the macro, so timing matches the behavioural model.
 * The port-only blackbox src/RM_IHPSG13_1P_256x16_c2_bm_bist.v provides the
 * module declaration for synthesis and lint; simulation compiles the vendored
 * model from macro/RM_IHPSG13_1P_256x16_c2_bm_bist/ instead (DECISIONS D-025).
 * With `KEYER_IMEM_FLOPS defined: behavioural flop array, the fallback used
 * by the FPGA build and the area comparison.
 *
 * Both branches hold rdata in a write cycle: the macro because A_REN = ~we,
 * the behavioural memory because its read register loads only when we is
 * low. The read data of a write cycle is never used: the fetch after any
 * memory port use is invalid (SEMANTICS 2.1, 10.5, 14.1), and the host and
 * the replay engine take read data only in the cycle after a read request.
 * Without read-during-write, Yosys maps the behavioural memory on iCE40 to
 * one SB_RAM40_4K instead of emulating write-through with flops and LUTs.
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module keyer_imem (
    input  wire        clk,
    input  wire        we,
    input  wire [7:0]  addr,
    input  wire [15:0] wdata,
    output wire [15:0] rdata
);
`ifndef KEYER_IMEM_FLOPS
    RM_IHPSG13_1P_256x16_c2_bm_bist u_sram (
        .A_CLK      (clk),
        .A_MEN      (1'b1),
        .A_WEN      (we),
        .A_REN      (~we),
        .A_ADDR     (addr),
        .A_DIN      (wdata),
        .A_DLY      (1'b1),
        .A_DOUT     (rdata),
        .A_BM       (16'hFFFF),
        .A_BIST_CLK (1'b0),
        .A_BIST_EN  (1'b0),
        .A_BIST_MEN (1'b0),
        .A_BIST_WEN (1'b0),
        .A_BIST_REN (1'b0),
        .A_BIST_ADDR(8'd0),
        .A_BIST_DIN (16'd0),
        .A_BIST_BM  (16'd0)
    );
`else
    reg [15:0] mem [0:255];
    reg [15:0] rdata_q;
`ifndef SYNTHESIS
    integer i;
    initial begin                         // simulation only: unwritten words read as NOP-like zeros
        for (i = 0; i < 256; i = i + 1) mem[i] = 16'd0;
        rdata_q = 16'd0;
    end
`endif
    always @(posedge clk) begin
        if (we) mem[addr] <= wdata;
        else    rdata_q <= mem[addr];   // no read in a write cycle: rdata holds
    end
    assign rdata = rdata_q;
`endif
endmodule
