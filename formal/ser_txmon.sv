// Transmit-side observer for keyer_ser (SEMANTICS 15.3), used by the
// formal/ser.sby proofs. It sees only what the engine shows outside: the
// SERCFG strobe (to know the configuration), the pin write port and
// tx_idle (status bit 1). From those it decodes the bit stream that the
// transmitter handed to its line coder, emit(b) of 15.3: data bits, CRC
// bits and stuffed bits alike, in both modes. It never reads the engine's
// registers.
//
//   A frame is in progress from the cycle after the start tick (tx_idle
//   falls: rule 2 writes no pin) until the first se0() write (the tail).
//   NRZI (mode 1): each line(s) write is one emitted bit b = (s == previous
//   symbol), the previous symbol being J (s = 0) at the start ("coded from
//   J"). Manchester (mode 2): the writes of a frame alternate first half,
//   line(~b), and second half, line(b); the first half carries the bit
//   (b = N). The Manchester tail's line(1) arrives as a first half and is
//   decoded as one extra 0 after the last real bit; it comes after every
//   real bit, so it cannot hide a run of ones (the CRC proof accounts for
//   it explicitly).
//
// Outputs are combinational for the current cycle c (an event is the write
// the engine makes at the end of c).
`default_nettype none
module ser_txmon (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       cfg_we,
    input  wire [7:0] cfg_val,
    input  wire       cfg_tid,
    input  wire       pin_valid,
    input  wire       pin_drive,
    input  wire       pin_p,
    input  wire       pin_n,
    input  wire       tx_idle,
    output wire [7:0] cfg,        // the configuration during c (15.1)
    output wire       owner,
    output wire       act,        // a frame is in progress during c
    output wire       ev_bit,     // an emitted bit is written at the end of c
    output wire       bit_b,      // ... and its value
    output wire [2:0] ones,       // consecutive ones emitted before this bit (saturates at 7)
    output wire       ev_second,  // a Manchester second-half write
    output wire       second_ok,  // ... and it is the complement of the first half
    output wire       ev_end,     // the se0() write that starts the tail
    output wire       ev_bad,     // a write in a frame that is neither line(s) nor se0()
    output wire       fresh       // c is the first cycle of a frame (the cycle after the start tick)
);
    reg [7:0] f_cfg;
    reg       f_owner;
    reg       f_idle_q, f_act, f_prev, f_ph, f_half;
    reg [2:0] f_ones;

    wire m1 = (f_cfg[1:0] == 2'd1);
    wire m2 = (f_cfg[1:0] == 2'd2);

    assign cfg   = f_cfg;
    assign owner = f_owner;
    assign fresh = f_idle_q & ~tx_idle;
    assign act   = fresh | f_act;
    wire prev    = fresh ? 1'b0 : f_prev;
    wire ph      = fresh ? 1'b0 : f_ph;
    assign ones  = fresh ? 3'd0 : f_ones;

    wire wr      = act & pin_valid & pin_drive;
    wire is_se0  = ~pin_p & ~pin_n;
    wire is_line = pin_p ^ pin_n;
    assign ev_end    = wr & is_se0;
    assign ev_bad    = act & pin_valid & ~(pin_drive & (is_se0 | is_line));
    assign ev_bit    = wr & is_line & (m1 | (m2 & ~ph));
    assign bit_b     = m1 ? (pin_p == prev) : pin_n;
    assign ev_second = wr & is_line & m2 & ph;
    assign second_ok = (pin_p == ~f_half);

    always @(posedge clk) begin
        if (!rst_n) begin
            f_cfg <= 8'd0; f_owner <= 1'b0;
            f_idle_q <= 1'b1; f_act <= 1'b0; f_prev <= 1'b0; f_ph <= 1'b0; f_half <= 1'b0;
            f_ones <= 3'd0;
        end else begin
            f_idle_q <= tx_idle;
            if (cfg_we) begin
                f_cfg <= cfg_val; f_owner <= cfg_tid;
                f_act <= 1'b0; f_prev <= 1'b0; f_ph <= 1'b0; f_ones <= 3'd0;
            end else begin
                f_act <= act & ~ev_end;
                f_prev <= prev; f_ph <= ph; f_ones <= ones;
                if (ev_bit) begin
                    f_ones <= bit_b ? (ones == 3'd7 ? 3'd7 : ones + 3'd1) : 3'd0;
                    if (m1) f_prev <= pin_p;
                    else begin f_ph <= 1'b1; f_half <= pin_p; end
                end
                if (ev_second) f_ph <= 1'b0;
            end
        end
    end
endmodule
