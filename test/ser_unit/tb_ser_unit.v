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
// Phases 7, 9 and 10: a SERCFG that aborts a frame returns the old pair to
// idle (15.1, 15.2, DECISIONS D-039); one with the transmitter IDLE writes
// no pin. Phases 11 and 12: rx_drop (15.5, 15.7), through the core and on a
// second engine (u_eng) whose strobes the bench drives directly, so that a
// SERRX or SERST can be placed in the very cycle of a frame start.
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
    reg        cfgh  [0:NC-1];           // u_ser.cfg_we (a SERCFG commits)
    reg        droph [0:NC-1];           // u_ser.rx_drop
    reg        rxsth [0:NC-1];           // u_ser.rx_state
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
            cfgh[cyc]  = dut.u_ser.cfg_we;
            droph[cyc] = dut.u_ser.rx_drop;
            rxsth[cyc] = dut.u_ser.rx_state;
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
    // the cycle of the k-th SERCFG commit (k = 0 first), or -1
    function integer cfg_of;
        input integer k;
        integer j, n;
        begin
            cfg_of = -1; n = 0;
            for (j = 0; j < cyc && j < NC; j = j + 1)
                if (cfgh[j]) begin
                    if (n == k && cfg_of < 0) cfg_of = j;
                    n = n + 1;
                end
        end
    endfunction
    // the cycle in which rx_state shows the k-th frame start (k = 0 first), or -1
    function integer rise_of;
        input integer k;
        integer j, n;
        begin
            rise_of = -1; n = 0;
            for (j = 1; j < cyc && j < NC; j = j + 1)
                if (rxsth[j] && !rxsth[j-1]) begin
                    if (n == k && rise_of < 0) rise_of = j;
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

    // NRZI frames (no stuffing): sync, one byte, end of packet, four idle J
    localparam [8*23-1:0]  FRAME_00 = "KJKJKJKKJKJKJKJKSSJJJJJ";
    localparam [8*23-1:0]  FRAME_5A = "KJKJKJKKJJKKKJJKSSJJJJJ";
    localparam [8*23-1:0]  FRAME_3C = "KJKJKJKKJKKKKKJKSSJJJJJ";
    localparam [8*16-1:0]  FRAME_PART = "KJKJKJKKJKJKJKJK";     // sync and one byte, no end

    // ---- a second engine, driven directly (phase 12) --------------------------
    // Receiver on pair 0, T = 8 from thread 0's period; e_tick drives the
    // transmitter's symbol ticks. e_fs_ack / e_fs_st arm a SERRX / SERST
    // strobe for the next cycle in which a frame starts (the engine's own
    // frame-start term r_fs, which depends only on its state and the level,
    // not on the strobes); the strobe is made in that cycle only and the arm
    // clears itself.
    reg        e_cfg_we = 1'b0, e_tx_we = 1'b0, e_m_ack = 1'b0, e_m_st = 1'b0;
    reg        e_fs_ack = 1'b0, e_fs_st = 1'b0, e_tick = 1'b0;
    reg  [7:0] e_cfg_val = 8'd0, e_lvl = 8'hFE;
    wire       e_rx_ack = e_m_ack | (e_fs_ack & u_eng.r_fs);
    wire       e_st_ack = e_m_st  | (e_fs_st  & u_eng.r_fs);
    wire       e_pv, e_pd, e_pw, e_pp, e_pn, e_full, e_idle, e_rv, e_re;
    wire [1:0] e_pk;
    wire [15:0] e_st, e_rx;
    keyer_ser u_eng (
        .clk (clk), .rst_n (rst_n), .cfg_we (e_cfg_we), .cfg_val (e_cfg_val), .cfg_tid (1'b0),
        .tx_we (e_tx_we), .tx_val (8'h00), .tx_val_c (1'b0), .rx_ack (e_rx_ack), .st_ack (e_st_ack),
        .period (32'h0000_0008), .tm_tick ({1'b0, e_tick}), .level (e_lvl),
        .pin_valid (e_pv), .pin_k (e_pk), .pin_drive (e_pd), .pin_wout (e_pw), .pin_p (e_pp), .pin_n (e_pn),
        .rd_st (e_st), .rd_rx (e_rx), .tx_full (e_full), .tx_idle (e_idle),
        .rx_valid (e_rv), .rx_end (e_re)
    );
    integer e_nfs = 0;                   // frame starts seen
    always @(posedge clk) begin
        if (!rst_n) e_nfs = 0;
        else if (u_eng.r_fs) begin
            e_nfs = e_nfs + 1;
            e_fs_ack <= 1'b0; e_fs_st <= 1'b0;
        end
    end
    task e_cfg;
        input [7:0] v;
        begin
            @(negedge clk); e_cfg_val = v; e_cfg_we = 1'b1;
            @(negedge clk); e_cfg_we = 1'b0;
        end
    endtask
    task e_ack;                          // a SERRX completing (rx_valid or rx_end must be 1)
        begin
            if (!(e_rv || e_re)) fail_msg("u_eng: SERRX with nothing to take (bench)", 0, 0);
            @(negedge clk); e_m_ack = 1'b1;
            @(negedge clk); e_m_ack = 1'b0;
        end
    endtask
    task e_st_pulse;                     // a SERST
        begin
            @(negedge clk); e_m_st = 1'b1;
            @(negedge clk); e_m_st = 1'b0;
        end
    endtask
    task e_drive;
        input [8*400-1:0] s;
        input integer len;
        integer j;
        reg [7:0] c;
        begin
            for (j = 0; j < len; j = j + 1) begin
                c = ch(s, len, j);
                e_lvl[0] = (c == "K");
                e_lvl[1] = (c == "J");
                repeat (8) @(negedge clk);
            end
        end
    endtask
    task e_check;
        input integer nfs;
        input drop, valid, rend;
        input integer id;
        begin
            if (e_nfs != nfs || u_eng.rx_drop !== drop || e_rv !== valid || e_re !== rend) begin
                $display("  u_eng check %0d: frame starts %0d (exp %0d), rx_drop %b (exp %b), rx_valid %b (exp %b), rx_end %b (exp %b)",
                         id, e_nfs, nfs, u_eng.rx_drop, drop, e_rv, valid, e_re, rend);
                fail_msg("u_eng rx_drop check", id, 0);
            end
        end
    endtask
    // a receive in DATA with one byte held, abandoned by a transmitter start
    // (15.4, D-039): no frame end, no verdict, no flag; then back to idle J
    task e_abandon;
        integer n;
        begin
            e_drive(FRAME_PART, 16);
            if (u_eng.rx_state !== 1'b1 || e_rv !== 1'b1 || e_re !== 1'b0) fail_msg("u_eng: partial frame", e_rv, e_re);
            @(negedge clk); e_tx_we = 1'b1;
            @(negedge clk); e_tx_we = 1'b0; e_tick = 1'b1;   // the start tick is this cycle
            @(negedge clk);
            if (e_idle !== 1'b0) fail_msg("u_eng: transmitter did not start", 0, 0);
            @(negedge clk);
            if (u_eng.rx_state !== 1'b0 || e_rv !== 1'b1 || e_re !== 1'b0 || u_eng.rx_ferr !== 1'b0 ||
                u_eng.rx_ovr !== 1'b0 || u_eng.rx_serr !== 1'b0 || u_eng.rx_c5ok !== 1'b0 || u_eng.rx_cok !== 1'b0)
                fail_msg("u_eng: receive not abandoned cleanly at the transmitter start", e_rv, e_re);
            n = 0;
            while (!e_idle && n < 200) begin
                @(negedge clk); n = n + 1;
                if (e_re !== 1'b0) fail_msg("u_eng: rx_end during the transmission", n, 0);
            end
            e_tick = 1'b0;
            if (!e_idle) fail_msg("u_eng: transmitter did not finish", 0, 0);
            e_drive("JJJJ", 4);
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
            if (padc[cmt] != "J" && padc[cmt] != "K") fail_msg("pair not driven before the abort", cmt, padc[cmt]);
            // the abort writes idle(1, P, N) of the old configuration
            // (D-039): both pins released, J (P 0, N 1) in the output
            // registers, from the cycle after the SERCFG on
            for (i = cmt + 1; i < cyc; i = i + 1)
                if (padc[i] != "R" || outh[i][1:0] !== 2'b10)
                    fail_msg("NRZI abort: pair not idle (released, J in uio_out)", i, cmt);
            for (i = 0; i < cyc; i = i + 1)
                if (oeh[i][7:2] !== 6'd0 || outh[i][7:2] !== 6'd0) fail_msg("pins outside the pair written", i, 0);
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

        // ======== phase 9: Manchester abort, then SERCFG with the transmitter IDLE ===========
        $display("phase 9: SERCFG aborts a Manchester frame (pair 1 driven low); SERCFG when IDLE writes no pin");
        start_phase("p9_abort_manch.hex", 2'd1);
        host_write(8'h00, 8'h01, 8'h00, 1);
        wait_marker(1000);
        repeat (10) @(negedge clk);
        cA = cfg_of(1); cB = cfg_of(2);
        if (cA < 0 || cB < 0 || cfg_of(3) >= 0) fail_msg("SERCFG commits", cA, cB);
        else begin
            if (txsh[cA] === 2'd0) fail_msg("first SERCFG not mid-frame", cA, 0);
            if (padc[cA] != "J" && padc[cA] != "K") fail_msg("pair not driven before the abort", cA, padc[cA]);
            if (outh[cA][2] !== 1'b1) fail_msg("P not high at the abort (test does not discriminate)", cA, outh[cA]);
            for (i = cA + 1; i < cyc; i = i + 1)
                if (padc[i] != "S" || oeh[i][3:2] !== 2'b11 || outh[i][3:2] !== 2'b00)
                    fail_msg("Manchester abort: pair not idle (both driven low)", i, cA);
            if (txsh[cB] !== 2'd0) fail_msg("second SERCFG not with the transmitter IDLE", cB, txsh[cB]);
            for (i = cB + 1; i < cyc; i = i + 1)
                if (oeh[i] !== oeh[cB] || outh[i] !== outh[cB]) fail_msg("SERCFG with tx IDLE wrote a pin", i, cB);
        end
        for (i = 0; i < cyc; i = i + 1)
            if (oeh[i][1:0] !== 2'd0 || oeh[i][7:4] !== 4'd0) fail_msg("pins outside the pair driven", i, oeh[i]);
        if (dut.u_ser.cfg !== 8'h41) fail_msg("final cfg", dut.u_ser.cfg, 8'h41);

        // ======== phase 10: aborts with an open-drain pin in the pair ======================
        $display("phase 10: NRZI and Manchester aborts with N open-drain");
        start_phase("p10_abort_od.hex", 2'd0);
        host_write(8'h0B, 8'h02, 8'h00, 1);           // PINMODE: uio1 open-drain
        host_write(8'h00, 8'h01, 8'h00, 1);
        wait_marker(1000);
        repeat (10) @(negedge clk);
        cA = cfg_of(1); cB = cfg_of(2);
        if (cA < 0 || cB < 0) fail_msg("SERCFG commits", cA, cB);
        else begin
            if (txsh[cA] === 2'd0 || txsh[cB] === 2'd0) fail_msg("aborts not mid-frame", cA, cB);
            if (oeh[cA][0] !== 1'b1 || oeh[cB][0] !== 1'b1) fail_msg("P not driven before an abort", cA, cB);
            if (outh[cA][0] !== 1'b1 || outh[cB][0] !== 1'b1) fail_msg("P not high at an abort (test does not discriminate)", cA, cB);
            // NRZI abort: P released with uio_out[P] = 0 (J's P)
            if (oeh[cA + 1][0] !== 1'b0 || outh[cA + 1][0] !== 1'b0) fail_msg("NRZI abort: P not idle", cA, outh[cA + 1]);
            // Manchester abort: P driven low, to the end
            for (i = cB + 1; i < cyc; i = i + 1)
                if (oeh[i][0] !== 1'b1 || outh[i][0] !== 1'b0) fail_msg("Manchester abort: P not driven low", i, cB);
        end
        for (i = 0; i < cyc; i = i + 1)
            if (oeh[i][1] !== 1'b0 || outh[i][1] !== 1'b0) fail_msg("open-drain N written", i, 0);

        // ======== phase 11: rx_drop through the core =======================================
        $display("phase 11: rx_drop set by a frame start, cleared by SERST and by SERRX returning the status");
        uio_ext = 8'hFE;                               // J on pair 0
        start_phase("p11_drop.hex", 2'd0);
        host_write(8'h00, 8'h01, 8'h00, 1);
        repeat (100) @(negedge clk);
        drive_line(FRAME_00, 23, 8, 2'd0);             // A
        repeat (50) @(negedge clk);
        drive_line(FRAME_5A, 23, 8, 2'd0);             // B: drops A's byte and frame end
        repeat (50) @(negedge clk);
        ui_in[4] = 1'b1;
        for (i = 0; i < 400 && npush < 4; i = i + 1) @(negedge clk);
        ui_in[4] = 1'b0;
        repeat (50) @(negedge clk);
        drive_line(FRAME_00, 23, 8, 2'd0);             // C: drops B's
        repeat (50) @(negedge clk);
        drive_line(FRAME_3C, 23, 8, 2'd0);             // D: drops C's
        repeat (50) @(negedge clk);
        ui_in[4] = 1'b1;
        wait_marker(1000);
        if (npush != 9) fail_msg("push count", npush, 9);
        else begin
            // status bits: 2 rx_valid, 4 rx_end, 10 rx_drop (5, 6: CRC verdicts, not checked)
            if ((push_v[0] & 8'h9F) !== 8'h14 || push_v[1] !== 8'h04) fail_msg("first SERST: drop set", push_v[0], push_v[1]);
            if ((push_v[2] & 8'h9F) !== 8'h14 || push_v[3] !== 8'h00) fail_msg("second SERST: drop cleared", push_v[2], push_v[3]);
            if (push_v[4] !== 8'h3C) fail_msg("D's byte", push_v[4], 8'h3C);
            if ((push_v[5] & 8'h9F) !== 8'h10 || push_v[6] !== 8'h04) fail_msg("SERRX status: drop as it was", push_v[5], push_v[6]);
            if ((push_v[7] & 8'h9F) !== 8'h00 || push_v[8] !== 8'h00) fail_msg("SERST after SERRX status: cleared", push_v[7], push_v[8]);
        end
        // the bit rises in the cycle that shows B's (C's) frame start, not at A's
        q = rise_of(0); t1 = rise_of(1); t2 = rise_of(2);
        if (q < 0 || t1 < 0 || t2 < 0 || rise_of(3) < 0 || rise_of(4) >= 0) fail_msg("frame starts", q, t1);
        else begin
            if (droph[q] !== 1'b0) fail_msg("rx_drop set at A's start", q, 0);
            if (droph[t1 - 1] !== 1'b0 || droph[t1] !== 1'b1) fail_msg("rx_drop not set at B's start", t1, 0);
            if (droph[t2 - 1] !== 1'b0 || droph[t2] !== 1'b1) fail_msg("rx_drop not set at C's start", t2, 0);
        end
        if (dut.u_ser.rx_drop !== 1'b0) fail_msg("rx_drop at the end", 0, 0);

        // ======== phase 12: rx_drop rules on a directly driven engine ======================
        $display("phase 12: rx_drop same-cycle rules (u_eng); a transmitter start abandons a receive");
        start_phase("p7_sercfg.hex", 2'd0);            // threads not started
        e_lvl = 8'hFE;
        e_cfg(8'h11);                                  // NRZI, receiver on, pair 0
        repeat (40) @(negedge clk);
        e_drive(FRAME_00, 23);                         // A
        e_check(1, 1'b0, 1'b1, 1'b1, 1);
        e_ack;                                         // takes A's byte; its frame end stays
        e_check(1, 1'b0, 1'b0, 1'b1, 2);
        e_drive(FRAME_00, 23);                         // B: A's frame end untaken: set
        e_check(2, 1'b1, 1'b1, 1'b1, 3);
        e_ack;                                         // a SERRX returning a byte keeps it
        e_check(2, 1'b1, 1'b0, 1'b1, 4);
        e_fs_ack = 1'b1;
        e_drive(FRAME_00, 23);                         // C: a SERRX at the start takes B's end:
        e_check(3, 1'b0, 1'b1, 1'b1, 5);               //    not set, and the status read clears
        if (e_fs_ack !== 1'b0) fail_msg("u_eng: SERRX at C's start not made", 0, 0);
        e_fs_ack = 1'b1;
        e_drive(FRAME_00, 23);                         // D: SERRX takes C's byte, C's end dropped: set
        e_check(4, 1'b1, 1'b1, 1'b1, 6);
        if (e_st[10] !== 1'b1 || e_rx[10] !== 1'b0) fail_msg("u_eng: status bit 10 / byte read", e_st, e_rx);
        e_st_pulse;                                    // SERST clears
        e_check(4, 1'b0, 1'b1, 1'b1, 7);
        if (e_st[10] !== 1'b0) fail_msg("u_eng: status bit 10 after SERST", e_st, 0);
        e_fs_st = 1'b1;
        e_drive(FRAME_00, 23);                         // E: SERST at the start, D's byte and end dropped:
        e_check(5, 1'b1, 1'b1, 1'b1, 8);               //    the set wins
        if (e_fs_st !== 1'b0) fail_msg("u_eng: SERST at E's start not made", 0, 0);
        e_ack;                                         // E's byte: kept
        e_check(5, 1'b1, 1'b0, 1'b1, 9);
        if (e_rx[10] !== 1'b1 || e_rx[4] !== 1'b1 || e_rx[2] !== 1'b0) fail_msg("u_eng: SERRX status word", e_rx, 0);
        e_ack;                                         // E's end: the status (bit as it was), cleared
        e_check(5, 1'b0, 1'b0, 1'b0, 10);
        e_drive(FRAME_00, 23);                         // F: nothing untaken: not set
        e_check(6, 1'b0, 1'b1, 1'b1, 11);
        e_drive(FRAME_00, 23);                         // G: F's byte and end dropped: set
        e_check(7, 1'b1, 1'b1, 1'b1, 12);
        e_cfg(8'h11);                                  // SERCFG clears (and everything else)
        e_check(7, 1'b0, 1'b0, 1'b0, 13);
        repeat (40) @(negedge clk);
        e_abandon;                                     // H: abandoned, its byte held, no frame end
        e_drive(FRAME_00, 23);                         // I: H's byte alone untaken: set
        e_check(9, 1'b1, 1'b1, 1'b1, 14);
        e_st_pulse; e_ack; e_ack;                      // clear; take I's byte and end
        e_check(9, 1'b0, 1'b0, 1'b0, 15);
        e_abandon;                                     // J: abandoned with its byte held
        e_fs_ack = 1'b1;
        e_drive(FRAME_00, 23);                         // K: SERRX at the start takes J's byte: not set
        e_check(11, 1'b0, 1'b1, 1'b1, 16);
        if (e_fs_ack !== 1'b0) fail_msg("u_eng: SERRX at K's start not made", 0, 0);

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
