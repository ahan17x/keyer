/*
 * Keyer on the Alhambra II (iCE40 HX4K-TQ144, 12 MHz oscillator).
 *
 * A board wrapper around the unmodified Tiny Tapeout top: the design is
 * built with `KEYER_IMEM_FLOPS, whose program memory maps to one 256 x 16
 * block RAM. The core runs on the board's 12 MHz clock, so firmware timing
 * constants are computed for 12 MHz (for instance BAUD_DIV = 104 for
 * 115200 baud).
 *
 * Pins (fpga/alhambra2/alhambra2.pcf, fpga/alhambra2/README.md):
 *   host SPI on the Arduino SPI positions of the top header:
 *     D13 SCK (ui[0]), D11 MOSI (ui[1]), D10 CS_n (ui[2]), D12 MISO (uo[0])
 *   D0..D7   uio[0..7], bidirectional (firmware pins 0-7)
 *   D8, D9   uo[2], uo[3]            DD0, DD1  uo[4], uo[5]
 *   DD2..DD4 ui[3], ui[4], ui[5]     DD5       RST_n (pulled up; low resets)
 *   SW1      ui[6]                   FTDI serial: RX -> ui[7], uo[7] -> TX
 *   LED7..LED2 uo[7..2], LED1 IRQ (uo[1]), LED0 heartbeat (about 0.7 Hz)
 *
 * Reset: a power-on counter holds rst_n low for 255 clocks after
 * configuration (iCE40 flops come up 0), then rst_n follows DD5 through a
 * two-flop synchroniser. uo[6] is on LED6 only.
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module keyer_alhambra2 (
    input  wire CLK,
    input  wire SW1,
    input  wire D13,
    input  wire D11,
    input  wire D10,
    output wire D12,
    inout  wire D0,
    inout  wire D1,
    inout  wire D2,
    inout  wire D3,
    inout  wire D4,
    inout  wire D5,
    inout  wire D6,
    inout  wire D7,
    output wire D8,
    output wire D9,
    output wire DD0,
    output wire DD1,
    input  wire DD2,
    input  wire DD3,
    input  wire DD4,
    input  wire DD5,
    input  wire RX,
    output wire TX,
    output wire LED0,
    output wire LED1,
    output wire LED2,
    output wire LED3,
    output wire LED4,
    output wire LED5,
    output wire LED6,
    output wire LED7
);
    // ---- reset ---------------------------------------------------------------
    // DD5 with the pad's pull-up, so an unconnected pin leaves the chip running
    wire rst_pad;
    SB_IO #(
        .PIN_TYPE (6'b0000_01),          // input, no output
        .PULLUP   (1'b1)
    ) u_rst_io (
        .PACKAGE_PIN (DD5),
        .D_IN_0      (rst_pad)
    );

    reg [7:0]  por;                      // power-on count; flops are 0 after configuration
    reg [1:0]  rst_sync;
    reg        rst_n;
    reg [23:0] beat;
`ifndef SYNTHESIS
    initial begin por = 8'd0; rst_sync = 2'b00; rst_n = 1'b0; beat = 24'd0; end
`endif
    always @(posedge CLK) begin
        if (por != 8'hFF) por <= por + 8'd1;
        rst_sync <= {rst_sync[0], rst_pad};
        rst_n    <= (por == 8'hFF) & rst_sync[1];
        beat     <= beat + 24'd1;
    end

    // ---- bidirectional firmware pins ------------------------------------------
    wire [7:0] uio_in, uio_out, uio_oe;
    SB_IO #(
        .PIN_TYPE (6'b1010_01),          // tristate output, plain input
        .PULLUP   (1'b0)
    ) u_uio [7:0] (
        .PACKAGE_PIN   ({D7, D6, D5, D4, D3, D2, D1, D0}),
        .OUTPUT_ENABLE (uio_oe),
        .D_OUT_0       (uio_out),
        .D_IN_0        (uio_in)
    );

    // ---- the design ------------------------------------------------------------
    wire [7:0] ui_in = {RX, SW1, DD4, DD3, DD2, D10, D11, D13};
    wire [7:0] uo_out;

    tt_um_ahan17x_keyer u_keyer (
        .ui_in   (ui_in),
        .uo_out  (uo_out),
        .uio_in  (uio_in),
        .uio_out (uio_out),
        .uio_oe  (uio_oe),
        .ena     (1'b1),
        .clk     (CLK),
        .rst_n   (rst_n)
    );

    assign D12 = uo_out[0];
    assign D8  = uo_out[2];
    assign D9  = uo_out[3];
    assign DD0 = uo_out[4];
    assign DD1 = uo_out[5];
    assign TX  = uo_out[7];
    assign {LED7, LED6, LED5, LED4, LED3, LED2, LED1} = uo_out[7:1];
    assign LED0 = beat[23];
endmodule
