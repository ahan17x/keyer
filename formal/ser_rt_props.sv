// keyer_ser round trip (SEMANTICS 15.3 to 15.6, 5.2), formal/ser.sby tasks
// rt_nrzi and rt_manch (bounded model check). Two engines with the same
// configuration and the same owner timer (period T, thread 0, tick phase
// free): the first one transmits, its pin writes become pad levels of the
// pair uio[2k], uio[2k+1] (k free; push-pull, so a write drives the pad;
// a released pin shows the bus pull: P low, N high, i.e. J), the pads pass
// through the two-flop synchroniser of 5.2 (level(c) = pad(c - 2)) and feed
// the second engine's level input; the other six pins carry arbitrary
// levels. The second engine receives, and its consumer takes every byte
// and the frame end in the cycle they appear (an always-ready SERRX).
//
// Frame: the sync byte (0x80 NRZI, 0xD5 Manchester), unmarked, then an
// arbitrary message of N bytes (free, fixed for the run), marked, except
// that with rxskip = 1 (free) the first message byte is unmarked (a PID:
// the receiver leaves it out of its CRCs), then the appended CRC. Stuffing
// is on in NRZI mode and free in Manchester mode. CRC-16 or CRC-32 by C32.
//
// T1  The receiver delivers exactly the message bytes followed by the W/8
//     bytes of the complemented reference CRC (ser_crc_ref.vh) of the
//     marked bytes, in order, and nothing else; then it reports the frame
//     end once, with rx_cok = 1 (CRC verdict good), rx_ferr = 0,
//     rx_serr = 0 and rx_ovr = 0.
// T2  At that frame end the receiver's crc5 equals the reference CRC-5
//     register of the bits it covered (every bit after the sync, without
//     the first byte if rxskip = 1), and rx_c5ok says whether that value is
//     the residual 0x06.
// T3  The frame end is reported by a fixed cycle (no stall).
`default_nettype none
module ser_rt_props #(
    parameter integer MODE = 1,      // 1 NRZI (stuffing on), 2 Manchester (stuffing free)
    parameter integer T    = 4,      // symbol period in cycles
    parameter integer N    = 2,      // message bytes
    parameter integer C32  = 0,
    parameter integer FIX  = 0       // 1: a fixed message with a stuffed bit, phase 0 (cover task)
) (
    input wire clk,
    input wire rst_n
);
    localparam integer W       = C32 ? 32 : 16;
    localparam integer NB      = 8 * N;
    localparam integer B       = NB + W;                  // bits after the sync
    localparam integer NBY     = N + W / 8;               // bytes delivered
    localparam integer REF_MAX = (B > 72) ? B : 72;
    localparam [7:0]   SYNC    = (MODE == 1) ? 8'h80 : 8'hD5;
    // symbols on the wire (sync, data, CRC, stuffed bits, tail), generously
    localparam integer SYMS    = (MODE == 1) ? (8 + B + (8 + B) / 6 + 3) : (2 * (8 + B + (8 + B) / 6) + 7);
    localparam integer DL      = T * (SYMS + 6) + 12;
`include "ser_crc_ref.vh"

    reg f_init = 1'b1;
    always @(posedge clk) f_init <= 1'b0;
    always @(*) assume(rst_n == !f_init);

    (* anyconst *) reg [NB-1:0] msg;
    (* anyconst *) reg [1:0]    kk;
    (* anyconst *) reg          skip;
    (* anyconst *) reg          st_free;
    (* anyconst *) reg [15:0]   phase;
    (* anyseq *)   reg [7:0]    other;      // the other six pads
    always @(*) assume(phase < T);
    // with one message byte and rxskip it would be unmarked and the frame
    // would carry no CRC at all (15.3): that case is not a CRC round trip
    always @(*) if (N == 1) assume(!skip);
    // cover run: 0x7E (six ones, so a stuffed bit with stuffing on), then 0xFF ...
    wire [255:0] fixmsg = {{31{8'hFF}}, 8'h7E};
    always @(*) if (FIX == 1) assume(msg == fixmsg[NB-1:0] && phase == 16'd0 && other == 8'd0);
    wire st = (MODE == 1) ? 1'b1 : st_free;

    reg [8:0] f_cyc;
    always @(posedge clk) f_cyc <= !rst_n ? 9'd0 : (f_cyc == 9'h1FF ? f_cyc : f_cyc + 9'd1);

    // the owner's timer: period T, tick when prescale = 0 (6.1)
    reg [15:0] f_pre;
    always @(posedge clk) f_pre <= !rst_n ? phase : (f_pre == 16'd0 ? T - 1 : f_pre - 16'd1);
    wire [1:0]  tm_tick = {1'b0, f_pre == 16'd0};
    wire [31:0] period  = {16'd0, T[15:0]};

    // configuration, written to both engines in cycle 1 by thread 0
    wire       cfg_we  = (f_cyc == 9'd1);
    wire [7:0] cfg_val = {kk, skip, 1'b1, C32 ? 1'b1 : 1'b0, st, (MODE == 1) ? 2'd1 : 2'd2};

    // the transmitter's queue: SYNC, then the message
    wire        a_full, a_idle;
    reg  [7:0]  f_sent;
    wire        tx_we   = (f_cyc >= 9'd2) && !a_full && f_sent < N + 1;
    wire [255:0] q      = {msg, SYNC};             // N <= 31; no out-of-range select
    wire [7:0]  tx_val  = q[8 * f_sent[4:0] +: 8];
    wire        tx_c    = (f_sent != 8'd0) && !(skip && f_sent == 8'd1);
    always @(posedge clk) f_sent <= !rst_n ? 8'd0 : f_sent + (tx_we ? 8'd1 : 8'd0);

    wire        a_pv, a_drive, a_wout, a_p, a_n, a_rv, a_re;
    wire [1:0]  a_k, a_state;
    wire [15:0] a_st, a_rx;
    wire [31:0] a_crc;
    wire [4:0]  a_crc5;
    keyer_ser tx (
        .clk(clk), .rst_n(rst_n), .cfg_we(cfg_we), .cfg_val(cfg_val), .cfg_tid(1'b0),
        .tx_we(tx_we), .tx_val(tx_val), .tx_val_c(tx_c), .rx_ack(1'b0), .st_ack(1'b0),
        .period(period), .tm_tick(tm_tick), .level(8'd0),
        .pin_valid(a_pv), .pin_k(a_k), .pin_drive(a_drive), .pin_wout(a_wout), .pin_p(a_p), .pin_n(a_n),
        .rd_st(a_st), .rd_rx(a_rx), .tx_full(a_full), .tx_idle(a_idle),
        .rx_valid(a_rv), .rx_end(a_re), .crc_m(a_crc), .tx_state(a_state), .crc5(a_crc5));

    // pads of the pair (5.3: registered, on the pad from the next cycle)
    reg f_out_p, f_out_n, f_oe_p, f_oe_n;
    always @(posedge clk)
        if (!rst_n) begin f_out_p <= 1'b0; f_out_n <= 1'b0; f_oe_p <= 1'b0; f_oe_n <= 1'b0; end
        else if (a_pv) begin
            f_oe_p <= a_drive; f_oe_n <= a_drive;
            if (a_wout) begin f_out_p <= a_p; f_out_n <= a_n; end
        end
    wire pad_p = f_oe_p ? f_out_p : 1'b0;          // released: the bus pulls give J
    wire pad_n = f_oe_n ? f_out_n : 1'b1;
    wire [7:0] pair = 8'd3 << {kk, 1'b0};
    wire [7:0] pad  = (other & ~pair) | ({4{pad_n, pad_p}} & pair);

    // synchroniser (5.2)
    reg [7:0] f_s1, f_s2;
    always @(posedge clk)
        if (!rst_n) begin f_s1 <= 8'd0; f_s2 <= 8'd0; end
        else begin f_s1 <= pad; f_s2 <= f_s1; end

    // the receiver and its always-ready consumer
    wire        b_pv, b_drive, b_wout, b_p, b_n, b_full, b_idle, b_rv, b_re;
    wire [1:0]  b_k, b_state;
    wire [15:0] b_st, b_rx;
    wire [31:0] b_crc;
    wire [4:0]  b_crc5;
    wire        ack = b_rv || b_re;
    keyer_ser rx (
        .clk(clk), .rst_n(rst_n), .cfg_we(cfg_we), .cfg_val(cfg_val), .cfg_tid(1'b0),
        .tx_we(1'b0), .tx_val(8'd0), .tx_val_c(1'b0), .rx_ack(ack), .st_ack(1'b0),
        .period(period), .tm_tick(tm_tick), .level(f_s2),
        .pin_valid(b_pv), .pin_k(b_k), .pin_drive(b_drive), .pin_wout(b_wout), .pin_p(b_p), .pin_n(b_n),
        .rd_st(b_st), .rd_rx(b_rx), .tx_full(b_full), .tx_idle(b_idle),
        .rx_valid(b_rv), .rx_end(b_re), .crc_m(b_crc), .tx_state(b_state), .crc5(b_crc5));

    // ---- reference ----------------------------------------------------------------
    // CRC over the marked message bytes: all of them, or all but the first
    wire [REF_MAX-1:0] all_bits  = msg;
    wire [REF_MAX-1:0] skip_bits = msg >> 8;
    wire [31:0] ref_crc = skip ? (C32 ? f_crc32(skip_bits, NB - 8) : f_crc16(skip_bits, NB - 8))
                               : (C32 ? f_crc32(all_bits, NB) : f_crc16(all_bits, NB));
    wire [B-1:0] frame = {~ref_crc[W-1:0], msg};              // the bits after the sync
    wire [REF_MAX-1:0] fr_all  = frame;
    wire [REF_MAX-1:0] fr_skip = frame >> 8;
    wire [4:0] ref5 = skip ? f_crc5(fr_skip, B - 8) : f_crc5(fr_all, B);

    // ---- T1 to T3 -------------------------------------------------------------------
    reg [B-1:0] f_exp;            // bytes still expected, next in bits 7:0
    reg [7:0]   f_d;              // bytes delivered
    reg         f_ended, f_good;
    wire        take_byte = ack && b_rv;
    wire        take_end  = ack && !b_rv && b_re;
    always @(posedge clk)
        if (!rst_n) begin f_exp <= frame; f_d <= 8'd0; f_ended <= 1'b0; f_good <= 1'b0; end
        else begin
            if (take_byte) begin f_exp <= f_exp >> 8; f_d <= f_d + 8'd1; end
            if (take_end) f_ended <= 1'b1;
            if (take_end && b_rx[6] && !b_rx[9] && !b_rx[8] && !b_rx[7]) f_good <= 1'b1;
        end

    always @(*) if (rst_n) begin
        if (take_byte) assert(!f_ended && f_d < NBY && b_rx[7:0] == f_exp[7:0]);       // T1
        if (take_end) begin
            assert(!f_ended && f_d == NBY);                                         // T1
            assert(b_rx[6] && !b_rx[9] && !b_rx[8] && !b_rx[7]);                    // rx_cok, no ferr/serr/ovr
            assert(b_crc5 == ref5 && b_rx[5] == (ref5 == 5'h06));                   // T2
        end
        if (f_cyc == DL) assert(f_ended);                                           // T3
    end

    // non-vacuity (task rt_cover): the first message byte through the round
    // trip. That the whole frame and a good verdict are reached is not left
    // to a cover: T3 asserts it for every run, and the assumptions above
    // constrain only constants and inputs, so they cannot cut a run short.
    always @(*) if (rst_n) cover(take_byte && f_d == 8'd0);
endmodule
