/*
 * Keyer: synchronous FIFO, first-word-fall-through (rd_data shows the head).
 * DEPTH must be a power of two. count is 0..DEPTH.
 * A push when full and a pop when empty are ignored.
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module keyer_fifo #(
    parameter WIDTH = 8,
    parameter DEPTH = 16,
    parameter AW    = 4
) (
    input  wire             clk,
    input  wire             rst_n,
    input  wire             clear,
    input  wire             push,
    input  wire [WIDTH-1:0] wr_data,
    input  wire             pop,
    output wire [WIDTH-1:0] rd_data,
    output wire             empty,
    output wire             full,
    output wire [AW:0]      count
);
    reg [WIDTH-1:0] mem [0:DEPTH-1];
    reg [AW:0] wr_ptr, rd_ptr;        // one extra bit distinguishes full from empty

    assign empty   = (wr_ptr == rd_ptr);
    assign full    = (wr_ptr[AW-1:0] == rd_ptr[AW-1:0]) && (wr_ptr[AW] != rd_ptr[AW]);
    assign count   = wr_ptr - rd_ptr;
    assign rd_data = mem[rd_ptr[AW-1:0]];

    wire do_push = push && !full;
    wire do_pop  = pop && !empty;

    always @(posedge clk) begin
        if (!rst_n || clear) begin
            wr_ptr <= 0;
            rd_ptr <= 0;
        end else begin
            if (do_push) wr_ptr <= wr_ptr + 1'b1;
            if (do_pop)  rd_ptr <= rd_ptr + 1'b1;
        end
    end

    always @(posedge clk) begin
        if (do_push) mem[wr_ptr[AW-1:0]] <= wr_data;
    end
endmodule
