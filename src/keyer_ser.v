/*
 * Keyer: serializer engine (docs/SEMANTICS.md section 15, design note
 * docs/SERIALIZER.md, DECISIONS D-036).
 *
 * One engine shared by both threads: a transmitter and a receiver for the
 * pin pair P = uio[2k], N = uio[2k+1], NRZI (mode 1) or Manchester (mode 2)
 * line coding, optional bit stuffing, CRC-5 (receive) and CRC-16 / CRC-32
 * in one 32-bit register. Half duplex: the receiver runs only while the
 * transmitter is idle (15.4).
 *
 * Timing: the symbol tick is the owner thread's timer tick, taken from the
 * core's registered period / prescale (tm_tick[t] = period[t] != 0 and
 * prescale[t] = 0, 15.1). Everything the core reads from here (rd_st,
 * rd_rx, tx_full, tx_idle, rx_valid, rx_end) is a flop or one gate level
 * from flops, so the engine adds no depth behind the instruction decode:
 * the byte / status selection of SERRX (rd_rx) is made here from rx_valid,
 * and the core only chooses rd_rx or rd_st by the function field, in
 * parallel with its own decode.
 *
 * Inputs from the core are one-cycle strobes of the executing thread's
 * committed instruction: cfg_we (SERCFG), tx_we (SERTX, SERTXC, SERI,
 * SERIC; only when tx_full = 0), rx_ack (SERRX taking a byte or a frame
 * end), st_ack (SERST). A SERCFG overrides every engine update of its
 * cycle and suppresses the engine's pin write (15.2); if it aborts a frame
 * (tx_state != IDLE) it writes idle(m, P, N) of the configuration it
 * replaces instead (15.1, 15.2, DECISIONS D-039).
 *
 * rx_drop (status bit 10, 15.5, 15.7): set by a frame start that discards
 * an untaken byte or frame end, cleared by a read of the status word
 * (st_ack, or rx_ack with rx_valid = 0) and by SERCFG; the set wins.
 *
 * The pin write leaves on pin_*: pin_valid, the pair k, pin_drive (the
 * uio_oe value written), pin_wout (uio_out <= pin_p / pin_n is written
 * too). line(s) / se0(): drive 1, wout 1. NRZI tail release: drive 0,
 * wout 0 (uio_out unchanged). Abort, mode 1: drive 0, wout 1, J (P 0,
 * N 1); mode 2: se0(). keyer_pins applies it only to push-pull pins
 * (od_mask' of 15.1).
 *
 * Register names and widths are those of SEMANTICS 13 / 15.1: the lockstep
 * harness reads them by name. All are reset to 0 by rst_n only.
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module keyer_ser (
    input  wire        clk,
    input  wire        rst_n,
    // instructions committed by the executing thread, one-cycle strobes
    input  wire        cfg_we,          // SERCFG rs
    input  wire [7:0]  cfg_val,         // rs[7:0]
    input  wire        cfg_tid,         // the executing thread: the new owner
    input  wire        tx_we,           // SERTX / SERTXC / SERI / SERIC completes
    input  wire [7:0]  tx_val,          // the byte
    input  wire        tx_val_c,        // 1: the byte is in the CRC (SERTXC, SERIC)
    input  wire        rx_ack,          // SERRX completes (byte or frame end)
    input  wire        st_ack,          // SERST completes (reads the status word)
    // both threads' timers (registered state of the core)
    input  wire [31:0] period,          // {period[1], period[0]}
    input  wire [1:0]  tm_tick,         // bit t: period[t] != 0 and prescale[t] = 0
    // pins
    input  wire [7:0]  level,           // level(c)[7:0]: the uio pins
    output wire        pin_valid,
    output wire [1:0]  pin_k,
    output wire        pin_drive,       // uio_oe value
    output wire        pin_wout,        // 1: uio_out <= pin_p / pin_n as well
    output wire        pin_p,
    output wire        pin_n,
    // to the core, all from flops
    output wire [15:0] rd_st,           // status(c) (15.7)
    output wire [15:0] rd_rx,           // SERRX result: zext(rx_hold) if rx_valid, else status
    output reg         tx_full,
    output wire        tx_idle,         // tx_state = IDLE
    output reg         rx_valid,
    output reg         rx_end
);
    localparam [1:0] TX_IDLE = 2'd0, TX_DATA = 2'd1, TX_CRC = 2'd2, TX_TAIL = 2'd3;

    // ---- state (15.1) ------------------------------------------------------
    reg [7:0]  cfg;
    reg        owner;
    reg [7:0]  tx_hold;
    reg        tx_hold_c;
    reg [1:0]  tx_state;
    reg [7:0]  tx_sh;
    reg        tx_c, tx_app;
    reg [4:0]  tx_n;
    reg        tx_half, tx_bit;
    reg [2:0]  tx_ones;
    reg        tx_line;
    reg [31:0] crc_m;
    reg [4:0]  crc5;
    reg        rx_state;
    reg [7:0]  rx_sh;
    reg [2:0]  rx_n;
    reg [2:0]  rx_ones;
    reg        rx_psym, rx_last;
    reg [15:0] rx_cnt;
    reg        rx_w, rx_first;
    reg [7:0]  rx_hold;
    reg        rx_ovr, rx_serr, rx_ferr, rx_c5ok, rx_cok;
    reg        rx_drop;

    // ---- configuration fields ---------------------------------------------
    wire       mode_on = cfg[0] ^ cfg[1];   // mode 1 or 2; 0 and 3 are off
    wire       m1      = cfg[0];            // with mode_on: 1 = NRZI, 0 = Manchester
    wire       stuff   = cfg[2];
    wire       crc32   = cfg[3];
    wire       rxen    = cfg[4];
    wire       rxskip  = cfg[5];
    wire [1:0] k       = cfg[7:6];
    assign pin_k = k;

    wire [15:0] T    = owner ? period[31:16] : period[15:0];   // T(c)
    wire        tick = mode_on & (owner ? tm_tick[1] : tm_tick[0]);

    wire [31:0] poly_m = crc32 ? 32'hEDB88320 : 32'h0000A001;
    wire [31:0] init_m = crc32 ? 32'hFFFFFFFF : 32'h0000FFFF;
    wire [31:0] res_m  = crc32 ? 32'hDEBB20E3 : 32'h0000B001;
    wire [4:0]  w_last = crc32 ? 5'd31 : 5'd15;                // W - 1

    wire [31:0] crc_m_sh = {1'b0, crc_m[31:1]};
    wire [4:0]  crc5_sh  = {1'b0, crc5[4:1]};

    // the pair's levels during c
    wire sym  = level[{k, 1'b0}];
    wire nsym = level[{k, 1'b1}];

    // ---- status (15.7) -----------------------------------------------------
    assign tx_idle = (tx_state == TX_IDLE);
    assign rd_st   = {5'd0, rx_drop, rx_ferr, rx_serr, rx_ovr, rx_cok, rx_c5ok, rx_end,
                      rx_state, rx_valid, ~tx_idle, tx_full};
    assign rd_rx   = rx_valid ? {8'd0, rx_hold} : rd_st;

    // ---- transmitter (15.3): next state for a symbol tick -------------------
    reg [7:0]  t_sh;
    reg        t_c, t_app, t_take;          // t_take: the holding register is taken
    reg [1:0]  t_state;
    reg [4:0]  t_n;
    reg        t_half, t_bit, t_line;
    reg [2:0]  t_ones;
    reg        t_crc_we;
    reg [31:0] t_crc;
    reg        t_pv;                        // a pin write (before the SERCFG override)
    reg        t_drive, t_pp, t_pn;         // its oe value (1: line / se0, 0: release) and levels
    reg        e_do, e_b, c_do, e_s;        // emit(e_b), count(e_b), NRZI symbol

    always @(*) begin
        t_sh = tx_sh; t_c = tx_c; t_app = tx_app; t_take = 1'b0;
        t_state = tx_state; t_n = tx_n;
        t_half = tx_half; t_bit = tx_bit; t_line = tx_line; t_ones = tx_ones;
        t_crc_we = 1'b0; t_crc = crc_m;
        t_pv = 1'b0; t_drive = 1'b1; t_pp = 1'b0; t_pn = 1'b0;
        e_do = 1'b0; e_b = 1'b0; c_do = 1'b0; e_s = 1'b0;
        if (tick) begin
            if (~m1 & tx_half) begin
                // 1. second half of a Manchester bit: line(tx_bit)
                t_pv = 1'b1; t_pp = tx_bit; t_pn = ~tx_bit;
                t_half = 1'b0;
            end else if (tx_state == TX_IDLE) begin
                // 2. start; no pin is written
                if (tx_full) begin
                    t_sh = tx_hold; t_c = tx_hold_c; t_app = tx_hold_c; t_take = 1'b1;
                    t_state = TX_DATA; t_n = 5'd0; t_ones = 3'd0; t_line = 1'b0;
                    t_crc_we = 1'b1; t_crc = init_m;
                end
            end else if (stuff && tx_ones == 3'd6) begin
                // 3. stuffed zero; no data consumed, no counter moves
                e_do = 1'b1; e_b = 1'b0;
                t_ones = 3'd0;
            end else if (tx_state == TX_DATA) begin
                // 4. data bit
                e_do = 1'b1; e_b = tx_sh[0]; c_do = 1'b1;
                if (tx_c) begin
                    t_crc_we = 1'b1;
                    t_crc = crc_m_sh ^ ((crc_m[0] ^ tx_sh[0]) ? poly_m : 32'd0);
                end
                if (tx_n == 5'd7) begin
                    t_n = 5'd0;
                    if (tx_full) begin
                        t_sh = tx_hold; t_c = tx_hold_c; t_app = tx_app | tx_hold_c; t_take = 1'b1;
                    end else begin
                        t_state = tx_app ? TX_CRC : TX_TAIL;
                    end
                end else begin
                    t_sh = {1'b0, tx_sh[7:1]};
                    t_n = tx_n + 5'd1;
                end
            end else if (tx_state == TX_CRC) begin
                // 5. CRC bit, complemented, bit 0 first
                e_do = 1'b1; e_b = ~crc_m[0]; c_do = 1'b1;
                t_crc_we = 1'b1; t_crc = crc_m_sh;
                if (tx_n == w_last) begin
                    t_n = 5'd0; t_state = TX_TAIL;
                end else begin
                    t_n = tx_n + 5'd1;
                end
            end else begin
                // 6. tail. tx_n stays below 4 (mode 1) or 7 (mode 2) here.
                t_n = tx_n + 5'd1;
                if (m1) begin
                    case (tx_n[1:0])
                        2'd0, 2'd1: t_pv = 1'b1;                       // se0()
                        2'd2: begin t_pv = 1'b1; t_pn = 1'b1; end    // line(0): J
                        default: begin                                 // release
                            t_pv = 1'b1; t_drive = 1'b0;
                            t_state = TX_IDLE; t_n = 5'd0;
                        end
                    endcase
                end else begin
                    if (tx_n[2:0] == 3'd0) begin                       // line(1): K
                        t_pv = 1'b1; t_pp = 1'b1;
                    end else if (tx_n[2:0] == 3'd6) begin              // se0()
                        t_pv = 1'b1;
                        t_state = TX_IDLE; t_n = 5'd0;
                    end
                end
            end
            // emit(b) and count(b)
            if (e_do) begin
                t_pv = 1'b1;
                if (m1) begin
                    e_s = e_b ? tx_line : ~tx_line;
                    t_line = e_s;
                    t_pp = e_s; t_pn = ~e_s;
                end else begin
                    t_pp = ~e_b; t_pn = e_b;
                    t_bit = e_b; t_half = 1'b1;
                end
            end
            if (c_do & stuff) t_ones = e_b ? tx_ones + 3'd1 : 3'd0;
        end
    end

    // A SERCFG of the same cycle cancels the engine's pin write (15.2). If
    // it aborts a frame (tx_state != IDLE, so the old mode is 1 or 2 and m1
    // tells them apart) it writes idle() of the old pair instead (15.1):
    // mode 1 write(P, 0, 0), write(N, 1, 0); mode 2 se0(). One 2:1 mux per
    // port bit on cfg_we, after the engine's own selection.
    assign pin_valid = cfg_we ? ~tx_idle : t_pv;
    assign pin_drive = cfg_we ? ~m1      : t_drive;
    assign pin_wout  = cfg_we | t_drive;
    assign pin_p     = ~cfg_we & t_pp;
    assign pin_n     = cfg_we ? m1       : t_pn;

    // ---- receiver (15.4 - 15.6): next state ----------------------------------
    wire        rx_run = mode_on & rxen & tx_idle;
    wire        edge_  = sym ^ rx_last;
    wire        se0    = ~sym & ~nsym;
    wire        cnt_z  = (rx_cnt == 16'd0);
    wire [15:0] t_half_p = {1'b0, T[15:1]};                 // T >> 1
    wire [15:0] t_m1     = T - 16'd1;                       // T - 1
    wire [15:0] t_2m1    = {t_m1[14:0], 1'b1};              // 2T - 1 = 2(T - 1) + 1
    wire [15:0] t_15m2   = T + t_half_p - 16'd2;            // T + (T >> 1) - 2
    wire [7:0]  sync     = m1 ? 8'h80 : 8'hD5;

    reg        r_state, r_psym, r_w, r_first, r_valid, r_end;
    reg [7:0]  r_sh, r_hold;
    reg [2:0]  r_n, r_ones;
    reg [15:0] r_cnt;
    reg        r_ovr, r_serr, r_ferr, r_c5ok, r_cok;
    reg [4:0]  r_crc5;
    reg        r_crc_we;
    reg [31:0] r_crc;
    reg        do_bit, bit_d, do_end;
    reg        r_fs;                       // a frame starts (15.5)
    reg [7:0]  v;

    always @(*) begin
        r_state = rx_state; r_psym = rx_psym; r_w = rx_w; r_first = rx_first;
        r_valid = rx_valid; r_end = rx_end;
        r_sh = rx_sh; r_hold = rx_hold; r_n = rx_n; r_ones = rx_ones; r_cnt = rx_cnt;
        r_ovr = rx_ovr; r_serr = rx_serr; r_ferr = rx_ferr; r_c5ok = rx_c5ok; r_cok = rx_cok;
        r_crc5 = crc5; r_crc_we = 1'b0; r_crc = crc_m;
        do_bit = 1'b0; bit_d = 1'b0; do_end = 1'b0; r_fs = 1'b0;
        v = {1'b0, rx_sh[7:1]};
        if (!rx_run) begin
            // 15.4: not running
            r_state = 1'b0; r_sh = 8'hFF; r_n = 3'd0; r_ones = 3'd0; r_psym = 1'b0;
            r_cnt = 16'd0; r_w = 1'b1; r_first = 1'b0;
        end else if (m1) begin
            // 15.6 mode 1 (NRZI)
            if (edge_) r_cnt = t_half_p;
            else if (!cnt_z) r_cnt = rx_cnt - 16'd1;
            else begin
                r_cnt = t_m1;
                if (se0) begin
                    do_end = 1'b1; r_psym = 1'b0;
                end else begin
                    do_bit = 1'b1; bit_d = (sym == rx_psym); r_psym = sym;
                end
            end
        end else begin
            // 15.6 mode 2 (Manchester)
            if (edge_ & rx_w) begin
                r_w = 1'b0; r_cnt = t_15m2; do_bit = 1'b1; bit_d = sym;
            end else if (!cnt_z) r_cnt = rx_cnt - 16'd1;
            else if (!rx_w) begin
                r_w = 1'b1; r_cnt = t_2m1;
            end else do_end = 1'b1;
        end

        // bit(d), 15.5
        v = {bit_d, rx_sh[7:1]};
        if (do_bit) begin
            if (rx_state && stuff && rx_ones == 3'd6) begin
                r_ones = 3'd0;                       // stuffed bit, discarded
                if (bit_d) r_serr = 1'b1;
            end else begin
                if (rx_state && stuff) r_ones = bit_d ? rx_ones + 3'd1 : 3'd0;
                r_sh = v;
                if (!rx_state) begin
                    if (v == sync) begin             // frame start
                        r_fs = 1'b1;
                        r_state = 1'b1; r_n = 3'd0; r_first = 1'b1;
                        r_ones = !stuff ? 3'd0 : m1 ? 3'd1 : 3'd2;
                        r_crc5 = 5'h1F; r_crc_we = 1'b1; r_crc = init_m;
                        r_valid = 1'b0; r_end = 1'b0;
                        r_ovr = 1'b0; r_serr = 1'b0; r_ferr = 1'b0;
                    end
                end else begin
                    if (!(rxskip && rx_first)) begin
                        r_crc5 = crc5_sh ^ ((crc5[0] ^ bit_d) ? 5'h14 : 5'd0);
                        r_crc_we = 1'b1;
                        r_crc = crc_m_sh ^ ((crc_m[0] ^ bit_d) ? poly_m : 32'd0);
                    end
                    if (rx_n == 3'd7) begin          // byte complete
                        r_n = 3'd0; r_first = 1'b0;
                        if (rx_valid) r_ovr = 1'b1;
                        else begin r_hold = v; r_valid = 1'b1; end
                    end else begin
                        r_n = rx_n + 3'd1;
                    end
                end
            end
        end

        // end(), 15.5
        if (do_end) begin
            if (rx_state) begin
                r_end = 1'b1;
                r_ferr = (rx_n != 3'd0);
                r_c5ok = (crc5 == 5'h06);
                r_cok = (crc_m == res_m);
            end
            r_state = 1'b0; r_sh = 8'hFF; r_n = 3'd0; r_ones = 3'd0; r_first = 1'b0;
        end
    end

    // rx_drop (15.5): a frame start discards an untaken byte (rx_valid, and
    // no SERRX takes it now) or an untaken frame end (rx_end, and no SERRX
    // takes it now: a SERRX takes the frame end only when rx_valid = 0).
    // A read of the status word clears it; the set wins (15.2).
    wire drop_set = r_fs & ((rx_valid & rx_end) | ((rx_valid | rx_end) & ~rx_ack));
    wire st_read  = st_ack | (rx_ack & ~rx_valid);

    // ---- registers -------------------------------------------------------------
    always @(posedge clk) begin
        if (!rst_n) begin
            cfg <= 8'd0; owner <= 1'b0;
            tx_hold <= 8'd0; tx_hold_c <= 1'b0; tx_full <= 1'b0; tx_state <= TX_IDLE;
            tx_sh <= 8'd0; tx_c <= 1'b0; tx_app <= 1'b0; tx_n <= 5'd0;
            tx_half <= 1'b0; tx_bit <= 1'b0; tx_ones <= 3'd0; tx_line <= 1'b0;
            crc_m <= 32'd0; crc5 <= 5'd0;
            rx_state <= 1'b0; rx_sh <= 8'd0; rx_n <= 3'd0; rx_ones <= 3'd0;
            rx_psym <= 1'b0; rx_last <= 1'b0; rx_cnt <= 16'd0; rx_w <= 1'b0; rx_first <= 1'b0;
            rx_hold <= 8'd0; rx_valid <= 1'b0; rx_end <= 1'b0;
            rx_ovr <= 1'b0; rx_serr <= 1'b0; rx_ferr <= 1'b0; rx_c5ok <= 1'b0; rx_cok <= 1'b0;
            rx_drop <= 1'b0;
        end else begin
            rx_last <= sym;                          // every cycle, P from cfg(c) (15.1)
            if (cfg_we) begin
                // SERCFG: overrides every engine update of the cycle (15.2)
                cfg <= cfg_val; owner <= cfg_tid;
                tx_hold <= 8'd0; tx_hold_c <= 1'b0; tx_full <= 1'b0; tx_state <= TX_IDLE;
                tx_sh <= 8'd0; tx_c <= 1'b0; tx_app <= 1'b0; tx_n <= 5'd0;
                tx_half <= 1'b0; tx_bit <= 1'b0; tx_ones <= 3'd0; tx_line <= 1'b0;
                crc_m <= 32'd0; crc5 <= 5'd0;
                rx_state <= 1'b0; rx_sh <= 8'hFF; rx_n <= 3'd0; rx_ones <= 3'd0;
                rx_psym <= 1'b0; rx_cnt <= 16'd0; rx_w <= 1'b1; rx_first <= 1'b0;
                rx_hold <= 8'd0; rx_valid <= 1'b0; rx_end <= 1'b0;
                rx_ovr <= 1'b0; rx_serr <= 1'b0; rx_ferr <= 1'b0; rx_c5ok <= 1'b0; rx_cok <= 1'b0;
                rx_drop <= 1'b0;
            end else begin
                // holding register: SERTX completes only when tx_full = 0, the
                // transmitter takes it only when tx_full = 1 (15.2)
                if (tx_we) begin tx_hold <= tx_val; tx_hold_c <= tx_val_c; end
                tx_full <= tx_we | (tx_full & ~t_take);
                tx_state <= t_state; tx_sh <= t_sh; tx_c <= t_c; tx_app <= t_app; tx_n <= t_n;
                tx_half <= t_half; tx_bit <= t_bit; tx_ones <= t_ones; tx_line <= t_line;
                // the transmitter's start wins over a receiver step (15.5)
                if (t_crc_we) crc_m <= t_crc;
                else if (r_crc_we) crc_m <= r_crc;
                crc5 <= r_crc5;
                rx_state <= r_state; rx_sh <= r_sh; rx_n <= r_n; rx_ones <= r_ones;
                rx_psym <= r_psym; rx_cnt <= r_cnt; rx_w <= r_w; rx_first <= r_first;
                rx_hold <= r_hold;
                // SERRX clears the flag it took; never the one the engine sets
                // in the same cycle (15.2)
                rx_valid <= r_valid & ~(rx_ack & rx_valid);
                rx_end   <= r_end & ~(rx_ack & ~rx_valid);
                rx_ovr <= r_ovr; rx_serr <= r_serr; rx_ferr <= r_ferr;
                rx_c5ok <= r_c5ok; rx_cok <= r_cok;
                rx_drop <= drop_set | (rx_drop & ~st_read);
            end
        end
    end
endmodule
