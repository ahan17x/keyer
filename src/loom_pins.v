/*
 * Loom: pin unit.
 *
 * Pin space seen by firmware: 0-7 uio (bidirectional, push-pull or
 * open-drain per pin), 8-15 ui (inputs), 16-23 uo (outputs; 16 and 17 are
 * owned by the host interface and ignore firmware writes).
 *
 * Inputs go through a two-flop synchroniser. `level` is the synchronised
 * pad level (for 16-23, the driven value); `level2` is `level` two cycles
 * earlier, for edge detection.
 *
 * In open-drain mode uio_out is forced to 0 so the pad is never driven high:
 * writing 0 drives low (oe=1), writing 1 releases (oe=0).
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module loom_pins (
    input  wire        clk,
    input  wire        rst_n,
    // pads
    input  wire [7:0]  ui_in,
    input  wire [7:0]  uio_in,
    output reg  [7:0]  uio_out,
    output reg  [7:0]  uio_oe,
    output reg  [7:0]  uo_out,          // bits 1:0 are overridden by the host interface in the top
    // core command, at most one per cycle
    input  wire        cmd_valid,
    input  wire [2:0]  cmd_op,          // see localparams
    input  wire [4:0]  cmd_pin,
    input  wire [7:0]  cmd_data,        // bit 0 for single-pin ops, a byte for OUTB/OUTOE
    // host
    input  wire        host_mode_we,
    input  wire [7:0]  host_mode_val,
    output reg  [7:0]  od_mask,
    // to the core
    output wire [23:0] level,
    output wire [23:0] level2
);
    localparam OP_WRITE = 3'd0;   // pinwrite(pin, data[0])
    localparam OP_OEN   = 3'd1;
    localparam OP_OEF   = 3'd2;
    localparam OP_OD    = 3'd3;
    localparam OP_PP    = 3'd4;
    localparam OP_OUTB  = 3'd5;   // pinwrite(i, data[i]) for i in 0..7
    localparam OP_OUTOE = 3'd6;   // uio_oe = data for push-pull pins

    // ---- input synchronisers -------------------------------------------
    reg [15:0] sync1, sync2, lvl_d1, lvl_d2;
    always @(posedge clk) begin
        if (!rst_n) begin
            sync1 <= 16'd0; sync2 <= 16'd0; lvl_d1 <= 16'd0; lvl_d2 <= 16'd0;
        end else begin
            sync1  <= {ui_in, uio_in};
            sync2  <= sync1;
            lvl_d1 <= sync2;
            lvl_d2 <= lvl_d1;
        end
    end
    reg [7:0] uo_d1, uo_d2;
    always @(posedge clk) begin
        if (!rst_n) begin uo_d1 <= 8'd0; uo_d2 <= 8'd0; end
        else begin uo_d1 <= uo_out; uo_d2 <= uo_d1; end
    end
    assign level  = {uo_out, sync2};
    assign level2 = {uo_d2, lvl_d2};

    // ---- drive registers -----------------------------------------------
    wire [7:0] pin_bit  = (cmd_pin < 5'd8) ? (8'd1 << cmd_pin[2:0]) : 8'd0;
    wire       pin_is_uo = (cmd_pin >= 5'd16);
    wire [7:0] uo_bit   = 8'd1 << cmd_pin[2:0];
    wire [7:0] uo_allowed = 8'hFC;                      // 16, 17 reserved

    // next-state computed in a procedural block to keep OD rules in one place
    reg [7:0] n_uio_out, n_uio_oe, n_od, n_uo;
    integer i;
    always @(*) begin
        n_uio_out = uio_out;
        n_uio_oe  = uio_oe;
        n_od      = od_mask;
        n_uo      = uo_out;
        if (cmd_valid) begin
            case (cmd_op)
                OP_WRITE: begin
                    if (pin_bit != 8'd0) begin
                        if ((od_mask & pin_bit) != 8'd0) begin
                            n_uio_out = uio_out & ~pin_bit;
                            n_uio_oe  = cmd_data[0] ? (uio_oe & ~pin_bit) : (uio_oe | pin_bit);
                        end else begin
                            n_uio_out = cmd_data[0] ? (uio_out | pin_bit) : (uio_out & ~pin_bit);
                        end
                    end else if (pin_is_uo && (uo_allowed & uo_bit) != 8'd0) begin
                        n_uo = cmd_data[0] ? (uo_out | uo_bit) : (uo_out & ~uo_bit);
                    end
                end
                OP_OEN: if ((pin_bit & ~od_mask) != 8'd0) n_uio_oe = uio_oe | pin_bit;
                OP_OEF: if ((pin_bit & ~od_mask) != 8'd0) n_uio_oe = uio_oe & ~pin_bit;
                OP_OD: if (pin_bit != 8'd0) begin
                    n_od      = od_mask | pin_bit;
                    n_uio_oe  = uio_oe & ~pin_bit;
                    n_uio_out = uio_out & ~pin_bit;
                end
                OP_PP: if (pin_bit != 8'd0) n_od = od_mask & ~pin_bit;
                OP_OUTB: begin
                    for (i = 0; i < 8; i = i + 1) begin
                        if (od_mask[i]) begin
                            n_uio_out[i] = 1'b0;
                            n_uio_oe[i]  = ~cmd_data[i];
                        end else begin
                            n_uio_out[i] = cmd_data[i];
                        end
                    end
                end
                OP_OUTOE: n_uio_oe = (uio_oe & od_mask) | (cmd_data & ~od_mask);
                default: ;
            endcase
        end
        if (host_mode_we) begin                      // host PINMODE: applied last
            n_od      = host_mode_val;
            n_uio_oe  = n_uio_oe & ~host_mode_val;
            n_uio_out = n_uio_out & ~host_mode_val;
        end
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            uio_out <= 8'd0; uio_oe <= 8'd0; od_mask <= 8'd0; uo_out <= 8'd0;
        end else begin
            uio_out <= n_uio_out;
            uio_oe  <= n_uio_oe;
            od_mask <= n_od;
            uo_out  <= n_uo;
        end
    end
endmodule
