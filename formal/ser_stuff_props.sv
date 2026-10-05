// keyer_ser bit stuffing (SEMANTICS 15.3), formal/ser.sby task `stuff`.
// One engine with every input free: SERCFG with any value at any time (so
// any mode, stuffing on or off, any owner, any pair, and an abort of the
// run in the middle of a frame), SERTX/SERTXC with any byte and mark at any
// time, any SERRX and SERST, any period, any tick pattern on either thread,
// any pin levels. The only assumption is a reset in cycle 0. Only the
// engine's ports are observed (formal/ser_txmon.sv decodes the emitted bit
// stream from the pin writes); its registers are not read.
//
// The pins of all four pairs are modelled as the engine's pin port leaves
// them (uio_out / uio_oe of 5.3, 0 after reset), with every uio pin
// push-pull and no other writer: the remaining assumption is that firmware
// pin commands, the replay engine and host PINMODE writes do not touch the
// pair between frames (SEMANTICS 15.3: "only firmware that writes the
// pair's pins itself between frames can break the precondition").
//
// S1  With stuff = 1, in either mode, the bits handed to the line coder in a
//     frame (data, CRC and stuffed bits) never contain seven consecutive
//     ones: an emitted bit that follows six ones is a 0.
// S2  NRZI wire form: with mode 1 and stuff = 1, while a frame is being
//     sent (from the start tick to the se0() that begins the tail) the pair,
//     as last written by the engine, never keeps its value through more
//     than six consecutive symbol ticks. (So at most seven identical symbols
//     follow each other: the symbol that starts a run plus six unchanged
//     ones, which is six coded ones.) Every frame the engine starts, with
//     no precondition: S4 proves the coder's assumption.
// S3  Framing sanity, both modes: inside a frame every engine write is
//     line(s) or se0() (P and N complementary, or both low), never a
//     release; in Manchester mode every second half is the complement of
//     its first half.
// S4  Every frame starts from idle (15.3, DECISIONS D-039): in the first
//     cycle of a frame, uio_out[P] of the configured pair is 0 (J, SE0, or
//     released with J or nothing in the output registers), after reset,
//     after a tail and after an abort.
// S5  An abort (a SERCFG while tx_state != IDLE) leaves the old pair in the
//     idle state of the old mode (15.1): mode 1 both pins released with J
//     in the output registers (uio_oe = 0, uio_out[P] = 0, uio_out[N] = 1),
//     mode 2 both pins driven low; and it is the only engine write of that
//     cycle, to the old pair.
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
    input wire        st_ack,
    input wire [31:0] period,
    input wire [1:0]  tm_tick,
    input wire [7:0]  level
);
    reg f_init = 1'b1;
    always @(posedge clk) f_init <= 1'b0;
    always @(*) if (f_init) assume(!rst_n);

    wire        pin_valid, pin_drive, pin_wout, pin_p, pin_n, tx_full, tx_idle, rx_valid, rx_end;
    wire [1:0]  pin_k;
    wire [15:0] rd_st, rd_rx;
    keyer_ser dut (
        .clk(clk), .rst_n(rst_n), .cfg_we(cfg_we), .cfg_val(cfg_val), .cfg_tid(cfg_tid),
        .tx_we(tx_we), .tx_val(tx_val), .tx_val_c(tx_val_c), .rx_ack(rx_ack), .st_ack(st_ack),
        .period(period), .tm_tick(tm_tick), .level(level),
        .pin_valid(pin_valid), .pin_k(pin_k), .pin_drive(pin_drive), .pin_wout(pin_wout),
        .pin_p(pin_p), .pin_n(pin_n),
        .rd_st(rd_st), .rd_rx(rd_rx), .tx_full(tx_full), .tx_idle(tx_idle),
        .rx_valid(rx_valid), .rx_end(rx_end));

    wire [7:0] cfg;
    wire       owner, act, ev_bit, bit_b, ev_second, second_ok, ev_end, ev_bad, fresh;
    wire [2:0] ones;
    ser_txmon mon (
        .clk(clk), .rst_n(rst_n), .cfg_we(cfg_we), .cfg_val(cfg_val), .cfg_tid(cfg_tid),
        .pin_valid(pin_valid), .pin_drive(pin_drive), .pin_wout(pin_wout), .pin_p(pin_p), .pin_n(pin_n),
        .tx_idle(tx_idle),
        .cfg(cfg), .owner(owner), .act(act), .ev_bit(ev_bit), .bit_b(bit_b), .ones(ones),
        .ev_second(ev_second), .second_ok(second_ok), .ev_end(ev_end), .ev_bad(ev_bad), .fresh(fresh));

    wire       m1 = (cfg[1:0] == 2'd1);
    wire       on = (cfg[1:0] == 2'd1) || (cfg[1:0] == 2'd2);
    wire       st = cfg[2];
    wire [1:0] kk = cfg[7:6];

    // symbol tick (15.1), from the observed configuration and the inputs
    wire tick = on && tm_tick[owner];

    // uio_out / uio_oe as the engine's writes leave them (keyer_pins with
    // every pin push-pull and no other writer; 0 after reset)
    reg  [7:0] f_out, f_oe;
    wire [7:0] wpair = 8'd3 << {pin_k, 1'b0};
    always @(posedge clk)
        if (!rst_n) begin f_out <= 8'd0; f_oe <= 8'd0; end
        else if (pin_valid) begin
            f_oe <= (f_oe & ~wpair) | ({8{pin_drive}} & wpair);
            if (pin_wout) f_out <= (f_out & ~wpair) | ({4{pin_n, pin_p}} & wpair);
        end
    wire f_line = f_out[{kk, 1'b0}];             // uio_out[P] of the configured pair

    // S2: symbol ticks of the frame through which the line has kept its value
    reg  [2:0] f_run;
    wire [2:0] run   = fresh ? 3'd0 : f_run;
    wire       new_p = (pin_valid && pin_wout) ? pin_p : f_line;
    wire       kept  = tick && act && !ev_end && !cfg_we && new_p == f_line;
    always @(posedge clk)
        if (!rst_n) f_run <= 3'd0;
        else if (tick && act && !cfg_we) f_run <= kept ? (run == 3'd7 ? 3'd7 : run + 3'd1) : 3'd0;
        else f_run <= run;

    // S5: an abort in the previous cycle, its old mode and pair
    wire abort = cfg_we && !tx_idle;
    reg       f_ab, f_ab_m1;
    reg [1:0] f_ab_k;
    always @(posedge clk) begin
        f_ab <= rst_n && abort; f_ab_m1 <= m1; f_ab_k <= kk;
    end
    wire [1:0] ab_out = {f_out[{f_ab_k, 1'b1}], f_out[{f_ab_k, 1'b0}]};   // {N, P}
    wire [1:0] ab_oe  = {f_oe[{f_ab_k, 1'b1}],  f_oe[{f_ab_k, 1'b0}]};

    always @(*) if (!f_init && rst_n) begin
        // S1
        if (st && ev_bit) assert(!(bit_b && ones == 3'd6));
        // S2
        if (m1 && st && kept) assert(run < 3'd6);
        // S3
        assert(!ev_bad);
        if (ev_second) assert(second_ok);
        // S4
        if (fresh) assert(!f_line);
        // S5 (the write itself, in the abort cycle)
        if (abort) assert(pin_valid && pin_k == kk);
        if (cfg_we && tx_idle) assert(!pin_valid);
    end
    // S5 (the state it leaves, the next cycle; an engine write of that cycle
    // lands one cycle later)
    always @(*) if (!f_init && f_ab) begin
        if (f_ab_m1) assert(ab_oe == 2'b00 && ab_out == 2'b10);
        else         assert(ab_oe == 2'b11 && ab_out == 2'b00);
    end

    // non-vacuity (task cover_stuff)
    reg f_stuffed;     // a stuffed zero was emitted in the current frame
    always @(posedge clk)
        if (!rst_n || cfg_we || fresh) f_stuffed <= 1'b0;
        else if (st && ev_bit && ones == 3'd6) f_stuffed <= 1'b1;
    always @(*) if (!f_init && rst_n) begin
        cover(m1 && st && ev_end && f_stuffed);                          // a full NRZI frame with a stuffed bit
        cover(!m1 && on && st && ev_end && f_stuffed);                   // the same in Manchester mode
        cover(m1 && st && kept && run == 3'd5);                          // the line held for six ticks
        cover(abort && m1 && f_line);                                    // an NRZI abort with K on the pair
        cover(abort && !m1 && f_line);                                   // a Manchester abort with P high
    end
endmodule
