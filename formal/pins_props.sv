// Formal properties for keyer_pins: open-drain safety, reserved outputs,
// synchroniser latency and edge-history correctness.
`default_nettype none
module pins_props (
    input wire clk, input wire rst_n,
    input wire [7:0] ui_in, input wire [7:0] uio_in,
    input wire cmd_valid, input wire [2:0] cmd_op, input wire [4:0] cmd_pin, input wire [7:0] cmd_data,
    input wire host_mode_we, input wire [7:0] host_mode_val
);
    wire [7:0] uio_out, uio_oe, uo_out, od_mask;
    wire [23:0] level, level2;
    keyer_pins dut (
        .clk(clk), .rst_n(rst_n), .ui_in(ui_in), .uio_in(uio_in),
        .uio_out(uio_out), .uio_oe(uio_oe), .uo_out(uo_out),
        .cmd_valid(cmd_valid), .cmd_op(cmd_op), .cmd_pin(cmd_pin), .cmd_data(cmd_data),
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
endmodule
