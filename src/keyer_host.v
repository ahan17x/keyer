/*
 * Keyer host interface: SPI slave (mode 0, MSB first) with a register map.
 * See docs/isa.md section 6 and docs/SEMANTICS.md section 10. SCK must be at
 * most clk / 8.
 *
 * A transaction is: CS_n low, command byte (bit 7 = write, bits 6:0 =
 * register), then data bytes until CS_n rises. For reads, data byte k is
 * presented on MISO during the (k+1)-th byte after the command; the slave
 * loads the next byte on the falling SCK edge that ends the previous byte.
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none
`include "keyer_isa.vh"

module keyer_host (
    input  wire        clk,
    input  wire        rst_n,
    // SPI pads
    input  wire        sck,
    input  wire        mosi,
    input  wire        csn,
    output reg         miso,
    output wire        irq,
    // program memory port (granted only while both threads are stopped)
    output reg         imem_we,
    output reg         imem_re,
    output wire [7:0]  imem_addr,
    output wire [15:0] imem_wdata,
    input  wire [15:0] imem_rdata,
    input  wire        imem_allowed,
    // thread control
    output reg         run_we,
    output reg  [1:0]  run_val,
    output reg  [1:0]  rst_pulse,
    output reg  [1:0]  pc_we,
    output wire [7:0]  pc_val,
    input  wire [1:0]  running,
    input  wire [1:0]  halted,
    input  wire [1:0]  blocked,
    input  wire [7:0]  pc0,             // PC[0], PC[1], read back through PC0 / PC1
    input  wire [7:0]  pc1,
    // FIFOs
    output reg  [1:0]  inbox_push,
    output wire [7:0]  inbox_wdata,
    output reg  [1:0]  outbox_pop,
    input  wire [7:0]  outbox0_rdata,
    input  wire [7:0]  outbox1_rdata,
    input  wire [4:0]  inbox0_count,
    input  wire [4:0]  outbox0_count,
    input  wire [4:0]  inbox1_count,
    input  wire [4:0]  outbox1_count,
    output reg  [3:0]  fifo_clr,
    // pins
    output reg         pinmode_we,
    output wire [7:0]  pinmode_val,
    input  wire [7:0]  od_mask,
    input  wire [15:0] level,           // {ui, uio} synchronised
    input  wire [7:0]  uo_out,
    input  wire [7:0]  uio_out,
    input  wire [7:0]  uio_oe,
    // capture and replay (SEMANTICS 10.3, 10.4, 14): one-cycle write pulses
    // landing at the end of the cycle, like RUN; two-byte registers pulse
    // once, after byte 1, with {byte 1, byte 0}
    output reg         cr_ctrl_we,
    output wire [7:0]  cr_ctrl_val,
    output reg         cap_cfg_we,
    output wire [15:0] cap_cfg_val,
    output reg         cap_buf_we,
    output wire [15:0] cap_buf_val,
    output reg         rep_cfg_we,
    output wire [7:0]  rep_cfg_val,
    output reg         rep_buf_we,
    output wire [15:0] rep_buf_val,
    input  wire [7:0]  cr_status,       // CR_CTRL read (14.8)
    input  wire [15:0] cr_count,        // CR_COUNT read {byte 1, byte 0}
    input  wire [15:0] cap_cfg_rd,
    input  wire [15:0] cap_buf_rd,
    input  wire [7:0]  rep_cfg_rd,
    input  wire [15:0] rep_buf_rd
);
    localparam R_CTRL = 7'h00, R_STAT = 7'h01, R_PC0 = 7'h02, R_PC1 = 7'h03,
               R_IMEM_ADDR = 7'h04, R_IMEM_DATA = 7'h05, R_INBOX0 = 7'h06,
               R_OUTBOX0 = 7'h07, R_INBOX1 = 7'h08, R_OUTBOX1 = 7'h09,
               R_LEVELS = 7'h0A, R_PINMODE = 7'h0B, R_IRQEN = 7'h0C,
               R_PINS = 7'h0D, R_FIFOCLR = 7'h0E, R_ID = 7'h0F, R_PINOUT = 7'h10,
               R_CR_CTRL = 7'h11, R_CAP_CFG = 7'h12, R_CAP_BUF = 7'h13,
               R_REP_CFG = 7'h14, R_REP_BUF = 7'h15, R_CR_COUNT = 7'h16;

    // ---- synchronisers and edge detection ---------------------------------
    reg [2:0] sck_s, mosi_s, csn_s;
    always @(posedge clk) begin
        if (!rst_n) begin sck_s <= 3'b000; mosi_s <= 3'b000; csn_s <= 3'b111; end
        else begin
            sck_s  <= {sck_s[1:0], sck};
            mosi_s <= {mosi_s[1:0], mosi};
            csn_s  <= {csn_s[1:0], csn};
        end
    end
    wire sck_rise = sck_s[1] & ~sck_s[2];
    wire sck_fall = ~sck_s[1] & sck_s[2];
    wire active   = ~csn_s[1];
    wire mosi_q   = mosi_s[1];

    // ---- receive -----------------------------------------------------------
    reg [2:0] bitcnt;
    reg [6:0] shift_in;
    reg       byte_done;            // one-cycle pulse: rx_byte holds a complete byte
    reg [7:0] rx_byte;
    reg [7:0] byte_idx;             // 0 = command byte, n = n-th data byte (saturating)
    reg       imem_hi;              // IMEM_DATA phase: the next data byte is a high byte
    reg       is_write;
    reg [6:0] reg_sel;
    reg       load_pending;         // present the next read byte on the next falling edge
    reg [7:0] tx_shift;
    reg [7:0] imem_addr_q;
    reg [7:0] imem_lo;              // IMEM_DATA low byte; byte 0 of a two-byte write
    reg [15:0] imem_rd_q;
    reg       imem_re_d;
    reg [7:0] irq_en;
    reg [7:0] wdata_q;

    wire       cmd_phase = (byte_idx == 8'd0);
    wire [7:0] data_idx  = byte_idx - 8'd1;        // index of the data byte just finished

    always @(posedge clk) begin
        if (!rst_n || !active) begin
            bitcnt <= 3'd0; byte_done <= 1'b0; byte_idx <= 8'd0; load_pending <= 1'b0;
            shift_in <= 7'd0; rx_byte <= 8'd0; imem_hi <= 1'b0;
            if (!rst_n) begin is_write <= 1'b0; reg_sel <= 7'd0; end
        end else begin
            byte_done <= 1'b0;
            if (sck_rise) begin
                bitcnt <= bitcnt + 3'd1;
                shift_in <= {shift_in[5:0], mosi_q};
                if (bitcnt == 3'd7) begin
                    byte_done <= 1'b1;
                    rx_byte <= {shift_in, mosi_q};
                end
            end
            if (byte_done) begin
                if (byte_idx != 8'hFF) byte_idx <= byte_idx + 8'd1;
                // IMEM_DATA alternates low/high for the whole transaction, however
                // long (SEMANTICS 10.2); byte_idx saturates, so it has its own phase
                imem_hi <= ~cmd_phase & (reg_sel == R_IMEM_DATA) & ~imem_hi;
                if (cmd_phase) begin
                    is_write <= rx_byte[7];
                    reg_sel  <= rx_byte[6:0];
                    load_pending <= ~rx_byte[7];
                end else if (!is_write) begin
                    load_pending <= 1'b1;
                end
            end
            if (sck_fall && load_pending) load_pending <= 1'b0;
        end
    end

    // ---- read data mux -----------------------------------------------------
    // value of the register byte that will be loaded next (index = byte_idx,
    // the data byte about to be clocked out, counting from 0)
    wire [7:0] next_idx = cmd_phase ? 8'd0 : byte_idx;   // after cmd byte_done, byte_idx is 1 => data byte 0
    reg  [7:0] rd_byte;
    always @(*) begin
        rd_byte = 8'd0;
        case (reg_sel)
            R_CTRL:    rd_byte = {6'd0, running};
            R_STAT:    rd_byte = {2'd0, blocked, halted, running};
            // two-byte registers: the value, then 0 (SEMANTICS 10.4)
            R_PC0:       rd_byte = byte_idx[0] ? pc0 : 8'd0;
            R_PC1:       rd_byte = byte_idx[0] ? pc1 : 8'd0;
            R_IMEM_ADDR: rd_byte = byte_idx[0] ? imem_addr_q : 8'd0;
            R_IMEM_DATA: rd_byte = imem_hi ? imem_rd_q[15:8] : imem_rd_q[7:0];
            R_OUTBOX0: rd_byte = outbox0_rdata;
            R_OUTBOX1: rd_byte = outbox1_rdata;
            R_LEVELS: case (byte_idx[1:0])
                2'd1: rd_byte = {3'd0, inbox0_count};
                2'd2: rd_byte = {3'd0, outbox0_count};
                2'd3: rd_byte = {3'd0, inbox1_count};
                default: rd_byte = {3'd0, outbox1_count};
            endcase
            R_PINMODE: rd_byte = od_mask;
            R_IRQEN:   rd_byte = irq_en;
            R_PINS: case (byte_idx[1:0])
                2'd1: rd_byte = level[7:0];
                2'd2: rd_byte = level[15:8];
                2'd3: rd_byte = uo_out;
                default: rd_byte = 8'd0;
            endcase
            R_ID:     rd_byte = byte_idx[0] ? 8'h4B : `KEYER_ISA_VERSION;
            R_PINOUT: rd_byte = byte_idx[0] ? uio_out : uio_oe;
            R_CR_CTRL:  rd_byte = cr_status;
            R_CAP_CFG:  rd_byte = byte_idx[0] ? cap_cfg_rd[7:0] : cap_cfg_rd[15:8];
            R_CAP_BUF:  rd_byte = byte_idx[0] ? cap_buf_rd[7:0] : cap_buf_rd[15:8];
            R_REP_CFG:  rd_byte = rep_cfg_rd;
            R_REP_BUF:  rd_byte = byte_idx[0] ? rep_buf_rd[7:0] : rep_buf_rd[15:8];
            R_CR_COUNT: rd_byte = byte_idx[0] ? cr_count[7:0] : cr_count[15:8];
            default:  rd_byte = 8'd0;
        endcase
    end

    // ---- transmit ----------------------------------------------------------
    always @(posedge clk) begin
        if (!rst_n || !active) begin
            tx_shift <= 8'd0; miso <= 1'b0;
        end else begin
            if (sck_fall) begin
                if (load_pending) tx_shift <= rd_byte;
                else tx_shift <= {tx_shift[6:0], 1'b0};
            end
            miso <= sck_fall ? (load_pending ? rd_byte[7] : tx_shift[6]) : tx_shift[7];
        end
    end

    // ---- register writes and side effects ---------------------------------
    assign pc_val      = wdata_q;
    assign inbox_wdata = wdata_q;
    assign pinmode_val = wdata_q;
    assign imem_addr   = imem_addr_q;
    assign imem_wdata  = {rx_byte, imem_lo};
    assign cr_ctrl_val = wdata_q;
    assign rep_cfg_val = wdata_q;
    assign cap_cfg_val = {wdata_q, imem_lo};
    assign cap_buf_val = {wdata_q, imem_lo};
    assign rep_buf_val = {wdata_q, imem_lo};

    wire data_done = byte_done && !cmd_phase;
    wire cmd_done  = byte_done && cmd_phase;

    always @(posedge clk) begin
        if (!rst_n) begin
            run_we <= 1'b0; run_val <= 2'b00; rst_pulse <= 2'b00; pc_we <= 2'b00;
            inbox_push <= 2'b00; outbox_pop <= 2'b00; fifo_clr <= 4'd0; pinmode_we <= 1'b0;
            imem_we <= 1'b0; imem_re <= 1'b0; imem_addr_q <= 8'd0; imem_lo <= 8'd0;
            imem_rd_q <= 16'd0; imem_re_d <= 1'b0; irq_en <= 8'd0; wdata_q <= 8'd0;
            cr_ctrl_we <= 1'b0; cap_cfg_we <= 1'b0; cap_buf_we <= 1'b0;
            rep_cfg_we <= 1'b0; rep_buf_we <= 1'b0;
        end else begin
            run_we <= 1'b0; rst_pulse <= 2'b00; pc_we <= 2'b00; inbox_push <= 2'b00;
            outbox_pop <= 2'b00; fifo_clr <= 4'd0; pinmode_we <= 1'b0;
            imem_we <= 1'b0; imem_re <= 1'b0;
            cr_ctrl_we <= 1'b0; cap_cfg_we <= 1'b0; cap_buf_we <= 1'b0;
            rep_cfg_we <= 1'b0; rep_buf_we <= 1'b0;
            imem_re_d <= imem_re;
            if (imem_re_d) imem_rd_q <= imem_rdata;
            if (imem_we) imem_addr_q <= imem_addr_q + 8'd1;
            wdata_q <= rx_byte;

            if (data_done && is_write) begin
                // low byte of a pair: every even IMEM_DATA byte (imem_hi has
                // its own phase because byte_idx saturates), byte 0 otherwise
                if (!imem_hi && !data_idx[0]) imem_lo <= rx_byte;
                case (reg_sel)
                    R_CTRL: if (data_idx == 8'd0) begin
                        run_we <= 1'b1; run_val <= rx_byte[1:0]; rst_pulse <= rx_byte[3:2];
                    end
                    R_PC0: if (data_idx == 8'd0) pc_we[0] <= 1'b1;
                    R_PC1: if (data_idx == 8'd0) pc_we[1] <= 1'b1;
                    R_IMEM_ADDR: if (data_idx == 8'd0) imem_addr_q <= rx_byte;
                    R_IMEM_DATA: if (imem_hi && imem_allowed) imem_we <= 1'b1;   // address increments after the write cycle
                    R_INBOX0: inbox_push[0] <= 1'b1;
                    R_INBOX1: inbox_push[1] <= 1'b1;
                    R_PINMODE: if (data_idx == 8'd0) pinmode_we <= 1'b1;
                    R_IRQEN:   if (data_idx == 8'd0) irq_en <= rx_byte;
                    R_FIFOCLR: if (data_idx == 8'd0) fifo_clr <= rx_byte[3:0];
                    R_CR_CTRL: if (data_idx == 8'd0) cr_ctrl_we <= 1'b1;
                    R_CAP_CFG: if (data_idx == 8'd1) cap_cfg_we <= 1'b1;
                    R_CAP_BUF: if (data_idx == 8'd1) cap_buf_we <= 1'b1;
                    R_REP_CFG: if (data_idx == 8'd0) rep_cfg_we <= 1'b1;
                    R_REP_BUF: if (data_idx == 8'd1) rep_buf_we <= 1'b1;
                    default: ;
                endcase
            end
            if (!is_write || cmd_done) begin
                // reads: fetch the first word at the command, pop at each byte end
                if (cmd_done && !rx_byte[7] && rx_byte[6:0] == R_IMEM_DATA && imem_allowed)
                    imem_re <= 1'b1;
                if (data_done && reg_sel == R_IMEM_DATA && imem_hi && imem_allowed) begin
                    imem_addr_q <= imem_addr_q + 8'd1;
                    imem_re <= 1'b1;
                end
                if (data_done && reg_sel == R_OUTBOX0) outbox_pop[0] <= 1'b1;
                if (data_done && reg_sel == R_OUTBOX1) outbox_pop[1] <= 1'b1;
            end
        end
    end

    // IRQ (section 11): bit 6 capture done, bit 7 replay done (14.8)
    wire [7:0] irq_cond = {cr_status[5], cr_status[2],
                           inbox1_count == 5'd0, inbox0_count == 5'd0, halted,
                           outbox1_count != 5'd0, outbox0_count != 5'd0};
    assign irq = |(irq_cond & irq_en);
endmodule
