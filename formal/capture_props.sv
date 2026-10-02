// Formal properties for keyer_capture (SEMANTICS section 14). This file is
// included into the body of src/keyer_capture.v under `ifdef FORMAL (the
// open-source Yosys front end has neither `bind` nor hierarchical
// references), so it sees the engine's registers by their section 13 names.
// Every input is free: host writes, CAPC, running bits, the slot parity, the
// host's port use, the pin levels and the memory read data. The only
// assumption is a reset in the first cycle. Run by formal/capture.sby.
//
// The trigger, recording and port rules are restated here from the spec
// (f_* signals) rather than taken from the RTL's own wires.
//
// C1  An entry produced while the queue is full (two entries and no write in
//     that cycle, 14.4) sets cap_ovf (and clears cap_armed) at the next
//     cycle, unless ARM lands in that cycle; cap_ovf never rises otherwise;
//     an entry produced with room is queued.
// C2  Every capture write goes to (cap_base + cap_w) mod 256 with
//     cap_w < cap_len, so inside [cap_base, cap_base + cap_len) mod 256, and
//     cap_w <= cap_n <= cap_len: nothing beyond cap_len is ever written.
//     Holds while CAP_BUF is unchanged since the last ARM (14: changing the
//     configuration of an active engine is undefined).
// C3  Replay exactness: when entry k >= 1 is applied, exactly
//     max(delta, 1) cycles have passed since the previous apply (own
//     13-bit counter f_el, which does not saturate at 4095) and
//     rep_under = 0, for every delta including 4095; while the replay is
//     active after entry 0, rep_dt equals the true elapsed count, which never
//     exceeds 4095 (14.7, fourth bullet: with no head at rep_dt = 4095 it
//     underruns; resolves docs/spec-questions.md Q16).
// C4  Once rep_under is set nothing is applied and the replay is inactive
//     until the next START, and rep_under stays set until then. rep_under
//     rises exactly when 14.7 says (f_under: the head is late, or no head
//     is present at rep_dt = 4095), unless START lands in that cycle.
// C5  Entry contents: the trigger entry has delta 0; a recorded entry has
//     delta = cycles since the previous entry (own counter f_ce) and
//     pins = the masked group nibble.
// C6  Data integrity: the j-th entry produced after ARM (any j) is the word
//     written at cap_base + j; the word fetched for replay index j is the
//     entry applied as entry j.
// C7  Port discipline: the engines only use a free cycle (14.1), never both
//     in one cycle, and the replay reads only when the queue is empty,
//     only entries below rep_n, at (rep_base + rep_f) mod 256.
// C8  Status: cap_done = !cap_armed && queue empty && cap_was_armed;
//     capture active = cap_armed || queue non-empty; the prefetch holds
//     nothing while the replay is inactive (14.6).
// C9  With the port free in every cycle since START (no host access, the
//     next thread stopped, no capture write), the replay never underruns,
//     whatever the deltas (14.6: entries one cycle apart replay
//     indefinitely).
// C10 Control actions (14.2, 14.5): ARM resets the capture (cap_armed =
//     cap_len != 0, done at once for length 0); DISARM clears cap_armed;
//     START latches rep_n = rep_len, or cap_w if rep_len = 0, and resets the
//     replay (active iff rep_n != 0, done iff rep_n = 0); STOP stops it.

    reg f_past_valid = 1'b0;
    always @(posedge clk) f_past_valid <= 1'b1;
    always @(*) if (!f_past_valid) assume(!rst_n);

    // ---- reference decode (SEMANTICS 14.1-14.5) --------------------------------
    wire        f_arm   = (host_ctrl_we && host_ctrl_val[0]) || (core_ctrl_we && core_ctrl_val[0]);
    wire        f_disarm = (host_ctrl_we && host_ctrl_val[1]) || (core_ctrl_we && core_ctrl_val[1]);
    wire        f_start = (host_ctrl_we && host_ctrl_val[2]) || (core_ctrl_we && core_ctrl_val[2]);
    wire        f_stop  = (host_ctrl_we && host_ctrl_val[3]) || (core_ctrl_we && core_ctrl_val[3]);
    wire [31:0] f_lv    = {8'd0, level};
    wire [3:0]  f_nib   = f_lv[{cap_group, 2'b00} +: 4];        // groups 6, 7: pins 24-31 = 0
    wire [3:0]  f_s     = f_nib & cap_mask;
    wire        f_match = (f_nib & cap_tmask) == (cap_tpat & cap_tmask);
    wire        f_pmatch = (cap_prev & cap_tmask) == (cap_tpat & cap_tmask);
    wire        f_trig  = cap_armed && !cap_trig && (cap_tmask == 4'd0 || (f_match && !f_pmatch));
    wire        f_rec   = cap_armed && cap_trig && (f_s != cap_last || cap_dt == 12'd4095);
    wire        f_prod  = f_trig || f_rec;
    wire        f_free  = !host_busy && !running[1 - tid];        // thread (c + 1) mod 2
    wire        f_qfull = q_count == 2'd2 && !f_free;             // no departing entry
    wire [7:0]  f_off   = mem_addr - cap_base;
    wire [11:0] f_delta = pf0[15:4];                              // the head entry
    wire [11:0] f_target = (f_delta == 12'd0) ? 12'd1 : f_delta;
    wire        f_under = rep_active && rep_k != 8'd0 &&
                          (pf_count != 2'd0 ? rep_dt > f_target : rep_dt == 12'd4095);

    // CAP_BUF written since the last ARM
    reg f_dirty;
    always @(posedge clk)
        if (!rst_n) f_dirty <= 1'b0;
        else f_dirty <= cap_buf_we || (f_dirty && !f_arm);

    // cycles since the last replay apply / since the last capture entry
    reg [12:0] f_el, f_ce;
    always @(posedge clk) begin
        if (!rst_n) begin f_el <= 13'd0; f_ce <= 13'd0; end
        else begin
            if (rep_valid) f_el <= 13'd1;
            else if (f_el != 13'h1FFF) f_el <= f_el + 13'd1;
            if (f_prod) f_ce <= 13'd1;
            else if (f_ce != 13'h1FFF) f_ce <= f_ce + 13'd1;
        end
    end

    // C6 trackers: an arbitrary entry index j for each engine
    (* anyconst *) reg [7:0] f_cj;
    (* anyconst *) reg [7:0] f_rj;
    wire [7:0] f_cpos = f_cj - cap_w;     // position of entry j in the queue (mod 256)
    wire [7:0] f_rpos = f_rj - rep_k;     // position of word j in the prefetch (mod 256)
    reg        f_chave;          // capture entry j produced and queued, not yet written
    reg [15:0] f_cword;
    reg        f_rhave;          // replay word j has landed in the prefetch, not yet applied
    reg [15:0] f_rword;
    always @(posedge clk) begin
        if (!rst_n || f_arm) f_chave <= 1'b0;
        else if (f_prod && !f_qfull && cap_n == f_cj) begin
            f_chave <= 1'b1; f_cword <= {cap_trig ? cap_dt : 12'd0, f_s};
        end else if (mem_we && cap_w == f_cj) f_chave <= 1'b0;
        if (!rst_n || f_start) f_rhave <= 1'b0;
        else if (rep_active && pf_fly && rep_f - 8'd1 == f_rj) begin f_rhave <= 1'b1; f_rword <= mem_rdata; end
        else if (rep_valid && rep_k == f_rj) f_rhave <= 1'b0;
    end

    // C9 tracker: the port has been free in every active cycle since START
    reg f_allfree;
    always @(posedge clk)
        if (!rst_n) f_allfree <= 1'b0;
        else if (f_start) f_allfree <= 1'b1;
        else if (rep_active && !(f_free && q_count == 2'd0)) f_allfree <= 1'b0;

    always @(posedge clk) if (f_past_valid) begin
        // ---- structure (helper invariants for the induction) ----------------
        assert(q_count != 2'd3);
        assert({1'b0, pf_count} + {2'b00, pf_fly} <= 3'd2);
        assert(cap_n == cap_w + {6'd0, q_count} + {7'd0, cap_ovf});
        if (rep_active) assert(rep_f == rep_k + {6'd0, pf_count} + {7'd0, pf_fly});
        else assert(pf_count == 2'd0);
        if (cap_armed) assert(!cap_ovf);
        if (cap_armed && !cap_trig) assert(cap_n == 8'd0 && q_count == 2'd0 && cap_w == 8'd0);
        if (cap_was_armed == 1'b0) assert(!cap_armed && q_count == 2'd0);

        // ---- C1 / C2: the capture never writes past its buffer ----------------
        if (!f_dirty) begin
            assert(cap_w <= cap_n && cap_n <= cap_len);
            if (cap_armed) assert(cap_n < cap_len);
            assert(cap_w <= cap_len);
            if (mem_we) assert(cap_w < cap_len && f_off < cap_len && mem_addr == cap_base + cap_w);
        end

        // ---- C3 / C4: replay timing and underrun -----------------------------
        if (rep_active) assert(!rep_under && rep_n != 8'd0 && rep_k < rep_n && rep_k <= rep_f && rep_f <= rep_n);
        if (rep_active && rep_k != 8'd0)
            assert(f_el <= 13'd4095 && rep_dt == f_el[11:0]);
        if (rep_valid && rep_k != 8'd0)
            assert(f_el == {1'b0, f_target} && !rep_under);
        if (rep_under) assert(!rep_valid && !rep_active);

        // ---- C5: entry contents ------------------------------------------------
        if (cap_armed && cap_trig) assert({1'b0, cap_dt} == f_ce && cap_dt != 12'd0);
        if (f_prod) assert(entry == {cap_trig ? f_ce[11:0] : 12'd0, f_s});

        // ---- C6: data integrity --------------------------------------------------
        // entry j is queued exactly when it was produced with room since the
        // last ARM and not written yet; the queue slot holds what was produced
        assert(f_chave == (f_cpos < {6'd0, q_count}));
        if (f_chave) begin
            assert((f_cpos == 8'd0 ? q0 : q1) == f_cword);
            if (mem_we && f_cpos == 8'd0) assert(mem_wdata == f_cword);
        end
        // word j is held exactly when it landed since the last START and was
        // not applied yet; the prefetch slot holds what the memory returned
        if (rep_active) assert(f_rhave == (f_rpos < {6'd0, pf_count}));
        if (rep_active && f_rhave) begin
            assert((f_rpos == 8'd0 ? pf0 : pf1) == f_rword);
            if (rep_valid && f_rpos == 8'd0) assert(rep_data == f_rword[3:0] && f_delta == f_rword[15:4]);
        end

        // ---- C7: port discipline ---------------------------------------------
        if (mem_we || mem_re) assert(f_free);
        assert(!(mem_we && mem_re));
        if (mem_re) assert(q_count == 2'd0 && rep_active && rep_f < rep_n && mem_addr == rep_base + rep_f);
        if (mem_we) assert(q_count != 2'd0);

        // ---- C9: no underrun while the port is free every cycle ----------------
        if (f_allfree) assert(!rep_under);
        if (f_allfree && rep_active && rep_k != 8'd0)
            assert(pf_count != 2'd0 && rep_dt <= f_target &&
                   ({1'b0, pf_count} + {2'b00, pf_fly} == 3'd2 || rep_f == rep_n));

        // ---- C8: status --------------------------------------------------------
        assert(cap_done == (!cap_armed && q_count == 2'd0 && cap_was_armed));
        assert(cap_active == (cap_armed || q_count != 2'd0));
    end

    // properties over one transition
    always @(posedge clk) if (f_past_valid && $past(f_past_valid) && $past(rst_n)) begin
        // C1
        if ($past(f_prod && f_qfull && !f_arm)) assert(cap_ovf && !cap_armed);
        if (cap_ovf && !$past(cap_ovf)) assert($past(f_prod && f_qfull));
        if ($past(f_prod && !f_qfull && !f_arm))
            assert(q_count == $past(q_count + {1'b0, f_prod} - {1'b0, mem_we}));
        // C4
        if ($past(rep_under && !f_start)) assert(rep_under);
        if ($past(f_under && !f_start)) assert(rep_under && !rep_active && rep_done);
        if (rep_under && !$past(rep_under)) assert($past(f_under));
        // C10
        if ($past(f_arm))
            assert(cap_armed == $past(cap_len != 8'd0) && !cap_trig && !cap_ovf && cap_n == 8'd0 &&
                   cap_w == 8'd0 && q_count == 2'd0 && cap_was_armed && cap_done == $past(cap_len == 8'd0));
        if ($past(f_disarm && !f_arm)) assert(!cap_armed);
        if ($past(f_start))
            assert(rep_n == $past(rep_len != 8'd0 ? rep_len : cap_w) && rep_active == (rep_n != 8'd0) &&
                   rep_done == (rep_n == 8'd0) && !rep_under && rep_k == 8'd0 && rep_f == 8'd0 &&
                   rep_dt == 12'd0 && pf_count == 2'd0 && !pf_fly);
        if ($past(f_stop && !f_start)) assert(!rep_active && rep_done);
        if (!$past(f_start)) assert(rep_n == $past(rep_n));
    end

    // non-vacuity (capture.sby task cover)
    always @(posedge clk) if (f_past_valid && rst_n) begin
        cover(mem_we && cap_w == 8'd2);
        cover(cap_ovf);
        cover(rep_valid && rep_k == 8'd2);
        cover(rep_under);
        cover(f_allfree && rep_valid && rep_k == 8'd4 && f_el == 13'd1);   // entries one cycle apart
        cover(cap_done && cap_w == 8'd3);
    end
