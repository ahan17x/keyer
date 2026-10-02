/*
 * Keyer: protocol emulator for the Jane Street ASIC competition.
 * Tiny Tapeout top level. See docs/info.md and docs/isa.md.
 *
 * Pins:
 *   ui[0]  SCK   (host SPI)      uo[0]  MISO (host SPI)
 *   ui[1]  MOSI                  uo[1]  IRQ
 *   ui[2]  CS_n                  uo[2..7] firmware outputs (pins 18-23)
 *   ui[3..7] firmware inputs (pins 11-15); ui[0..2] readable as pins 8-10
 *   uio[0..7] firmware bidirectional pins 0-7 (push-pull or open-drain)
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module tt_um_ahan17x_keyer (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
    // ---- program memory port arbitration ----------------------------------
    wire [7:0]  fetch_addr;
    wire [15:0] imem_rdata;
    wire        host_imem_we, host_imem_re;
    wire [7:0]  host_imem_addr;
    wire [15:0] host_imem_wdata;
    wire        host_access = host_imem_we | host_imem_re;
    reg         fetch_ok;
    always @(posedge clk) begin
        if (!rst_n) fetch_ok <= 1'b0;
        else fetch_ok <= ~host_access;
    end

    keyer_imem u_imem (
        .clk   (clk),
        .we    (host_imem_we),
        .addr  (host_access ? host_imem_addr : fetch_addr),
        .wdata (host_imem_wdata),
        .rdata (imem_rdata)
    );

    // ---- pins ----------------------------------------------------------------
    wire        pin_valid;
    wire [2:0]  pin_op;
    wire [4:0]  pin_pin;
    wire [7:0]  pin_data;
    wire        pinmode_we;
    wire [7:0]  pinmode_val, od_mask;
    wire [23:0] level, level2;
    wire [7:0]  uo_fw;

    keyer_pins u_pins (
        .clk (clk), .rst_n (rst_n),
        .ui_in (ui_in), .uio_in (uio_in),
        .uio_out (uio_out), .uio_oe (uio_oe), .uo_out (uo_fw),
        .cmd_valid (pin_valid), .cmd_op (pin_op), .cmd_pin (pin_pin), .cmd_data (pin_data),
        .host_mode_we (pinmode_we), .host_mode_val (pinmode_val), .od_mask (od_mask),
        .level (level), .level2 (level2)
    );

    // ---- FIFOs ---------------------------------------------------------------
    wire [1:0] inbox_push, inbox_pop, outbox_push, outbox_pop;
    wire [7:0] inbox_wdata, outbox_wdata;
    wire [7:0] inbox_rdata [0:1];
    wire [7:0] outbox_rdata [0:1];
    wire [1:0] inbox_empty, inbox_full, outbox_empty, outbox_full;
    wire [4:0] inbox_count [0:1];
    wire [4:0] outbox_count [0:1];
    wire [3:0] fifo_clr;
    wire [1:0] rst_pulse;               // host soft reset of thread n: also empties its FIFOs

    genvar g;
    generate
        for (g = 0; g < 2; g = g + 1) begin : fifos
            keyer_fifo u_inbox (
                .clk (clk), .rst_n (rst_n), .clear (fifo_clr[2*g] | rst_pulse[g]),
                .push (inbox_push[g]), .wr_data (inbox_wdata),
                .pop (inbox_pop[g]), .rd_data (inbox_rdata[g]),
                .empty (inbox_empty[g]), .full (inbox_full[g]), .count (inbox_count[g])
            );
            keyer_fifo u_outbox (
                .clk (clk), .rst_n (rst_n), .clear (fifo_clr[2*g+1] | rst_pulse[g]),
                .push (outbox_push[g]), .wr_data (outbox_wdata),
                .pop (outbox_pop[g]), .rd_data (outbox_rdata[g]),
                .empty (outbox_empty[g]), .full (outbox_full[g]), .count (outbox_count[g])
            );
        end
    endgenerate

    // ---- core ----------------------------------------------------------------
    wire       run_we;
    wire [1:0] run_val, pc_we, running, halted, blocked;
    wire [7:0] pc_val, core_pc0, core_pc1;
    wire       dbg_retire, dbg_tid;
    wire [7:0] dbg_pc;
    wire [15:0] dbg_ir;

    keyer_core u_core (
        .clk (clk), .rst_n (rst_n),
        .fetch_addr (fetch_addr), .imem_rdata (imem_rdata), .fetch_ok (fetch_ok),
        .level (level), .level2 (level2),
        .pin_valid (pin_valid), .pin_op (pin_op), .pin_pin (pin_pin), .pin_data (pin_data),
        .inbox_rdata ({inbox_rdata[1], inbox_rdata[0]}),
        .inbox_empty (inbox_empty), .inbox_full (inbox_full), .inbox_pop (inbox_pop),
        .outbox_wdata (outbox_wdata), .outbox_push (outbox_push),
        .outbox_full (outbox_full), .outbox_empty (outbox_empty),
        .host_run_we (run_we), .host_run_val (run_val), .host_rst (rst_pulse),
        .host_pc_we (pc_we), .host_pc_val (pc_val),
        .running (running), .halted (halted), .blocked (blocked),
        .pc0_out (core_pc0), .pc1_out (core_pc1),
        .dbg_retire (dbg_retire), .dbg_tid (dbg_tid), .dbg_pc (dbg_pc), .dbg_ir (dbg_ir)
    );

    // ---- host interface ------------------------------------------------------
    wire miso, irq;
    keyer_host u_host (
        .clk (clk), .rst_n (rst_n),
        .sck (ui_in[0]), .mosi (ui_in[1]), .csn (ui_in[2]), .miso (miso), .irq (irq),
        .imem_we (host_imem_we), .imem_re (host_imem_re), .imem_addr (host_imem_addr),
        .imem_wdata (host_imem_wdata), .imem_rdata (imem_rdata),
        .imem_allowed (~running[0] & ~running[1]),
        .run_we (run_we), .run_val (run_val), .rst_pulse (rst_pulse),
        .pc_we (pc_we), .pc_val (pc_val),
        .running (running), .halted (halted), .blocked (blocked),
        .pc0 (core_pc0), .pc1 (core_pc1),
        .inbox_push (inbox_push), .inbox_wdata (inbox_wdata), .outbox_pop (outbox_pop),
        .outbox0_rdata (outbox_rdata[0]), .outbox1_rdata (outbox_rdata[1]),
        .inbox0_count (inbox_count[0]), .outbox0_count (outbox_count[0]),
        .inbox1_count (inbox_count[1]), .outbox1_count (outbox_count[1]),
        .fifo_clr (fifo_clr),
        .pinmode_we (pinmode_we), .pinmode_val (pinmode_val), .od_mask (od_mask),
        .level (level[15:0]), .uo_out (uo_fw), .uio_out (uio_out), .uio_oe (uio_oe)
    );

    assign uo_out = {uo_fw[7:2], irq, miso};

    wire _unused = &{ena, dbg_retire, dbg_tid, dbg_pc, dbg_ir, 1'b0};
endmodule
