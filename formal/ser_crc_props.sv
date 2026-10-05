// keyer_ser transmit CRC (SEMANTICS 15.1, 15.3), formal/ser.sby tasks
// crc16 and crc32 (bounded model check). One engine; after the reset in
// cycle 0, a SERCFG in cycle 1 (mode NRZI or Manchester, stuffing on or
// off: both free and fixed for the run; CRC-32 by the parameter C32), then
// an arbitrary message of N bytes (free and fixed for the run), all marked
// (SERTXC), each queued in the first cycle the holding register is empty,
// so they form one frame. The owner's timer ticks in every cycle (period 1).
//
// R1  When the data ends (the first cycle with tx_state = CRC), crc_m holds
//     the reference CRC register of the message (bits 31:16 zero for
//     CRC-16).
// R2  The frame, decoded from the pin writes alone (formal/ser_txmon.sv)
//     with the stuffed bits removed (the bit after six ones, 15.3 rule 3),
//     is exactly the message bits, then the W bits of the complemented
//     reference CRC, bit 0 first, then the tail (in Manchester mode the
//     tail's line(1) decodes as one 0); nothing else.
// R3  The frame ends (se0() is written) by a fixed cycle that allows for
//     the worst-case stuffing in either mode.
// R4  Known answers: the reference functions give the published check
//     values on "123456789" (CRC-16/USB B4C8, CRC-32 CBF43926 after the
//     final complement) and the USB CRC-5 field 00010 for the 11-bit token
//     address 0, endpoint 0; and for the message made of the first N bytes
//     of "123456789", crc_m at the end of the data equals the constant
//     computed independently in Python (zlib for CRC-32, MSB-first division
//     for CRC-16), with a cover that this run is reached.
`default_nettype none
module ser_crc_props #(
    parameter integer N   = 2,
    parameter integer C32 = 0,
    parameter integer MODE = 0,     // 1 NRZI, 2 Manchester, 0 either (free)
    parameter integer STUFF = 2,    // 0 off, 1 on, 2 either (free)
    parameter integer FIX = 0       // 1: the message is the known-answer one (cover task)
) (
    input wire clk,
    input wire rst_n
);
    localparam integer W       = C32 ? 32 : 16;
    localparam integer NB      = 8 * N;
    localparam integer REF_MAX = (NB > 72) ? NB : 72;
    localparam integer B       = NB + W;
    // the frame has ended before this cycle: start tick in cycle 3, first bit
    // written in cycle 4, at most B / 6 stuffed bits; NRZI one tick per bit,
    // Manchester two, then its tail's line(1) and six more ticks to se0()
    localparam integer DL1     = 5 + B + B / 6;
    localparam integer DL2     = 4 + 2 * (B + B / 6) + 7;
`include "ser_crc_ref.vh"

    reg f_init = 1'b1;
    always @(posedge clk) f_init <= 1'b0;
    always @(*) assume(rst_n == !f_init);

    wire [71:0]   chk    = f_check_bits(0);              // "123456789" in sending order
    wire [NB-1:0] ka_msg = chk[NB-1:0];                   // its first N bytes
    (* anyconst *) reg [NB-1:0] msg;       // byte j = msg[8j +: 8], sent first to last
    (* anyconst *) reg          m2;        // 1: Manchester, 0: NRZI
    (* anyconst *) reg          st;        // stuffing
    always @(*) begin
        if (MODE == 1) assume(!m2);
        if (MODE == 2) assume(m2);
        if (STUFF == 0) assume(!st);
        if (STUFF == 1) assume(st);
        if (FIX == 1) assume(msg == ka_msg);
    end

    reg [7:0] f_cyc;
    always @(posedge clk) f_cyc <= !rst_n ? 8'd0 : (f_cyc == 8'hFF ? f_cyc : f_cyc + 8'd1);

    wire        tx_full, tx_idle;
    reg  [7:0]  f_sent;
    wire        cfg_we  = (f_cyc == 8'd1);
    wire [7:0]  cfg_val = {2'b00, 1'b0, 1'b0, C32 ? 1'b1 : 1'b0, st, m2 ? 2'd2 : 2'd1};
    wire        tx_we   = (f_cyc >= 8'd2) && !tx_full && (f_sent < N);
    wire [127:0] msg_pad = msg;                            // N <= 16; no out-of-range select
    wire [7:0]  tx_val  = msg_pad[8 * f_sent[3:0] +: 8];
    always @(posedge clk) f_sent <= !rst_n ? 8'd0 : f_sent + (tx_we ? 8'd1 : 8'd0);

    wire        pin_valid, pin_drive, pin_p, pin_n, rx_valid, rx_end;
    wire [1:0]  pin_k, tx_state;
    wire [15:0] rd_st, rd_rx;
    wire [31:0] crc_m;
    keyer_ser dut (
        .clk(clk), .rst_n(rst_n), .cfg_we(cfg_we), .cfg_val(cfg_val), .cfg_tid(1'b0),
        .tx_we(tx_we), .tx_val(tx_val), .tx_val_c(1'b1), .rx_ack(1'b0),
        .period(32'h00000001), .tm_tick(2'b01), .level(8'd0),
        .pin_valid(pin_valid), .pin_k(pin_k), .pin_drive(pin_drive), .pin_p(pin_p), .pin_n(pin_n),
        .rd_st(rd_st), .rd_rx(rd_rx), .tx_full(tx_full), .tx_idle(tx_idle),
        .rx_valid(rx_valid), .rx_end(rx_end),
        // SEMANTICS 15.1 registers, made ports by `expose` in ser.sby
        .crc_m(crc_m), .tx_state(tx_state));

    wire [7:0] cfg;
    wire       owner, act, ev_bit, bit_b, ev_second, second_ok, ev_end, ev_bad, fresh;
    wire [2:0] ones;
    ser_txmon mon (
        .clk(clk), .rst_n(rst_n), .cfg_we(cfg_we), .cfg_val(cfg_val), .cfg_tid(1'b0),
        .pin_valid(pin_valid), .pin_drive(pin_drive), .pin_p(pin_p), .pin_n(pin_n), .tx_idle(tx_idle),
        .cfg(cfg), .owner(owner), .act(act), .ev_bit(ev_bit), .bit_b(bit_b), .ones(ones),
        .ev_second(ev_second), .second_ok(second_ok), .ev_end(ev_end), .ev_bad(ev_bad), .fresh(fresh));

    // ---- the reference ----------------------------------------------------------
    wire [REF_MAX-1:0] msg_bits = msg;                      // already in sending order
    wire [31:0] ref_crc = C32 ? f_crc32(msg_bits, NB) : f_crc16(msg_bits, NB);
    wire [B-1:0] ref_frame = {~ref_crc[W-1:0], msg};        // bit i = i-th unstuffed bit
    // the bits still expected, next one in bit 0 (loaded at reset, shifted per
    // unstuffed bit: no symbolic index into the frame)
    reg [B-1:0] f_exp;

    // ---- R1 ------------------------------------------------------------------------
    reg [1:0] f_state_q;
    always @(posedge clk) f_state_q <= !rst_n ? 2'd0 : tx_state;
    wire data_end = rst_n && tx_state == 2'd2 && f_state_q == 2'd1;

    // ---- R2, R3 -----------------------------------------------------------------------
    reg  [7:0] f_j;            // unstuffed bits seen
    reg        f_ended, f_de;      // f_de: the end of the data was seen
    wire       stuffed = st && ones == 3'd6;
    always @(posedge clk)
        if (!rst_n) begin f_j <= 8'd0; f_ended <= 1'b0; f_de <= 1'b0; f_exp <= ref_frame; end
        else begin
            if (data_end) f_de <= 1'b1;
            if (ev_bit && !stuffed) begin f_j <= f_j + 8'd1; f_exp <= f_exp >> 1; end
            if (ev_end) f_ended <= 1'b1;
        end

    always @(*) if (rst_n) begin
        if (data_end) assert(crc_m == ref_crc);                              // R1
        if (ev_bit && !stuffed) begin                                        // R2
            if (f_j < B) assert(bit_b == f_exp[0]);
            else assert(m2 && f_j == B && !bit_b);
        end
        if (ev_bit && stuffed) assert(!bit_b);
        if (ev_end) assert(!f_ended && f_de && f_j == B + (m2 ? 1 : 0));
        assert(!ev_bad);
        if (f_cyc == (m2 ? DL2 : DL1)) assert(f_ended);                      // R3
    end

    // ---- R4 -------------------------------------------------------------------------------
    localparam [31:0] KA16 = (N == 1) ? 32'h947E : (N == 2) ? 32'hF595 : (N == 3) ? 32'h7A75 :
                             (N == 4) ? 32'h30BA : (N == 5) ? 32'hA471 : (N == 6) ? 32'h32E4 :
                             (N == 7) ? 32'h9D73 : (N == 8) ? 32'h37DD : 32'h4B37;
    localparam [31:0] KA32 = (N == 1) ? 32'h7C231048 : (N == 2) ? 32'hB0ACBB32 : (N == 3) ? 32'h77B79C2D :
                             (N == 4) ? 32'h641C1F5C : (N == 5) ? 32'h340AC5E3 : (N == 6) ? 32'hF68D2C9E :
                             (N == 7) ? 32'hAFFC9660 : (N == 8) ? 32'h651F2550 : 32'h340BC6D9;
    wire [REF_MAX-1:0] chk_bits = chk;
    wire [REF_MAX-1:0] zero_bits = {REF_MAX{1'b0}};
    always @(*) begin
        assert((f_crc16(chk_bits, 72) ^ 32'h0000FFFF) == 32'h0000B4C8);
        assert((f_crc32(chk_bits, 72) ^ 32'hFFFFFFFF) == 32'hCBF43926);
        assert((f_crc5(zero_bits, 11) ^ 5'h1F) == 5'b00010);
        if (rst_n && data_end && msg == ka_msg) assert(crc_m == (C32 ? KA32 : KA16));
    end
    always @(*) if (rst_n) begin
        cover(data_end && msg == ka_msg);                  // the known answer is checked
        cover(ev_end && msg == ka_msg && !m2);             // a whole NRZI frame, CRC appended
        cover(ev_end && msg == ka_msg && m2);              // the same in Manchester mode
    end
endmodule
