// Reference CRCs for the formal/ser.sby proofs, included into a module body.
// Written from the polynomials of SEMANTICS 15.1, not from the reflected
// shift-register form the engine uses: a CRC here is MSB-first polynomial
// division of the bits in the order they are sent, with the generator
// written the ordinary (non-reflected) way, and the register is reflected
// at the end. That is the textbook definition of a reflected CRC
// (refin = refout = true), and reflected arithmetic is never used:
//
//   CRC-16/USB  x^16 + x^15 + x^2 + 1          0x8005      init 0xFFFF
//   CRC-32      IEEE 802.3                      0x04C11DB7  init 0xFFFFFFFF
//   CRC-5/USB   x^5 + x^2 + 1                   0x05        init 0x1F
//
// (all-ones initial values are the same reflected or not). The known-answer
// assertions in the property files check these functions on "123456789"
// (CRC-16/USB B4C8 and CRC-32 CBF43926 after the final complement) and on
// the USB token with address 0 and endpoint 0 (CRC-5 field 00010).
//
// Requires localparam integer REF_MAX (the longest bit string).

    function [31:0] f_reflect(input [31:0] v, input integer w);
        integer i;
        begin
            f_reflect = 32'd0;
            for (i = 0; i < 32; i = i + 1)
                if (i < w) f_reflect[i] = v[w - 1 - i];
        end
    endfunction

    // bits[i] is the i-th bit sent; the first nbits are used
    function [31:0] f_crc_div(input [REF_MAX-1:0] bits, input integer nbits,
                              input integer w, input [31:0] gen, input [31:0] init);
        integer i;
        reg [31:0] r;
        reg fb;
        begin
            r = init;
            for (i = 0; i < REF_MAX; i = i + 1)
                if (i < nbits) begin
                    fb = r[w - 1] ^ bits[i];
                    r = r << 1;
                    if (fb) r = r ^ gen;
                    r = r & ((w == 32) ? 32'hFFFFFFFF : ((32'd1 << w) - 32'd1));
                end
            f_crc_div = f_reflect(r, w);
        end
    endfunction

    // the register the engine should hold after the bits (no final complement)
    function [31:0] f_crc16(input [REF_MAX-1:0] bits, input integer nbits);
        f_crc16 = f_crc_div(bits, nbits, 16, 32'h00008005, 32'h0000FFFF);
    endfunction
    function [31:0] f_crc32(input [REF_MAX-1:0] bits, input integer nbits);
        f_crc32 = f_crc_div(bits, nbits, 32, 32'h04C11DB7, 32'hFFFFFFFF);
    endfunction
    function [4:0] f_crc5(input [REF_MAX-1:0] bits, input integer nbits);
        f_crc5 = f_crc_div(bits, nbits, 5, 32'h00000005, 32'h0000001F);
    endfunction

    // "123456789" as bits in sending order (byte 0 = '1' first, LSB first)
    function [71:0] f_check_bits(input integer dummy);
        integer j;
        reg [71:0] s;
        begin
            s = "123456789";
            for (j = 0; j < 9; j = j + 1) f_check_bits[8*j +: 8] = s[8*(8-j) +: 8];
        end
    endfunction
