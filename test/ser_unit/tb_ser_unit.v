// Directed testbench for the serializer (SEMANTICS 15), self-contained: it
// does not use the golden model. It runs the whole chip (core, pins,
// engine) with the program memory as flops, loads each phase's firmware
// (p*.s, assembled by run.sh) straight into u_imem.mem, starts the threads
// with host SPI writes, drives or records the pads, and checks the result
// against vectors derived by hand from the protocols:
//   - line symbols: NRZI (0 = transition, from J) with bit stuffing, USB
//     CRC-16 / CRC-5 and IEEE CRC-32 (catalog check values for
//     "123456789": CRC-16/USB 0xB4C8, CRC-32 0xCBF43926, and the USB token
//     example address 0x3A endpoint 0xA, CRC-5 0x1C in wire order);
//   - Manchester as in IEEE 802.3 (a 1 is low then high);
//   - the exact symbol ticks (the owner's NOW increments) and the cycle
//     rules of 15.3 (first symbol T + 1 cycles after the start tick) and
//     of the blocking and timeout forms (7.4, 15.2).
// Symbols are written J (P low, N high), K (P high, N low), S (SE0, both
// driven low), R (both released).
// Continuous checks, every cycle of every phase: rx_last(c + 1) =
// level(c)[P] (15.1); a cycle in which the receiver does not run is
// followed by its hunt values (15.4); the open-drain invariant.
// Prints "SER_UNIT PASS" or "SER_UNIT FAIL (n errors)".
`default_nettype none
`timescale 1ns / 1ps

module tb_ser_unit;
    reg clk = 1'b0;
    always #5 clk = ~clk;

    reg        rst_n = 1'b0;
    reg  [7:0] ui_in = 8'b0000_0100;     // CS_n high, SCK low
    reg  [7:0] uio_ext = 8'hFF;          // external level of released uio pins
    wire [7:0] uo_out, uio_out, uio_oe;
    wire [7:0] uio_in = (uio_oe & uio_out) | (~uio_oe & uio_ext);

    tt_um_ahan17x_keyer dut (
        .ui_in (ui_in), .uo_out (uo_out), .uio_in (uio_in), .uio_out (uio_out),
        .uio_oe (uio_oe), .ena (1'b1), .clk (clk), .rst_n (rst_n)
    );

    integer errors = 0;
    task fail_msg;
        input [8*96-1:0] msg;
        input integer a, b;
        begin
            errors = errors + 1;
            $display("ERROR: %0s (%0d, %0d)", msg, a, b);
        end
    endtask

    // ---- history, one entry per cycle of the current phase ------------------
    localparam NC = 16384;
    integer cyc;                         // current cycle (cycle 0: first with rst_n high)
    reg     rec = 1'b0;
    reg [1:0] pk = 2'd0;                 // the pair the phase looks at
    reg [7:0]  padc  [0:NC-1];           // pair symbol on the pads
    reg [15:0] now0h [0:NC-1];
    reg [15:0] now1h [0:NC-1];
    reg [1:0]  txsh  [0:NC-1];           // u_ser.tx_state
    reg        txfh  [0:NC-1];           // u_ser.tx_full
    reg [7:0]  holdh [0:NC-1];           // u_ser.tx_hold
    reg [7:0]  uoh   [0:NC-1];
    reg [7:0]  oeh   [0:NC-1];
    reg [7:0]  outh  [0:NC-1];
    reg        blk0h [0:NC-1];           // u_core.blocked[0]
    // commits: cycle and word, in order
    integer    ncm;
    integer    cm_cyc [0:NC-1];
    reg [15:0] cm_ir  [0:NC-1];
    // outbox pushes, either thread
    integer    npush;
    reg [7:0]  push_v [0:255];

    function [7:0] pairc;
        input [7:0] oe, out;
        input [1:0] k;
        reg op, on, vp, vn;
        begin
            op = oe[2*k]; on = oe[2*k+1]; vp = out[2*k]; vn = out[2*k+1];
            if (op & on)
                pairc = ({vp, vn} == 2'b01) ? "J" : ({vp, vn} == 2'b10) ? "K" :
                        ({vp, vn} == 2'b00) ? "S" : "X";
            else if (!op & !on) pairc = "R";
            else pairc = "?";
        end
    endfunction

    // continuous checks use the previous cycle's values
    reg        p_valid = 1'b0;
    reg        p_lvlP, p_run, p_cfg_we;
    wire [7:0] s_cfg = dut.u_ser.cfg;
    wire       s_run = (s_cfg[0] ^ s_cfg[1]) & s_cfg[4] & (dut.u_ser.tx_state == 2'd0);

    always @(negedge clk) if (rec) begin
        cyc = cyc + 1;
        if (cyc < NC) begin
            padc[cyc]  = pairc(uio_oe, uio_out, pk);
            now0h[cyc] = dut.u_core.now[0];
            now1h[cyc] = dut.u_core.now[1];
            txsh[cyc]  = dut.u_ser.tx_state;
            txfh[cyc]  = dut.u_ser.tx_full;
            holdh[cyc] = dut.u_ser.tx_hold;
            uoh[cyc]   = uo_out;
            oeh[cyc]   = uio_oe;
            outh[cyc]  = uio_out;
            blk0h[cyc] = dut.u_core.blocked[0];
            if (dut.u_core.commit && ncm < NC) begin
                cm_cyc[ncm] = cyc; cm_ir[ncm] = dut.u_core.ir; ncm = ncm + 1;
            end
            if (dut.u_core.outbox_push != 2'b00 && npush < 256) begin
                push_v[npush] = dut.u_core.outbox_wdata; npush = npush + 1;
            end
        end
        if (dut.u_core.cyc != cyc[15:0]) fail_msg("cycle numbering", cyc, dut.u_core.cyc);
        if ((dut.u_pins.od_mask & dut.u_pins.uio_out) != 8'd0) fail_msg("open-drain invariant", cyc, 0);
        if (p_valid) begin
            if (dut.u_ser.rx_last !== p_lvlP) fail_msg("rx_last(c+1) != level(c)[P]", cyc, 0);
            if (!p_run || p_cfg_we) begin
                if (dut.u_ser.rx_state !== 1'b0 || dut.u_ser.rx_sh !== 8'hFF || dut.u_ser.rx_n !== 3'd0 ||
                    dut.u_ser.rx_ones !== 3'd0 || dut.u_ser.rx_psym !== 1'b0 || dut.u_ser.rx_cnt !== 16'd0 ||
                    dut.u_ser.rx_w !== 1'b1 || dut.u_ser.rx_first !== 1'b0)
                    fail_msg("receiver not running: hunt values (15.4)", cyc, 0);
            end
        end
        p_valid  = 1'b1;
        p_lvlP   = dut.level[{s_cfg[7:6], 1'b0}];
        p_run    = s_run;
        p_cfg_we = dut.u_ser.cfg_we;
    end

    // ---- host SPI (mode 0, MSB first), half period 5 cycles -----------------
    task spi_byte;
        input [7:0] b;
        integer i;
        begin
            for (i = 7; i >= 0; i = i - 1) begin
                ui_in[1] = b[i];
                repeat (5) @(negedge clk);
                ui_in[0] = 1'b1;
                repeat (5) @(negedge clk);
                ui_in[0] = 1'b0;
            end
        end
    endtask
    task host_write;                     // a register write of n data bytes (1 or 2)
        input [7:0] reg_a;
        input [7:0] d0, d1;
        input integer n;
        begin
            @(negedge clk); ui_in[2] = 1'b0;
            repeat (5) @(negedge clk);
            spi_byte(8'h80 | reg_a);
            spi_byte(d0);
            if (n > 1) spi_byte(d1);
            repeat (5) @(negedge clk);
            ui_in[2] = 1'b1;
            repeat (8) @(negedge clk);
        end
    endtask

    // ---- phase control -----------------------------------------------------
    integer i;
    task start_phase;
        input [8*32-1:0] hexfile;
        input [1:0] pair;
        begin
            rec = 1'b0; p_valid = 1'b0;
            rst_n = 1'b0; ui_in = 8'b0000_0100;
            repeat (4) @(negedge clk);
            for (i = 0; i < 256; i = i + 1) dut.u_imem.mem[i] = 16'd0;
            $readmemh(hexfile, dut.u_imem.mem);
            pk = pair; ncm = 0; npush = 0;
            @(posedge clk); #1;
            rst_n = 1'b1; cyc = -1; rec = 1'b1;
        end
    endtask
    task wait_marker;                    // uo[2] (pass) or uo[3] (fail), or a timeout
        input integer limit;
        integer n;
        begin
            n = 0;
            while (!uo_out[2] && !uo_out[3] && n < limit) begin @(negedge clk); n = n + 1; end
            if (uo_out[3]) fail_msg("firmware reached its fail label", cyc, 0);
            if (!uo_out[2]) fail_msg("firmware did not reach its pass marker", cyc, limit);
        end
    endtask

    // ---- analysis -----------------------------------------------------------
    function [7:0] ch;                   // character i of a string literal of length len
        input [8*400-1:0] s;
        input integer len, i;
        begin
            ch = s[8*(len-1-i) +: 8];
        end
    endfunction
    function is_tick;                    // cycle c is a tick of thread t (NOW increments at its end)
        input integer t, c;
        begin
            is_tick = (t == 0) ? (now0h[c+1] != now0h[c]) : (now1h[c+1] != now1h[c]);
        end
    endfunction

    // Checks a transmitted frame: the first pad change at or after `from`
    // starts it (c0); symbol i must be on the pads in exactly the cycles
    // c0 + i T .. c0 + (i + 1) T - 1 (the last one at least T cycles: a
    // following frame may start after that). c0 - 1 must be a tick of the
    // owner, and the start tick (tx_state
    // IDLE -> DATA) must be T cycles before that. Returns c0.
    integer c0_ret;
    task check_frame;
        input [8*400-1:0] exp;
        input integer len, T, owner, from;
        integer c, c0, st, bad;
        begin
            c0 = -1;
            for (c = from; c < cyc && c0 < 0; c = c + 1)
                if (c > 0 && padc[c] != padc[c-1]) c0 = c;
            if (c0 < 0) fail_msg("no frame found", from, 0);
            else begin
                bad = 0;
                for (c = c0; c < c0 + len * T; c = c + 1) begin
                    if (padc[c] != ch(exp, len, (c - c0) / T)) begin
                        if (bad < 4)
                            $display("  cycle %0d (symbol %0d): pads %s, expected %s", c, (c - c0) / T,
                                     padc[c], ch(exp, len, (c - c0) / T));
                        bad = bad + 1;
                    end
                end
                if (bad) fail_msg("frame symbols differ (cycles)", c0, bad);
                if (!is_tick(owner, c0 - 1)) fail_msg("first symbol not right after a tick", c0, 0);
                st = -1;
                for (c = c0 - 1; c > 0 && st < 0; c = c - 1)
                    if (txsh[c] == 2'd0 && txsh[c+1] == 2'd1) st = c;
                if (st != c0 - 1 - T) fail_msg("start tick is not T cycles before the first symbol tick", st, c0);
                if (!is_tick(owner, st)) fail_msg("start was not a tick", st, 0);
            end
            c0_ret = c0;
        end
    endtask

    // the k-th commit (k = 0 first) of instruction word w, or -1
    function integer commit_of;
        input [15:0] w;
        input integer k;
        integer j, n;
        begin
            commit_of = -1; n = 0;
            for (j = 0; j < ncm; j = j + 1)
                if (cm_ir[j] == w) begin
                    if (n == k && commit_of < 0) commit_of = cm_cyc[j];
                    n = n + 1;
                end
        end
    endfunction
    function integer first_even_at_or_after;
        input integer c;
        begin
            first_even_at_or_after = (c % 2 == 0) ? c : c + 1;
        end
    endfunction

    task expect_pushes;
        input [8*32-1:0] exp;            // bytes, first pushed in the most significant byte
        input integer n, base;
        integer j;
        begin
            if (npush < base + n) fail_msg("too few outbox pushes", npush, base + n);
            for (j = 0; j < n; j = j + 1)
                if (push_v[base + j] !== exp[8*(n-1-j) +: 8]) begin
                    $display("  push %0d: %02x, expected %02x", base + j, push_v[base + j], exp[8*(n-1-j) +: 8]);
                    fail_msg("outbox byte", base + j, 0);
                end
        end
    endtask

    // drive the pair from a symbol string, T cycles per symbol
    task drive_line;
        input [8*400-1:0] s;
        input integer len, T;
        input [1:0] k;
        integer j;
        reg [7:0] c;
        begin
            for (j = 0; j < len; j = j + 1) begin
                c = ch(s, len, j);
                uio_ext[2*k]   = (c == "K");
                uio_ext[2*k+1] = (c == "J");
                repeat (T) @(negedge clk);
            end
        end
    endtask

    // ---- the phases ---------------------------------------------------------------
    integer c0, q, t1, t2, cA, cB, cC, cmt;
    localparam [8*100-1:0] SYM_A = "KJKJKJKKKJKJJJKJKKJKKKJKKKJKKKJKJKKJJJKJJKKJJJKJKKKJJJKJJJJKKKJKJKJJJJKJJKJJJJKJKJKKJKKKJKKJJJKKSSJR";
    localparam [8*30-1:0]  SYM_B = "KJKJKJKKKKKKKJJJJKKKKKKKJKSSJR";
    localparam [8*21-1:0]  SYM_C = "KJKJKJKKJKKKKKKKJSSJR";
    localparam [8*263-1:0] SYM_D = "JKKJJKKJJKKJJKKJJKKJJKKJJKKJJKKJJKKJJKKJJKKJJKJKJKKJKJKJJKJKKJKJKJJKKJKJJKJKKJKJJKJKKJKJJKJKKJKJKJKJJKKJJKJKKJKJJKKJJKKJJKJKKJKJKJJKJKKJJKJKKJKJJKJKJKKJJKJKKJKJKJKJKJJKJKJKKJKJJKKJKJJKJKJKKJKJKJJKJKKJKJJKKJKJJKKJKJJKJKJKKJKJKJKJJKKJJKJKJKJKJKJKKJJKKJKJJKJKKKKKKKS";
    localparam [8*76-1:0]  LINE_E = "KJKJKJKKKKJKJKKKKJKJKJKJJJJJJJKKKJJKJKJKJJKJKKKJKJJKJJJKJJJKJKKKKJJJJKKJJSSJ";
    localparam [8*35-1:0]  LINE_F = "KJKJKJKKKJKJKKKKJJKKKKJKKJJJJJKJSSJ";
    localparam [8*46-1:0]  LINE_G = "KJKJKJKKKKJKJKKKKJKJJKJKJJKJKKJKKKKKKKKJJKJSSJ";
    localparam [8*183-1:0] LINE_H = "JKKJJKKJJKKJJKKJJKKJJKKJJKKJJKKJJKKJJKKJJKKJJKJKJKKJKJKJJKJKKJKJKJJKKJKJJKJKKJKJJKJKKJKJJKJKKJKJKJKJJKKJJKJKKJKJJKJKKJKJKJJKKJJKKJKJKJKJKJJKJKJKJKJKKJKJKJJKJKJKJKJKKJJKJKKJKJJKKKKKKKS";
    localparam [8*28-1:0]  SYM_6 = "JKKJKKJJKKJJJKKJJJJKKKKJSSJR";

    initial begin
        // ======== phase 1: NRZI transmit, stuffing, CRC-16 =========================
        $display("phase 1: NRZI transmit, CRC-16 of \"123456789\"");
        start_phase("p1_nrzi_tx.hex", 2'd0);
        // engine off after reset: rx_sh = FF and rx_w = 1 from the end of cycle 0 (15.4)
        @(negedge clk); @(negedge clk);
        if (dut.u_ser.rx_sh !== 8'hFF || dut.u_ser.rx_w !== 1'b1 || dut.u_ser.cfg !== 8'd0 ||
            dut.u_ser.crc_m !== 32'd0 || dut.u_ser.tx_state !== 2'd0)
            fail_msg("state after reset", cyc, 0);
        host_write(8'h00, 8'h01, 8'h00, 1);           // CTRL: RUN0
        wait_marker(4000);
        repeat (20) @(negedge clk);
        check_frame(SYM_A, 100, 4, 0, 1);
        c0 = c0_ret;
        q = c0 + 99 * 4;                               // first cycle with both pins released
        // SERWT completes at thread 0's first slot >= q; SET 18 two cycles later; pads one after
        for (i = q; i < cyc && !uoh[i][2]; i = i + 1) ;
        if (i != first_even_at_or_after(q) + 3) fail_msg("SERWT completion: marker cycle", i, first_even_at_or_after(q) + 3);
        if (dut.u_core.regs[3] !== 16'd0) fail_msg("SERST after the frame", dut.u_core.regs[3], 0);
        if (dut.u_ser.owner !== 1'b0 || dut.u_ser.cfg !== 8'h05) fail_msg("cfg/owner", dut.u_ser.cfg, dut.u_ser.owner);
        if (outh[q][1:0] !== 2'b10) fail_msg("release keeps uio_out (J: P 0, N 1)", outh[q], 0);

        // ======== phase 2: owner thread 1, pair 2, stuffing ================================
        $display("phase 2: thread 1 owns the engine, pair 2, stuffed zeros");
        start_phase("p2_owner_stuff.hex", 2'd2);
        host_write(8'h03, 8'h40, 8'h00, 2);           // PC1 = 0x40
        host_write(8'h00, 8'h03, 8'h00, 1);           // CTRL: RUN0 | RUN1
        wait_marker(6000);
        repeat (20) @(negedge clk);
        check_frame(SYM_B, 30, 5, 1, 1);
        cB = c0_ret;
        check_frame(SYM_C, 21, 5, 1, cB + 30 * 5);
        if (dut.u_ser.owner !== 1'b1) fail_msg("owner", dut.u_ser.owner, 1);
        for (i = 0; i < cyc; i = i + 1)
            if (oeh[i][3:0] !== 4'd0 || oeh[i][7:6] !== 2'd0) fail_msg("pins outside the pair driven", i, oeh[i]);

        // ======== phase 3: Manchester transmit, CRC-32 ======================================
        $display("phase 3: Manchester transmit, CRC-32 of \"123456789\", six-tick tail");
        start_phase("p3_manch_tx.hex", 2'd1);
        host_write(8'h00, 8'h01, 8'h00, 1);
        wait_marker(4000);
        repeat (20) @(negedge clk);
        check_frame(SYM_D, 263, 2, 0, 1);
        c0 = c0_ret;
        q = c0 + 262 * 2;                              // the final SE0 (driven low), tx_state IDLE
        if (txsh[q] !== 2'd0 || txsh[q-1] !== 2'd3) fail_msg("Manchester tail returns to IDLE with the SE0", q, txsh[q]);
        for (i = q; i < cyc && !uoh[i][2]; i = i + 1) ;
        if (i != first_even_at_or_after(q) + 3) fail_msg("SERWT completion: marker cycle", i, first_even_at_or_after(q) + 3);

        // ======== phase 4: NRZI receive ====================================================
        $display("phase 4: NRZI receive: DATA0 (CRC-16), token (CRC-5), errors");
        uio_ext = 8'hFE;                               // J on pair 0: P (uio0) low, N (uio1) high
        start_phase("p4_nrzi_rx.hex", 2'd0);
        host_write(8'h00, 8'h01, 8'h00, 1);
        repeat (100) @(negedge clk);
        drive_line(LINE_E, 76, 8, 2'd0);
        repeat (200) @(negedge clk);
        drive_line(LINE_F, 35, 8, 2'd0);
        repeat (200) @(negedge clk);
        expect_pushes({8'hC3, 8'h01, 8'hFF, 8'h02, 8'h31, 8'h32, 8'hE3, 8'hAE, 8'h50, 8'h00}, 10, 0);
        expect_pushes({8'hE1, 8'h3A, 8'h3D, 8'h30, 8'h00}, 5, 10);
        host_write(8'h0E, 8'h02, 8'h00, 1);           // FIFOCLR: outbox 0
        fork
            drive_line(LINE_G, 46, 8, 2'd0);
            begin
                // at the frame end, before the firmware's reads: the PID held,
                // the rest lost (overrun), the stuffing error, two stray bits
                wait (dut.u_ser.rx_end === 1'b1);
                @(negedge clk);
                if (!(dut.u_ser.rx_valid === 1'b1 && dut.u_ser.rx_hold === 8'hC3 && dut.u_ser.rx_ovr === 1'b1 &&
                      dut.u_ser.rx_serr === 1'b1 && dut.u_ser.rx_ferr === 1'b1 && dut.u_ser.rx_state === 1'b0))
                    fail_msg("frame G flags at the frame end", dut.u_ser.rx_hold, dut.u_ser.rx_valid);
            end
        join
        wait_marker(2000);
        expect_pushes({8'hC3, 8'h90, 8'h03}, 3, 15);
        if (dut.u_ser.rx_end !== 1'b0 || dut.u_ser.rx_valid !== 1'b0) fail_msg("SERRX clears rx_valid / rx_end", 0, 0);
        if (npush != 18) fail_msg("push count", npush, 18);

        // ======== phase 5: Manchester receive =============================================
        $display("phase 5: Manchester receive, CRC-32, SERRXT");
        uio_ext = 8'hFC;                               // idle: P and N low
        start_phase("p5_manch_rx.hex", 2'd0);
        host_write(8'h03, 8'h40, 8'h00, 2);           // PC1 = 0x40
        host_write(8'h00, 8'h03, 8'h00, 1);           // RUN0 | RUN1
        repeat (100) @(negedge clk);
        drive_line(LINE_H, 183, 4, 2'd0);
        wait_marker(400);
        expect_pushes({8'h31, 8'h32, 8'h33, 8'h34, 8'hA3, 8'hE0, 8'hE3, 8'h9B, 8'h50, 8'h00}, 10, 0);
        if (npush != 10) fail_msg("push count", npush, 10);

        // ======== phase 6: blocking and timeout forms ======================================
        $display("phase 6: blocking SERI, SERTXT, SERWTT, SERRXT timeouts, undefined function");
        uio_ext = 8'hFF;
        start_phase("p6_block.hex", 2'd0);
        host_write(8'h00, 8'h01, 8'h00, 1);
        wait_marker(4000);
        repeat (20) @(negedge clk);
        check_frame(SYM_6, 28, 20, 0, 1);
        // the take ticks: tx_full 1 -> 0
        t1 = -1; t2 = -1;
        for (i = 1; i < cyc; i = i + 1)
            if (txfh[i-1] && !txfh[i]) begin
                if (t1 < 0) t1 = i - 1; else if (t2 < 0) t2 = i - 1;
            end
        cmt = commit_of(16'hFA5A, 0);                  // SERI 0x5A: first slot after the start tick
        if (cmt != first_even_at_or_after(t1 + 1)) fail_msg("blocked SERI completion", cmt, t1);
        if (!blk0h[cmt - 1]) fail_msg("SERI was not blocked before", cmt, 0);
        cA = commit_of(16'hE9F1, 0);                   // SERTXT r4, immediate timeout
        cB = commit_of(16'hE9F1, 1);                   // SERTXT r4, timeout after 3 ticks
        cC = commit_of(16'hE9F1, 2);                   // SERTXT r4, success
        if (cA < 0 || cB < 0 || cC < 0) fail_msg("SERTXT commits", cA, cB);
        if (holdh[cA + 1] !== 8'h5A || !txfh[cA + 1]) fail_msg("timed-out SERTXT queued something", cA, holdh[cA + 1]);
        if (holdh[cB + 1] !== 8'h5A || !(cB < t2)) fail_msg("second SERTXT", cB, t2);
        if (cC != first_even_at_or_after(t2 + 1) || holdh[cC + 1] !== 8'h77 || dut.u_ser.tx_hold_c !== 1'b0)
            fail_msg("SERTXT success", cC, t2);
        if (dut.u_core.regs[7] !== 16'd0 || dut.u_core.regs[5] !== 16'd1 || dut.u_core.regs[6] !== 16'd0)
            fail_msg("phase 6 registers", dut.u_core.regs[7], dut.u_core.regs[5]);

        // ======== phase 7: SERCFG mid-frame in a tick ======================================
        $display("phase 7: SERCFG in the middle of a frame");
        start_phase("p7_sercfg.hex", 2'd0);
        host_write(8'h00, 8'h01, 8'h00, 1);
        wait_marker(400);
        repeat (20) @(negedge clk);
        cmt = commit_of(16'hE1E0, 1);                  // the second SERCFG r0
        if (cmt < 0) fail_msg("no second SERCFG", 0, 0);
        else begin
            if (txsh[cmt] !== 2'd1) fail_msg("not mid-frame", cmt, txsh[cmt]);
            if (!is_tick(0, cmt)) fail_msg("SERCFG not in a tick", cmt, 0);
            if (padc[cmt] == padc[cmt - 2]) fail_msg("the line was not toggling", cmt, 0);
            for (i = cmt + 1; i < cyc; i = i + 1)
                if (padc[i] != padc[cmt]) fail_msg("pins changed at or after SERCFG", i, cmt);
            if (txsh[cmt + 1] !== 2'd0 || txfh[cmt + 1] !== 1'b0) fail_msg("engine not reset by SERCFG", cmt, 0);
        end
        if (dut.u_ser.tx_sh !== 8'd0 || dut.u_ser.tx_n !== 5'd0 || dut.u_ser.tx_line !== 1'b0 ||
            dut.u_ser.crc_m !== 32'd0 || dut.u_ser.tx_hold !== 8'd0 || dut.u_ser.rx_sh !== 8'hFF)
            fail_msg("registers after SERCFG", dut.u_ser.tx_sh, dut.u_ser.tx_n);

        // ======== phase 8: open-drain pin in the pair ======================================
        $display("phase 8: N open-drain, the engine drives P only");
        start_phase("p8_opendrain.hex", 2'd0);
        host_write(8'h0B, 8'h02, 8'h00, 1);           // PINMODE: uio1 open-drain
        host_write(8'h00, 8'h01, 8'h00, 1);
        wait_marker(1000);
        repeat (10) @(negedge clk);
        cA = 0; cB = 0;
        for (i = 0; i < cyc; i = i + 1) begin
            if (oeh[i][1] !== 1'b0 || outh[i][1] !== 1'b0) fail_msg("open-drain N written", i, 0);
            if (oeh[i][0]) cA = cA + 1;
            if (i > 0 && oeh[i][0] && oeh[i-1][0] && outh[i][0] != outh[i-1][0]) cB = cB + 1;
        end
        // 0x00 from J: P = 1 0 1 0 1 0 1 0 (7 transitions), then SE0 SE0 J
        // (P low) and the release: P driven for 11 symbols of 4 cycles
        if (cA != 11 * 4 || cB != 7) fail_msg("P driven cycles / transitions", cA, cB);
        if (oeh[cyc - 1][0] !== 1'b0) fail_msg("P not released at the end", 0, 0);

        if (errors == 0) $display("SER_UNIT PASS");
        else $display("SER_UNIT FAIL (%0d errors)", errors);
        $finish;
    end

    initial begin
        #20000000;
        $display("SER_UNIT FAIL (global timeout)");
        $finish;
    end
endmodule
