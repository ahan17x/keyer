// keyer_ser transmit CRC for frames of any length (SEMANTICS 15.1, 15.3),
// formal/ser.sby task crc_any (unbounded, abc pdr). The environment is the
// one of ser_stuff_props.sv (SERCFG with any value at any time, so either
// CRC and either mode, stuffing on or off; bytes at any time; any ticks;
// any SERRX, periods and pin levels) except that every byte is marked
// (SERTXC), so the CRC covers the whole frame.
//
// The reference is kept incrementally, bit by bit, in the textbook
// non-reflected form (MSB-first division by 0x8005 or 0x04C11DB7, see
// ser_crc_ref.vh): f_r starts at the all-ones value when a frame starts and
// takes each unstuffed bit decoded from the pin writes (ser_txmon.sv) while
// the transmitter is in DATA (tx_state = 1, a 15.1 register made a port by
// `expose`; only it tells the data bits from the CRC bits of a frame).
//
// Q1  Throughout DATA and CRC, crc_m equals the reflection of f_r (bits
//     31:16 zero for CRC-16): in particular at the end of the data crc_m is
//     the reference CRC of every data bit sent in the frame, whatever the
//     frame length.
// Q2  Each unstuffed bit sent in CRC is the complement of the reference
//     register's next bit (bit 0 of the reflected value first), and there
//     are exactly W of them (16 or 32) before the tail begins.
`default_nettype none
module ser_crcind_props (
    input wire        clk,
    input wire        rst_n,
    input wire        cfg_we,
    input wire [7:0]  cfg_val,
    input wire        cfg_tid,
    input wire        tx_we,
    input wire [7:0]  tx_val,
    input wire        rx_ack,
    input wire [31:0] period,
    input wire [1:0]  tm_tick,
    input wire [7:0]  level
);
    localparam integer REF_MAX = 72;
`include "ser_crc_ref.vh"

    reg f_init = 1'b1;
    always @(posedge clk) f_init <= 1'b0;
    always @(*) if (f_init) assume(!rst_n);

    wire        pin_valid, pin_drive, pin_p, pin_n, tx_full, tx_idle, rx_valid, rx_end;
    wire [1:0]  pin_k, tx_state;
    wire [15:0] rd_st, rd_rx;
    wire [31:0] crc_m;
    keyer_ser dut (
        .clk(clk), .rst_n(rst_n), .cfg_we(cfg_we), .cfg_val(cfg_val), .cfg_tid(cfg_tid),
        .tx_we(tx_we), .tx_val(tx_val), .tx_val_c(1'b1), .rx_ack(rx_ack),
        .period(period), .tm_tick(tm_tick), .level(level),
        .pin_valid(pin_valid), .pin_k(pin_k), .pin_drive(pin_drive), .pin_p(pin_p), .pin_n(pin_n),
        .rd_st(rd_st), .rd_rx(rd_rx), .tx_full(tx_full), .tx_idle(tx_idle),
        .rx_valid(rx_valid), .rx_end(rx_end),
        .crc_m(crc_m), .tx_state(tx_state));

    wire [7:0] cfg;
    wire       owner, act, ev_bit, bit_b, ev_second, second_ok, ev_end, ev_bad, fresh;
    wire [2:0] ones;
    ser_txmon mon (
        .clk(clk), .rst_n(rst_n), .cfg_we(cfg_we), .cfg_val(cfg_val), .cfg_tid(cfg_tid),
        .pin_valid(pin_valid), .pin_drive(pin_drive), .pin_p(pin_p), .pin_n(pin_n), .tx_idle(tx_idle),
        .cfg(cfg), .owner(owner), .act(act), .ev_bit(ev_bit), .bit_b(bit_b), .ones(ones),
        .ev_second(ev_second), .second_ok(second_ok), .ev_end(ev_end), .ev_bad(ev_bad), .fresh(fresh));

    wire        c32     = cfg[3];
    wire        stuffed = cfg[2] && ones == 3'd6;
    wire [31:0] gen     = c32 ? 32'h04C11DB7 : 32'h00008005;
    wire [31:0] mask    = c32 ? 32'hFFFFFFFF : 32'h0000FFFF;

    reg  [31:0] f_r;
    reg  [5:0]  f_nc;          // CRC bits sent
    wire [31:0] r  = fresh ? mask : f_r;
    wire [5:0]  nc = fresh ? 6'd0 : f_nc;
    wire        top     = c32 ? r[31] : r[15];
    wire [31:0] r_next = (((r << 1) ^ ((top ^ bit_b) ? gen : 32'd0)) & mask);
    wire [31:0] refl   = c32 ? f_reflect(r, 32) : f_reflect(r, 16);
    wire        in_data = (tx_state == 2'd1);
    wire        in_crc  = (tx_state == 2'd2);

    always @(posedge clk)
        if (!rst_n) begin f_r <= 32'd0; f_nc <= 6'd0; end
        else begin
            f_r <= r; f_nc <= nc;
            if (ev_bit && !stuffed && in_data) f_r <= r_next;
            if (ev_bit && !stuffed && in_crc) begin f_r <= (r << 1) & mask; f_nc <= nc + 6'd1; end
        end

    always @(*) if (!f_init && rst_n) begin
        if (act && (in_data || in_crc)) assert(crc_m == refl);                     // Q1
        if (ev_bit && !stuffed && in_crc) assert(bit_b == !top);                    // Q2
        if (ev_bit && !stuffed && !in_data && !in_crc && act) assert(nc == (c32 ? 6'd32 : 6'd16));
        if (ev_end) assert(nc == (c32 ? 6'd32 : 6'd16));
    end

    // non-vacuity (task crc_any_cover)
    always @(*) if (!f_init && rst_n) begin
        cover(ev_end && !c32);                                  // a whole CRC-16 frame
        cover(ev_bit && !stuffed && in_crc && c32 && nc == 6'd8);   // CRC-32 bits going out
        cover(ev_bit && stuffed && in_crc);                     // a stuffed bit inside the CRC
    end
endmodule
