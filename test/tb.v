`default_nettype none
`timescale 1ns / 1ps

/* Testbench wrapper for cocotb. The bidirectional pads are modelled: uio_in
   follows the DUT's own drive when uio_oe is set and the external level
   (uio_ext, driven by the test) otherwise. */
module tb ();

`ifdef DUMP
  initial begin
    $dumpfile("tb.fst");
    $dumpvars(0, tb);
    #1;
  end
`endif

  reg clk;
  reg rst_n;
  reg ena;
  reg [7:0] ui_in;
  reg [7:0] uio_ext;
  wire [7:0] uio_in;
  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;

  assign uio_in = (uio_oe & uio_out) | (~uio_oe & uio_ext);

`ifdef GL_TEST
  wire VPWR = 1'b1;
  wire VGND = 1'b0;
`endif

  tt_um_ahan17x_loom user_project (
`ifdef GL_TEST
      .VPWR   (VPWR),
      .VGND   (VGND),
`endif
      .ui_in  (ui_in),
      .uo_out (uo_out),
      .uio_in (uio_in),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (ena),
      .clk    (clk),
      .rst_n  (rst_n)
  );

endmodule
