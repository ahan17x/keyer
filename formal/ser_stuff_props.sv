// keyer_ser bit stuffing (SEMANTICS 15.3), formal/ser.sby task `stuff`.
// One engine with every input free: SERCFG with any value at any time (so
// any mode, stuffing on or off, any owner, and a reset of the run in the
// middle of a frame), SERTX/SERTXC with any byte and mark at any time, any
// SERRX, any period, any tick pattern on either thread, any pin levels.
// The only assumption is a reset in cycle 0. Only the engine's ports are
// observed (formal/ser_txmon.sv decodes the emitted bit stream from the pin
// writes); its registers are not read.
//
// S1  With stuff = 1, in either mode, the bits handed to the line coder in a
//     frame (data, CRC and stuffed bits) never contain seven consecutive
//     ones: an emitted bit that follows six ones is a 0.
// S2  NRZI wire form: with mode 1 and stuff = 1, while a frame is being
//     sent (from the start tick to the se0() that begins the tail) the pair,
//     as last written by the engine, never keeps its value through more
//     than six consecutive symbol ticks. (So at most seven identical symbols
//     follow each other: the symbol that starts a run plus six unchanged
//     ones, which is six coded ones.) The frame must start with P low as
//     last written (J or SE0, never written since reset, or released after
//     the NRZI tail, where the pull-ups give J): the coder codes the frame
//     from J (15.3), so after a SERCFG that aborted a frame with K on the
//     pair, a first symbol K is not a transition on the wire and S2 does not
//     apply to that frame (S1 still does).
// S3  Framing sanity, both modes: inside a frame every engine write is
//     line(s) or se0() (P and N complementary, or both low), never a
//     release; in Manchester mode every second half is the complement of
//     its first half.
`default_nettype none
module ser_stuff_props (
    input wire        clk,
    input wire        rst_n,
    input wire        cfg_we,
    input wire [7:0]  cfg_val,
    input wire        cfg_tid,
    input wire        tx_we,
    input wire [7:0]  tx_val,
    input wire        tx_val_c,
    input wire        rx_ack,
    input wire [31:0] period,
    input wire [1:0]  tm_tick,
    input wire [7:0]  level
);
    reg f_init = 1'b1;
    always @(posedge clk) f_init <= 1'b0;
    always @(*) if (f_init) assume(!rst_n);

    wire        pin_valid, pin_drive, pin_p, pin_n, tx_full, tx_idle, rx_valid, rx_end;
    wire [1:0]  pin_k;
    wire [15:0] rd_st, rd_rx;
    keyer_ser dut (
        .clk(clk), .rst_n(rst_n), .cfg_we(cfg_we), .cfg_val(cfg_val), .cfg_tid(cfg_tid),
        .tx_we(tx_we), .tx_val(tx_val), .tx_val_c(tx_val_c), .rx_ack(rx_ack),
        .period(period), .tm_tick(tm_tick), .level(level),
        .pin_valid(pin_valid), .pin_k(pin_k), .pin_drive(pin_drive), .pin_p(pin_p), .pin_n(pin_n),
        .rd_st(rd_st), .rd_rx(rd_rx), .tx_full(tx_full), .tx_idle(tx_idle),
        .rx_valid(rx_valid), .rx_end(rx_end));

    wire [7:0] cfg;
    wire       owner, act, ev_bit, bit_b, ev_second, second_ok, ev_end, ev_bad, fresh;
    wire [2:0] ones;
    ser_txmon mon (
        .clk(clk), .rst_n(rst_n), .cfg_we(cfg_we), .cfg_val(cfg_val), .cfg_tid(cfg_tid),
        .pin_valid(pin_valid), .pin_drive(pin_drive), .pin_p(pin_p), .pin_n(pin_n), .tx_idle(tx_idle),
        .cfg(cfg), .owner(owner), .act(act), .ev_bit(ev_bit), .bit_b(bit_b), .ones(ones),
        .ev_second(ev_second), .second_ok(second_ok), .ev_end(ev_end), .ev_bad(ev_bad), .fresh(fresh));

    wire m1 = (cfg[1:0] == 2'd1);
    wire on = (cfg[1:0] == 2'd1) || (cfg[1:0] == 2'd2);
    wire st = cfg[2];

    // symbol tick (15.1), from the observed configuration and the inputs
    wire tick = on && tm_tick[owner];

    // the pair's P as last written by the engine (uio_out[P] of 5.3; the
    // pins are push-pull here); 0 after reset like uio_out
    reg f_line;
    always @(posedge clk)
        if (!rst_n) f_line <= 1'b0;
        else if (pin_valid && pin_drive) f_line <= pin_p;

    // the frame started from P low
    reg  f_j0;
    wire j0 = fresh ? !f_line : f_j0;
    always @(posedge clk) f_j0 <= rst_n && j0;

    // S2: symbol ticks of the frame through which the line has kept its value
    reg  [2:0] f_run;
    wire [2:0] run   = fresh ? 3'd0 : f_run;
    wire       new_p = (pin_valid && pin_drive) ? pin_p : f_line;
    wire       kept  = tick && act && !ev_end && !cfg_we && new_p == f_line;
    always @(posedge clk)
        if (!rst_n) f_run <= 3'd0;
        else if (tick && act && !cfg_we) f_run <= kept ? (run == 3'd7 ? 3'd7 : run + 3'd1) : 3'd0;
        else f_run <= run;

    always @(*) if (!f_init && rst_n) begin
        // S1
        if (st && ev_bit) assert(!(bit_b && ones == 3'd6));
        // S2
        if (m1 && st && j0 && kept) assert(run < 3'd6);
        // S3
        assert(!ev_bad);
        if (ev_second) assert(second_ok);
    end

    // non-vacuity (task cover_stuff)
    reg f_stuffed;     // a stuffed zero was emitted in the current frame
    always @(posedge clk)
        if (!rst_n || cfg_we || fresh) f_stuffed <= 1'b0;
        else if (st && ev_bit && ones == 3'd6) f_stuffed <= 1'b1;
    always @(*) if (!f_init && rst_n) begin
        cover(m1 && st && ev_end && f_stuffed);                          // a full NRZI frame with a stuffed bit
        cover(!m1 && on && st && ev_end && f_stuffed);                   // the same in Manchester mode
        cover(m1 && st && j0 && kept && run == 3'd5);                         // the line held for six ticks
    end
endmodule
