/*
 * Keyer: capture and replay engines (docs/SEMANTICS.md section 14,
 * programmer's view in docs/CAPTURE.md).
 *
 * Capture watches one 4-pin group of the synchronised level vector, waits
 * for its trigger, and produces 16-bit entries {delta[11:0], pins[3:0]} into
 * a two-entry queue that drains into the program memory at
 * cap_base + cap_w. Replay fetches entries from rep_base + rep_f into a
 * two-entry prefetch and applies them to a 4-pin group through the pin
 * unit's second command port, entry k exactly max(delta_k, 1) cycles after
 * entry k - 1.
 *
 * Both engines use the program memory port only in a "free" cycle (14.1):
 * the host does not use the port, and the thread that executes in the next
 * cycle is not running. The capture write has priority over the replay
 * read. The top invalidates the next fetch after any engine access.
 *
 * Register names follow SEMANTICS section 13 (the lockstep harness reads
 * them by name). Everything is reset to 0 by rst_n only.
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module keyer_capture (
    input  wire        clk,
    input  wire        rst_n,
    // observation
    input  wire [23:0] level,            // synchronised pin levels, this cycle
    input  wire [1:0]  running,
    input  wire        tid,              // thread owning this cycle (cycle mod 2)
    input  wire        host_busy,        // the host uses the memory port this cycle
    // host register writes: one-cycle pulses, landing at the end of the cycle
    input  wire        host_ctrl_we,     // CR_CTRL
    input  wire [3:0]  host_ctrl_val,
    input  wire        cap_cfg_we,       // CAP_CFG {byte 1, byte 0}
    input  wire [15:0] cap_cfg_val,
    input  wire        cap_buf_we,       // CAP_BUF {length, base}
    input  wire [15:0] cap_buf_val,
    input  wire        rep_cfg_we,       // REP_CFG
    input  wire [7:0]  rep_cfg_val,
    input  wire        rep_buf_we,       // REP_BUF {length, base}
    input  wire [15:0] rep_buf_val,
    // CAPC rs committed by the executing thread
    input  wire        core_ctrl_we,
    input  wire [3:0]  core_ctrl_val,
    // program memory port
    output wire        mem_we,           // capture writes mem_wdata to mem_addr
    output wire        mem_re,           // replay reads mem_addr (data next cycle)
    output wire [7:0]  mem_addr,
    output wire [15:0] mem_wdata,
    input  wire [15:0] mem_rdata,
    // status (14.8)
    output wire        cap_active,       // RDS bit 7
    output reg         rep_active,       // RDS bit 8
    output wire [7:0]  status,           // CR_CTRL read
    output wire [15:0] count,            // CR_COUNT read {rep_k, cap_w}
    output wire [15:0] cap_cfg_rd,
    output wire [15:0] cap_buf_rd,
    output wire [7:0]  rep_cfg_rd,
    output wire [15:0] rep_buf_rd,
    // replay pin command (pinwrite of the masked pins of rep_group)
    output wire        rep_valid,
    output reg  [2:0]  rep_group,
    output reg  [3:0]  rep_mask,
    output wire [3:0]  rep_data
);
    // ---- configuration (section 14, read back as written) ------------------
    reg [2:0] cap_group;
    reg [3:0] cap_mask, cap_tpat, cap_tmask;
    reg [7:0] cap_base, cap_len, rep_base, rep_len;

    always @(posedge clk) begin
        if (!rst_n) begin
            cap_group <= 3'd0; cap_mask <= 4'd0; cap_tpat <= 4'd0; cap_tmask <= 4'd0;
            cap_base <= 8'd0; cap_len <= 8'd0;
            rep_group <= 3'd0; rep_mask <= 4'd0; rep_base <= 8'd0; rep_len <= 8'd0;
        end else begin
            if (cap_cfg_we) begin
                cap_group <= cap_cfg_val[2:0];  cap_mask  <= cap_cfg_val[7:4];
                cap_tpat  <= cap_cfg_val[11:8]; cap_tmask <= cap_cfg_val[15:12];
            end
            if (cap_buf_we) begin cap_base <= cap_buf_val[7:0]; cap_len <= cap_buf_val[15:8]; end
            if (rep_cfg_we) begin rep_group <= rep_cfg_val[2:0]; rep_mask <= rep_cfg_val[7:4]; end
            if (rep_buf_we) begin rep_base <= rep_buf_val[7:0]; rep_len <= rep_buf_val[15:8]; end
        end
    end

    // ---- control actions (14.2, 14.5): host and CAPC are ORed --------------
    wire arm    = (host_ctrl_we & host_ctrl_val[0]) | (core_ctrl_we & core_ctrl_val[0]);
    wire disarm = (host_ctrl_we & host_ctrl_val[1]) | (core_ctrl_we & core_ctrl_val[1]);
    wire start  = (host_ctrl_we & host_ctrl_val[2]) | (core_ctrl_we & core_ctrl_val[2]);
    wire stop   = (host_ctrl_we & host_ctrl_val[3]) | (core_ctrl_we & core_ctrl_val[3]);

    // ---- memory port (14.1) -------------------------------------------------
    // free: no host access and the thread of the next cycle is not running
    wire free = ~host_busy & ~(tid ? running[0] : running[1]);

    // ---- capture state ------------------------------------------------------
    reg        cap_armed, cap_trig, cap_done, cap_ovf, cap_was_armed;
    reg [3:0]  cap_last, cap_prev;
    reg [11:0] cap_dt;
    reg [7:0]  cap_n, cap_w;
    reg [1:0]  q_count;                  // 0..2 entries queued
    reg [15:0] q0, q1;                   // q0 is the oldest

    // group nibble; groups 6 and 7 are the reserved pins 24-31 and read 0
    reg [3:0] nib;
    always @(*) begin
        case (cap_group)
            3'd0: nib = level[3:0];
            3'd1: nib = level[7:4];
            3'd2: nib = level[11:8];
            3'd3: nib = level[15:12];
            3'd4: nib = level[19:16];
            3'd5: nib = level[23:20];
            default: nib = 4'd0;
        endcase
    end
    wire [3:0] s = nib & cap_mask;

    // trigger (14.3): tmask = 0, or the nibble starts to match this cycle
    wire match_now  = ((nib      ^ cap_tpat) & cap_tmask) == 4'd0;
    wire match_prev = ((cap_prev ^ cap_tpat) & cap_tmask) == 4'd0;
    wire trig_now   = cap_armed & ~cap_trig & match_now & (~match_prev | (cap_tmask == 4'd0));
    // recording (14.4): a change, or an idle entry after 4095 quiet cycles
    wire rec_now    = cap_armed & cap_trig & ((s != cap_last) | (cap_dt == 12'd4095));
    wire produce    = trig_now | rec_now;
    wire [15:0] entry = {cap_dt & {12{cap_trig}}, s};   // delta 0 for the trigger entry

    wire q_nempty = q_count[1] | q_count[0];
    wire cap_wr   = free & q_nempty;            // write the oldest entry
    wire q_full   = q_count[1] & ~cap_wr;       // a departing entry frees its slot at once
    wire push     = produce & ~q_full;
    wire lost     = produce & q_full;           // overflow: the entry is lost
    wire [7:0] cap_n_inc = cap_n + 8'd1;
    wire last     = produce & (cap_n_inc == cap_len);

    // next values that cap_done depends on ("after which", 14.4)
    reg       armed_n;
    reg [1:0] q_count_n;
    always @(*) begin
        if (arm) armed_n = (cap_len != 8'd0);    // length 0: done at once
        else if (disarm | last | lost) armed_n = 1'b0;
        else armed_n = cap_armed;
        if (arm) q_count_n = 2'd0;
        else q_count_n = q_count + {1'b0, push} - {1'b0, cap_wr};
    end
    wire was_n = cap_was_armed | arm;

    assign cap_active = cap_armed | q_nempty;

    always @(posedge clk) begin
        if (!rst_n) begin
            cap_armed <= 1'b0; cap_trig <= 1'b0; cap_done <= 1'b0; cap_ovf <= 1'b0;
            cap_was_armed <= 1'b0; cap_last <= 4'd0; cap_prev <= 4'd0; cap_dt <= 12'd0;
            cap_n <= 8'd0; cap_w <= 8'd0; q_count <= 2'd0; q0 <= 16'd0; q1 <= 16'd0;
        end else begin
            cap_prev <= nib;
            cap_armed <= armed_n;
            q_count <= q_count_n;
            cap_was_armed <= was_n;
            cap_done <= ~armed_n & (q_count_n == 2'd0) & was_n;

            if (trig_now) cap_trig <= 1'b1;
            if (lost) cap_ovf <= 1'b1;
            if (produce) begin
                cap_n <= cap_n_inc;
                cap_last <= s;
                cap_dt <= 12'd1;
            end else if (cap_armed & cap_trig) begin
                cap_dt <= cap_dt + 12'd1;
            end
            if (cap_wr) begin
                cap_w <= cap_w + 8'd1;
                q0 <= q1;
            end
            if (push) begin                       // lands at position q_count - cap_wr
                if (~q_count[1] & (~q_count[0] | cap_wr)) q0 <= entry;
                else q1 <= entry;
            end
            if (arm) begin
                cap_trig <= 1'b0; cap_ovf <= 1'b0; cap_n <= 8'd0; cap_w <= 8'd0;
            end
        end
    end

    // ---- replay state ---------------------------------------------------------
    reg        rep_done, rep_under, pf_fly;
    reg [7:0]  rep_k, rep_f, rep_n;
    reg [11:0] rep_dt;
    reg [1:0]  pf_count;                 // entries held in the prefetch (not in flight)
    reg [15:0] pf0, pf1;                 // pf0 is the head

    wire        head    = pf_count[1] | pf_count[0];
    wire [11:0] hdelta  = pf0[15:4];
    wire [11:0] target  = (hdelta == 12'd0) ? 12'd1 : hdelta;
    wire        k0      = (rep_k == 8'd0);
    wire        apply   = rep_active & head & (k0 | (rep_dt == target));
    // underrun (14.7): the head is late, or none is present when rep_dt
    // saturates (no entry can be applied on time any more)
    wire        under   = rep_active & ~k0 & (head ? (rep_dt > target) : (rep_dt == 12'd4095));
    wire [7:0]  rep_k_inc = rep_k + 8'd1;
    wire        fin     = apply & (rep_k_inc == rep_n);      // the last entry
    // 14.6: fewer than two entries held or in flight, not counting the one
    // applied this cycle (pf_count + pf_fly <= 2, so the sum fits two bits)
    wire [1:0]  pf_occ  = pf_count + {1'b0, pf_fly} - {1'b0, apply};
    wire        rep_rd  = free & ~q_nempty & rep_active & (rep_f < rep_n) & ~pf_occ[1];
    // the replay stays active into the next cycle; otherwise the prefetch is
    // emptied and a word in flight is dropped on arrival (14.6)
    wire        rep_keep = rep_active & ~(stop | under | fin);
    wire [7:0]  n_rep   = (rep_len != 8'd0) ? rep_len : cap_w;   // 14.5

    always @(posedge clk) begin
        if (!rst_n) begin
            rep_active <= 1'b0; rep_done <= 1'b0; rep_under <= 1'b0; pf_fly <= 1'b0;
            rep_k <= 8'd0; rep_f <= 8'd0; rep_n <= 8'd0; rep_dt <= 12'd0;
            pf_count <= 2'd0; pf0 <= 16'd0; pf1 <= 16'd0;
        end else begin
            if (stop | under | fin) begin rep_active <= 1'b0; rep_done <= 1'b1; end
            if (under) rep_under <= 1'b1;
            if (apply) begin
                rep_k <= rep_k_inc;
                rep_dt <= 12'd1;
            end else if (rep_active && rep_dt != 12'd4095) begin
                rep_dt <= rep_dt + 12'd1;
            end
            if (rep_rd) rep_f <= rep_f + 8'd1;
            pf_fly <= rep_rd;
            // prefetch: the head leaves on apply; the word in flight lands behind
            pf_count <= rep_keep ? pf_occ : 2'd0;
            if (apply) pf0 <= pf1;
            if (pf_fly) begin
                if (~pf_count[0] | apply) pf0 <= mem_rdata;
                else pf1 <= mem_rdata;
            end
            if (start) begin
                rep_n <= n_rep;
                rep_active <= (n_rep != 8'd0);
                rep_done <= (n_rep == 8'd0);
                rep_under <= 1'b0; rep_k <= 8'd0; rep_f <= 8'd0; rep_dt <= 12'd0;
                pf_count <= 2'd0; pf_fly <= 1'b0;     // also drops a word in flight
            end
        end
    end

    // ---- outputs ----------------------------------------------------------------
    assign mem_we    = cap_wr;
    assign mem_re    = rep_rd;
    assign mem_addr  = (cap_wr ? cap_base : rep_base) + (cap_wr ? cap_w : rep_f);
    assign mem_wdata = q0;

    assign rep_valid = apply;
    assign rep_data  = pf0[3:0];

    assign status     = {1'b0, rep_under, rep_done, rep_active, cap_ovf, cap_done, cap_trig, cap_active};
    assign count      = {rep_k, cap_w};
    assign cap_cfg_rd = {cap_tmask, cap_tpat, cap_mask, 1'b0, cap_group};
    assign cap_buf_rd = {cap_len, cap_base};
    assign rep_cfg_rd = {rep_mask, 1'b0, rep_group};
    assign rep_buf_rd = {rep_len, rep_base};

    wire _unused = &{cap_cfg_val[3], rep_cfg_val[3], 1'b0};   // bit 3 of the group bytes is not stored

`ifdef FORMAL
    // Properties C1-C4 and their helper invariants: formal/capture_props.sv,
    // included here because the open-source Yosys front end has neither
    // `bind` nor hierarchical references (formal/capture.sby).
`include "capture_props.sv"
`endif
endmodule
