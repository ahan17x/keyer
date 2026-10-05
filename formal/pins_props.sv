// Formal properties for keyer_pins: open-drain safety, reserved outputs,
// synchroniser latency and edge-history correctness. The replay command port
// (SEMANTICS 14.7) is a free input like the core's, so property 1 also covers
// replay writes to open-drain pins and property 2 replay writes to group 4.
// The serializer's write port (SEMANTICS 15.1, 15.3) is free as well, so
// property 1 covers every serializer write, alone or together with the
// core's command, the replay and a host PINMODE write in the same cycle;
// property 6 states the serializer's own write rule.
`default_nettype none
module pins_props (
    input wire clk, input wire rst_n,
    input wire [7:0] ui_in, input wire [7:0] uio_in,
    input wire cmd_valid, input wire [2:0] cmd_op, input wire [4:0] cmd_pin, input wire [7:0] cmd_data,
    input wire rep_valid, input wire [2:0] rep_group, input wire [3:0] rep_mask, input wire [3:0] rep_data,
    input wire ser_valid, input wire [1:0] ser_k, input wire ser_drive, input wire ser_p, input wire ser_n,
    input wire host_mode_we, input wire [7:0] host_mode_val
);
    wire [7:0] uio_out, uio_oe, uo_out, od_mask;
    wire [23:0] level, level2;
    keyer_pins dut (
        .clk(clk), .rst_n(rst_n), .ui_in(ui_in), .uio_in(uio_in),
        .uio_out(uio_out), .uio_oe(uio_oe), .uo_out(uo_out),
        .cmd_valid(cmd_valid), .cmd_op(cmd_op), .cmd_pin(cmd_pin), .cmd_data(cmd_data),
        .rep_valid(rep_valid), .rep_group(rep_group), .rep_mask(rep_mask), .rep_data(rep_data),
        .ser_valid(ser_valid), .ser_k(ser_k), .ser_drive(ser_drive), .ser_p(ser_p), .ser_n(ser_n),
        .host_mode_we(host_mode_we), .host_mode_val(host_mode_val), .od_mask(od_mask),
        .level(level), .level2(level2));

    reg init = 1'b1;
    always @(posedge clk) init <= 1'b0;
    always @(*) if (init) assume(!rst_n);

    reg [3:0] since_reset;
    always @(posedge clk) if (!rst_n) since_reset <= 0; else if (since_reset != 4'hF) since_reset <= since_reset + 1;

    always @(*) if (!init && rst_n) begin
        // 1. An open-drain pin is never driven high, whatever the command history.
        assert((od_mask & uio_out) == 8'd0);
        // 2. uo[0] and uo[1] belong to the host interface: firmware can never set them.
        assert(uo_out[1:0] == 2'b00);
        // 3. level for 16-23 is the driven output register.
        assert(level[23:16] == uo_out);
    end

    // 4. Synchroniser: level is the pad level from two cycles earlier (after the
    //    pipeline has filled following reset).
    always @(posedge clk) if (!init && rst_n && since_reset >= 3) begin
        assert(level[15:0] == $past({ui_in, uio_in}, 2));
        // 5. level2 is level from two cycles earlier.
        assert(level2 == $past(level, 2));
    end

    // 6. A serializer write alone (no core command, replay or PINMODE in the
    //    cycle): open-drain pins and pins outside the pair keep uio_out and
    //    uio_oe; a push-pull pin of the pair takes uio_oe <= ser_drive and,
    //    when driven, uio_out <= its value (P = 2k gets ser_p, N = 2k+1
    //    ser_n); od_mask is unchanged.
    wire [7:0] f_pair = 8'd3 << {ser_k, 1'b0};
    wire [7:0] f_val  = {4{ser_n, ser_p}};
    wire [7:0] f_w    = f_pair & ~od_mask;      // the pair's push-pull pins
    always @(posedge clk) if (!init && rst_n && $past(rst_n) && since_reset >= 1
                              && $past(ser_valid && !cmd_valid && !rep_valid && !host_mode_we)) begin
        assert(od_mask == $past(od_mask));
        assert((uio_oe  & ~$past(f_w)) == ($past(uio_oe)  & ~$past(f_w)));
        assert((uio_out & ~$past(f_w)) == ($past(uio_out) & ~$past(f_w)));
        assert((uio_oe  &  $past(f_w)) == $past({8{ser_drive}} & f_w));
        if ($past(ser_drive))
            assert((uio_out & $past(f_w)) == $past(f_val & f_w));
        else
            assert((uio_out & $past(f_w)) == $past(uio_out & f_w));
    end
endmodule
