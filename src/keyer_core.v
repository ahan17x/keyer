/*
 * Keyer core: barrel-interleaved hardware threads (two by default), 16-bit
 * datapath.
 *
 * Thread 0 executes on even cycles, thread 1 on odd cycles. The instruction
 * for the executing thread was fetched on the previous cycle from
 * imem_rdata (one-cycle synchronous memory). Every instruction completes in
 * its slot unless it is blocking, in which case the PC does not advance and
 * it is re-evaluated at the thread's next slot.
 *
 * The contract is docs/SEMANTICS.md (section numbers below refer to it);
 * cocotb runs this RTL in lockstep with the golden model.
 *
 * NTHREADS (DECISIONS D-019) sizes the thread state for area studies; the
 * ISA, the host interface and the top are two-thread, and the top uses the
 * default. NTHREADS must be a power of two, at least 2: thread t owns the
 * cycles with cyc mod NTHREADS = t (the slot counter is the low bits of
 * cyc). "The other thread" of START, STOP and RDS bit 5 is thread
 * (tid + 1) mod NTHREADS, which for two threads is the other one. RDS
 * returns the thread id in bit 6 (its upper bits, for more than two threads,
 * from bit 9 up, above the capture and replay bits 7 and 8).
 * pc0_out and pc1_out export threads 0 and 1, the PCs the host can read.
 *
 * Thread select (DECISIONS D-028). `tid` (the low bits of cyc) stays the
 * slot owner that the harness and keyer_capture observe, but no per-thread
 * consumer decodes it. Each consumer group has its own registered one-hot
 * copy of it, NTHREADS wide, reset to bit 0 and rotated every cycle in step
 * with cyc, so bit t is set iff tid == t (formal S1 proves it for every
 * copy in every cycle):
 *   sel_rf  register file: thread selection of the A and B reads, register
 *           write enables;
 *   sel_tm  timers: NOW and DEADLINE selection, timer write enables;
 *   sel_pc  PC (and the fetch address), LR, flags, DELAY count, blocked,
 *           running / halted updates, RDS bit 5;
 *   sel_io  FIFO head and flag selection, FIFO pop and push strobes, the
 *           pin command strobe, the CAPC strobe and the serializer strobes.
 * Per-thread reads are AND-OR selections over a copy; per-thread write
 * enables are <thread t executes, from flops> & <field enable>, with the
 * register-file address pre-decoded from the instruction word, so the late
 * execute results (done, wr_en, the ALU) only meet one level of per-thread
 * gating. Each copy rotates its own bits, so no two copies share a D input
 * and structural merging leaves them apart; (* keep *) keeps them as named
 * flops.
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none
`include "keyer_isa.vh"

module keyer_core #(
    parameter NTHREADS = 2
) (
    input  wire        clk,
    input  wire        rst_n,
    // program memory
    output wire [7:0]  fetch_addr,       // PC of the thread that executes next cycle
    input  wire [15:0] imem_rdata,
    input  wire        fetch_ok,         // imem_rdata holds a fetch (port not used by the host)
    // pins
    input  wire [23:0] level,
    input  wire [23:0] level2,
    output reg         pin_valid,
    output reg  [2:0]  pin_op,
    output reg  [4:0]  pin_pin,
    output reg  [7:0]  pin_data,
    // FIFOs, index = thread
    input  wire [8*NTHREADS-1:0] inbox_rdata,   // head of thread t's inbox at [8t+7:8t]
    input  wire [NTHREADS-1:0]   inbox_empty,
    input  wire [NTHREADS-1:0]   inbox_full,
    output reg  [NTHREADS-1:0]   inbox_pop,
    output reg  [7:0]            outbox_wdata,
    output reg  [NTHREADS-1:0]   outbox_push,
    input  wire [NTHREADS-1:0]   outbox_full,
    input  wire [NTHREADS-1:0]   outbox_empty,
    // host control
    input  wire                  host_run_we,
    input  wire [NTHREADS-1:0]   host_run_val,
    input  wire [NTHREADS-1:0]   host_rst,     // one-cycle pulses: soft reset of thread n
    input  wire [NTHREADS-1:0]   host_pc_we,
    input  wire [7:0]            host_pc_val,
    output reg  [NTHREADS-1:0]   running,
    output reg  [NTHREADS-1:0]   halted,
    output reg  [NTHREADS-1:0]   blocked,
    output wire [7:0]  pc0_out,          // PC[0], PC[1] for the host read-back (10.4)
    output wire [7:0]  pc1_out,
    // capture and replay (SEMANTICS 14): CAPC rs, RDS bits 7 and 8
    output reg                   cr_ctrl_we,
    output wire [3:0]            cr_ctrl_val,
    input  wire                  cap_active,
    input  wire                  rep_active,
    // serializer (SEMANTICS 15): strobes of the committed instruction, the
    // engine's registered state, and the timers it takes its tick from
    output reg                   ser_cfg_we,     // SERCFG rs
    output reg                   ser_tx_we,      // SERTX SERTXC SERI SERIC take the holding register
    output wire                  ser_tx_c,       // SERTXC, SERIC: the byte is in the CRC
    output wire [7:0]            ser_wdata,      // rs[7:0] (SERCFG, SERTX, SERTXC) or n (SERI, SERIC)
    output reg                   ser_rx_ack,     // SERRX takes a byte or a frame end
    input  wire                  ser_tx_full,
    input  wire                  ser_tx_idle,
    input  wire                  ser_rx_valid,
    input  wire                  ser_rx_end,
    input  wire [15:0]           ser_rd_st,      // status (15.7)
    input  wire [15:0]           ser_rd_rx,      // SERRX data: byte if rx_valid, else status
    output wire [NTHREADS-1:0]   tm_tick,        // thread t: period != 0 and prescale = 0 (15.1)
    output wire [16*NTHREADS-1:0] tm_period,     // period[t] at [16t+15:16t]
    // debug / trace (unused in silicon)
    output wire        dbg_retire,
    output wire [$clog2(NTHREADS)-1:0] dbg_tid,
    output wire [7:0]  dbg_pc,
    output wire [15:0] dbg_ir
);
    localparam TW = $clog2(NTHREADS);    // thread id width
    localparam NT = NTHREADS;
    localparam [TW-1:0] TID_ONE = 1;
    localparam [NT-1:0] SEL_RESET = 1;   // thread 0 owns cycle 0

    // ---- thread state ------------------------------------------------------
    // mem2reg: plain registers with per-element enables, no memory inference
    (* mem2reg *) reg [15:0] regs [0:8*NT-1];   // {tid, r}
    (* mem2reg *) reg [7:0]  pc    [0:NT-1];
    (* mem2reg *) reg [7:0]  lr    [0:NT-1];
    (* mem2reg *) reg        fz    [0:NT-1];
    (* mem2reg *) reg        fc    [0:NT-1];
    // timer (section 6): NOW counts ticks, one tick every `period` cycles
    (* mem2reg *) reg [15:0] period   [0:NT-1];   // 0 = timer disabled, NOW frozen
    (* mem2reg *) reg [15:0] prescale [0:NT-1];   // cycles left before the next tick
    (* mem2reg *) reg [15:0] now      [0:NT-1];   // NOW
    (* mem2reg *) reg [15:0] deadline [0:NT-1];   // DEADLINE
    (* mem2reg *) reg [7:0]  delay    [0:NT-1];
    reg [15:0] cyc;

    // ---- slot owner ----------------------------------------------------------
    // One-hot thread select copies, one per consumer group (header comment).
    (* keep *) reg [NT-1:0] sel_rf;
    (* keep *) reg [NT-1:0] sel_tm;
    (* keep *) reg [NT-1:0] sel_pc;
    (* keep *) reg [NT-1:0] sel_io;

    always @(posedge clk) begin
        if (!rst_n) begin
            cyc    <= 16'd0;
            sel_rf <= SEL_RESET;
            sel_tm <= SEL_RESET;
            sel_pc <= SEL_RESET;
            sel_io <= SEL_RESET;
        end else begin
            cyc    <= cyc + 16'd1;
            sel_rf <= {sel_rf[NT-2:0], sel_rf[NT-1]};
            sel_tm <= {sel_tm[NT-2:0], sel_tm[NT-1]};
            sel_pc <= {sel_pc[NT-2:0], sel_pc[NT-1]};
            sel_io <= {sel_io[NT-2:0], sel_io[NT-1]};
        end
    end

    wire [TW-1:0] tid  = cyc[TW-1:0];    // slot owner
    wire [TW-1:0] otid = tid + TID_ONE;  // next slot's owner = "the other thread"
    // the other thread, one-hot: next cycle's owner
    wire [NT-1:0] osel_pc = {sel_pc[NT-2:0], sel_pc[NT-1]};

    // Thread t executes in this cycle (2.1), one copy per group: flops only.
    wire [NT-1:0] run_ok = running & {NT{fetch_ok}};
    wire [NT-1:0] x_rf   = sel_rf & run_ok;
    wire [NT-1:0] x_tm   = sel_tm & run_ok;
    wire [NT-1:0] x_pc   = sel_pc & run_ok;
    wire [NT-1:0] x_io   = sel_io & run_ok;
    wire [NT-1:0] xo_pc  = {x_pc[NT-2:0], x_pc[NT-1]};   // bit t: thread t-1 executes

    wire exec    = |x_pc;                // running[tid] & fetch_ok
    wire exec_io = |x_io;                // the same, for the strobes

    // ---- per-thread reads: AND-OR over a one-hot copy ----------------------
    reg [16*8-1:0] rf_thr;               // the slot owner's register k at [16k+15:16k]
    reg [15:0] tm_now, tm_dl;            // NOW, DEADLINE of the slot owner
    reg [7:0]  pc_cur, lr_cur, dly_cur;  // PC, LR, DELAY count of the slot owner
    reg [7:0]  pc_oth;                   // PC of the other thread (next fetch)
    reg        z_cur, c_cur, run_oth;
    reg [7:0]  in_head;
    reg        in_empty, in_full, out_full, out_empty;
    integer sr, kr, st, sp, si;          // one loop variable per always block

    always @(*) begin
        rf_thr = {16*8{1'b0}};
        for (sr = 0; sr < NT; sr = sr + 1)
            for (kr = 0; kr < 8; kr = kr + 1)
                rf_thr[16*kr +: 16] = rf_thr[16*kr +: 16] | (regs[8*sr+kr] & {16{sel_rf[sr]}});
    end

    always @(*) begin
        tm_now = 16'd0; tm_dl = 16'd0;
        for (st = 0; st < NT; st = st + 1) begin
            tm_now = tm_now | (now[st]      & {16{sel_tm[st]}});
            tm_dl  = tm_dl  | (deadline[st] & {16{sel_tm[st]}});
        end
    end

    always @(*) begin
        pc_cur = 8'd0; lr_cur = 8'd0; dly_cur = 8'd0; pc_oth = 8'd0;
        z_cur = 1'b0; c_cur = 1'b0; run_oth = 1'b0;
        for (sp = 0; sp < NT; sp = sp + 1) begin
            pc_cur  = pc_cur  | (pc[sp]    & {8{sel_pc[sp]}});
            lr_cur  = lr_cur  | (lr[sp]    & {8{sel_pc[sp]}});
            dly_cur = dly_cur | (delay[sp] & {8{sel_pc[sp]}});
            pc_oth  = pc_oth  | (pc[sp]    & {8{osel_pc[sp]}});
            z_cur   = z_cur   | (fz[sp] & sel_pc[sp]);
            c_cur   = c_cur   | (fc[sp] & sel_pc[sp]);
            run_oth = run_oth | (running[sp] & osel_pc[sp]);
        end
    end

    always @(*) begin
        in_head = 8'd0;
        in_empty = 1'b0; in_full = 1'b0; out_full = 1'b0; out_empty = 1'b0;
        for (si = 0; si < NT; si = si + 1) begin
            in_head   = in_head   | (inbox_rdata[8*si +: 8] & {8{sel_io[si]}});
            in_empty  = in_empty  | (inbox_empty[si]  & sel_io[si]);
            in_full   = in_full   | (inbox_full[si]   & sel_io[si]);
            out_full  = out_full  | (outbox_full[si]  & sel_io[si]);
            out_empty = out_empty | (outbox_empty[si] & sel_io[si]);
        end
    end

    assign fetch_addr = pc_oth;
    assign pc0_out    = pc[0];
    assign pc1_out    = pc[1];

    // ---- decode ------------------------------------------------------------
    wire [15:0] ir   = imem_rdata;
    wire [3:0]  maj  = ir[15:12];
    wire [2:0]  ra   = ir[11:9];
    wire [2:0]  rb   = ir[8:6];
    wire [15:0] A    = rf_thr[{ra, 4'd0} +: 16];
    wire [15:0] B    = rf_thr[{rb, 4'd0} +: 16];
    wire [7:0]  imm8 = ir[7:0];
    wire [15:0] simm8 = {{8{ir[7]}}, ir[7:0]};
    wire [4:0]  pin  = ir[4:0];
    wire [4:0]  bpin = ir[10:6];
    wire        Z    = z_cur;
    wire        C    = c_cur;

    wire [31:0] lvl32  = {8'd0, level};
    wire [31:0] lvl232 = {8'd0, level2};
    wire        lv     = lvl32[pin];
    wire        lv2    = lvl232[pin];
    wire        blv    = lvl32[bpin];

    wire [7:0] pc_inc  = pc_cur + 8'd1;
    wire [7:0] pc_rel8 = pc_inc + imm8;                     // sext(off8) wraps the same way
    wire [7:0] pc_rel6 = pc_inc + {{2{ir[5]}}, ir[5:0]};
    wire [7:0] pc_rel5 = pc_inc + {{3{ir[4]}}, ir[4:0]};

    wire [15:0] a_dec  = A - 16'd1;                          // DEC, DJNZ, SETT
    wire        a_zero = (A == 16'd0);

    // ---- timer datapath of the executing thread (section 6.2) --------------
    // Two results, each one carry chain deep, side by side:
    //   tm_sum     = base + imm8, base = NOW for SETD and DEADLINE otherwise:
    //                the DEADLINE write data, NOW + k for SETD and the target
    //                DEADLINE + k for WAITD. Only those two write DEADLINE
    //                (dl_we), so imm8 is added ungated.
    //   tm_diff    = NOW - (DEADLINE + K), K = {8'd0, tm_k}, tm_k = imm8 in
    //                major MISC and 0 otherwise, computed without forming
    //                DEADLINE + K: NOW - (DEADLINE + K) = NOW + ~DEADLINE + ~K
    //                + 2 (mod 2^16). A 3:2 carry-save step over (NOW,
    //                ~DEADLINE, ~K), per bit a full adder's sum (XOR3) and
    //                carry (majority) and no carry between bits, gives
    //                tm_cs_s + 2 tm_cs_c = NOW + ~DEADLINE + ~K; one 16-bit
    //                carry chain then adds tm_cs_s + {tm_cs_c[14:0], 1} + 1
    //                (tm_cpa: the 1s in bit 0 of both operands make the
    //                carry-in).
    //   tm_reached = ~tm_diff[15]: reached(a, b) is bit 15 of a - b being 0
    //                (section 1).
    // Consumers: WAITD (major MISC, K = k): tm_reached = reached(NOW,
    // DEADLINE + k), its completion test. RDT (tm_diff), BDR, RDS bit 4 and
    // the timeout-form waits (tm_reached) are in majors XFER, BCC and PIN,
    // so K = 0: NOW - DEADLINE and reached(NOW, DEADLINE). No other
    // instruction of major MISC reads tm_diff or tm_reached; one that needs
    // reached(NOW, DEADLINE) must narrow tm_k to WAITD.
    // The completion test, which feeds done and through it the DEADLINE,
    // PC, flag and blocked write enables, is thus a 4-bit major decode, one
    // compressor level and one carry chain. The compressor is written
    // bitwise, not as a three-operand sum, so that synthesis keeps it a
    // carry-save step.
    wire        tm_misc    = (maj == `KEYER_MAJ_MISC);
    wire        tm_setd    = tm_misc & (ir[11:8] == `KEYER_SETD);
    wire [15:0] tm_base    = tm_setd ? tm_now : tm_dl;
    wire [15:0] tm_sum     = tm_base + {8'd0, imm8};
    wire [7:0]  tm_k       = tm_misc ? imm8 : 8'd0;
    wire [15:0] tm_cs_x    = tm_now;
    wire [15:0] tm_cs_y    = ~tm_dl;
    wire [15:0] tm_cs_z    = ~{8'd0, tm_k};
    wire [15:0] tm_cs_s    = tm_cs_x ^ tm_cs_y ^ tm_cs_z;
    wire [15:0] tm_cs_c    = (tm_cs_x & tm_cs_y) | (tm_cs_x & tm_cs_z) | (tm_cs_y & tm_cs_z);
    wire [16:0] tm_cpa     = {tm_cs_s, 1'b1} + {tm_cs_c[14:0], 2'b11};
    wire [15:0] tm_diff    = tm_cpa[16:1];
    wire        tm_reached = ~tm_diff[15];

    // ---- RDS status word (section 11) --------------------------------------
    wire [15:0] tid16 = {{(16-TW){1'b0}}, tid};
    wire [15:0] rds_word = {7'd0, rep_active, cap_active, tid16[0], run_oth, tm_reached,
                            out_full, out_empty, in_full, in_empty}
                         | ((tid16 >> 1) << 9);
    assign cr_ctrl_val = A[3:0];                             // CAPC rs: rs[3:0]

    // ---- serializer operands (section 15.2) ----------------------------------
    // Decoded from the instruction word beside the main decode, not behind
    // it. ser_word is the engine's read data: rd_rx (byte or status, chosen
    // in the engine from its own rx_valid) for SERRX, rd_st for SERST and
    // everything else; one 2:1 mux on the function field, from flops.
    wire [3:0]  ser_g    = ir[`KEYER_SER_G_MSB:`KEYER_SER_G_LSB];
    wire        ser_g_rx = (ser_g == `KEYER_SERG_RX);
    wire [15:0] ser_word = ser_g_rx ? ser_rd_rx : ser_rd_st;
    assign ser_wdata = tm_misc ? imm8 : A[7:0];
    assign ser_tx_c  = tm_misc ? (ir[11:8] == `KEYER_SERIC) : (ser_g == `KEYER_SERG_TXC);

    // ---- execute (combinational) -------------------------------------------
    reg        done;
    reg        wr_en;
    reg [15:0] wr_val;
    reg        z_we, z_val, c_we, c_val;
    reg [7:0]  pc_next;
    reg        lr_we;
    reg        do_sett, dl_we, do_halt, do_start, do_stop;
    reg        delay_load, delay_dec;
    reg        is_wait, wait_base, wait_tmo;
    reg        pop_req, push_req;        // the slot owner's inbox pop / outbox push
    reg        ser_cfg_req, ser_tx_req, ser_rx_req;
    reg [16:0] sum;

    always @(*) begin
        done = 1'b1;
        wr_en = 1'b0; wr_val = 16'd0;
        z_we = 1'b0; z_val = 1'b0; c_we = 1'b0; c_val = 1'b0;
        pc_next = pc_inc;
        lr_we = 1'b0;
        do_sett = 1'b0; dl_we = 1'b0; do_halt = 1'b0; do_start = 1'b0; do_stop = 1'b0;
        delay_load = 1'b0; delay_dec = 1'b0;
        is_wait = 1'b0; wait_base = 1'b0; wait_tmo = 1'b0;
        pin_valid = 1'b0; pin_op = 3'd0; pin_pin = pin; pin_data = 8'd0;
        pop_req = 1'b0; push_req = 1'b0; outbox_wdata = A[7:0];
        cr_ctrl_we = 1'b0;
        ser_cfg_req = 1'b0; ser_tx_req = 1'b0; ser_rx_req = 1'b0;
        sum = 17'd0;

        case (maj)
        `KEYER_MAJ_ALU2: begin
            wr_en = 1'b1; z_we = 1'b1;
            case (ir[5:2])
                `KEYER_ADD: begin sum = {1'b0, A} + {1'b0, B}; wr_val = sum[15:0]; c_we = 1'b1; c_val = sum[16]; end
                `KEYER_SUB: begin sum = {1'b0, A} - {1'b0, B}; wr_val = sum[15:0]; c_we = 1'b1; c_val = sum[16]; end
                `KEYER_AND: wr_val = A & B;
                `KEYER_OR:  wr_val = A | B;
                `KEYER_XOR: wr_val = A ^ B;
                `KEYER_MOV: wr_val = B;
                `KEYER_CMP: begin sum = {1'b0, A} - {1'b0, B}; wr_val = sum[15:0]; wr_en = 1'b0; c_we = 1'b1; c_val = sum[16]; end
                `KEYER_TST: begin wr_val = A & B; wr_en = 1'b0; end
                `KEYER_ADC: begin sum = {1'b0, A} + {1'b0, B} + {16'd0, C}; wr_val = sum[15:0]; c_we = 1'b1; c_val = sum[16]; end
                `KEYER_SBC: begin sum = {1'b0, A} - {1'b0, B} - {16'd0, C}; wr_val = sum[15:0]; c_we = 1'b1; c_val = sum[16]; end
                default: begin wr_en = 1'b0; z_we = 1'b0; end
            endcase
            z_val = (wr_val == 16'd0);
        end
        `KEYER_MAJ_ALU1: begin
            wr_en = 1'b1; z_we = 1'b1;
            case (ir[8:5])
                `KEYER_SHL:  begin wr_val = {A[14:0], 1'b0}; c_we = 1'b1; c_val = A[15]; end
                `KEYER_SHR:  begin wr_val = {1'b0, A[15:1]}; c_we = 1'b1; c_val = A[0]; end
                `KEYER_RCL:  begin wr_val = {A[14:0], C};    c_we = 1'b1; c_val = A[15]; end
                `KEYER_RCR:  begin wr_val = {C, A[15:1]};    c_we = 1'b1; c_val = A[0]; end
                `KEYER_NOT:  wr_val = ~A;
                `KEYER_NEG:  begin wr_val = 16'd0 - A; c_we = 1'b1; c_val = ~a_zero; end
                `KEYER_INC:  begin sum = {1'b0, A} + 17'd1; wr_val = sum[15:0]; c_we = 1'b1; c_val = sum[16]; end
                `KEYER_DEC:  begin wr_val = a_dec; c_we = 1'b1; c_val = a_zero; end
                `KEYER_SWAP: wr_val = {A[7:0], A[15:8]};
                `KEYER_REV8: wr_val = {A[15:8], A[0], A[1], A[2], A[3], A[4], A[5], A[6], A[7]};
                `KEYER_DJNZ: begin
                    wr_val = a_dec; z_we = 1'b0;
                    if (wr_val != 16'd0) pc_next = pc_rel5;
                end
                default: begin wr_en = 1'b0; z_we = 1'b0; end
            endcase
            z_val = (wr_val == 16'd0);
        end
        `KEYER_MAJ_ADDI: begin
            sum = {1'b0, A} + {1'b0, simm8};
            wr_en = 1'b1; wr_val = sum[15:0]; z_we = 1'b1; z_val = (wr_val == 16'd0); c_we = 1'b1; c_val = sum[16];
        end
        `KEYER_MAJ_ANDI: begin wr_en = 1'b1; wr_val = A & {8'd0, imm8}; z_we = 1'b1; z_val = (wr_val == 16'd0); end
        `KEYER_MAJ_ORI:  begin wr_en = 1'b1; wr_val = A | {8'd0, imm8}; z_we = 1'b1; z_val = (wr_val == 16'd0); end
        `KEYER_MAJ_XORI: begin wr_en = 1'b1; wr_val = A ^ {8'd0, imm8}; z_we = 1'b1; z_val = (wr_val == 16'd0); end
        `KEYER_MAJ_LDI:  begin wr_en = 1'b1; wr_val = {8'd0, imm8}; end
        `KEYER_MAJ_LDIH: begin wr_en = 1'b1; wr_val = {imm8, A[7:0]}; end
        `KEYER_MAJ_CMPI: begin
            sum = {1'b0, A} - {9'd0, imm8};
            z_we = 1'b1; z_val = (sum[15:0] == 16'd0); c_we = 1'b1; c_val = sum[16];
        end
        `KEYER_MAJ_BCC: begin
            case (ir[11:9])
                `KEYER_COND_RA:  pc_next = pc_rel8;
                `KEYER_COND_EQ:  if (Z)  pc_next = pc_rel8;
                `KEYER_COND_NE:  if (!Z) pc_next = pc_rel8;
                `KEYER_COND_CS:  if (C)  pc_next = pc_rel8;
                `KEYER_COND_CC:  if (!C) pc_next = pc_rel8;
                `KEYER_COND_FE:  if (in_empty)  pc_next = pc_rel8;
                `KEYER_COND_FNE: if (!in_empty) pc_next = pc_rel8;
                `KEYER_COND_DR:  if (tm_reached) pc_next = pc_rel8;
                default: ;
            endcase
        end
        `KEYER_MAJ_BPIN: if (blv == ir[11]) pc_next = pc_rel6;
        `KEYER_MAJ_JMP: begin
            pc_next = ir[7:0];
            lr_we = ir[11];
        end
        `KEYER_MAJ_PIN: begin
            wait_tmo = ir[`KEYER_PIN_TBIT];
            case (ir[11:8])
                `KEYER_SET:  begin pin_valid = 1'b1; pin_op = 3'd0; pin_data = 8'd1; end
                `KEYER_CLR:  begin pin_valid = 1'b1; pin_op = 3'd0; pin_data = 8'd0; end
                `KEYER_OEN:  begin pin_valid = 1'b1; pin_op = 3'd1; end
                `KEYER_OEF:  begin pin_valid = 1'b1; pin_op = 3'd2; end
                `KEYER_OD:   begin pin_valid = 1'b1; pin_op = 3'd3; end
                `KEYER_PP:   begin pin_valid = 1'b1; pin_op = 3'd4; end
                `KEYER_WT0:  begin is_wait = 1'b1; wait_base = ~lv; end
                `KEYER_WT1:  begin is_wait = 1'b1; wait_base = lv; end
                `KEYER_WTR:  begin is_wait = 1'b1; wait_base = lv & ~lv2; end
                `KEYER_WTF:  begin is_wait = 1'b1; wait_base = ~lv & lv2; end
                `KEYER_WRC:  begin pin_valid = 1'b1; pin_op = 3'd0; pin_data = {7'd0, C}; end
                `KEYER_RDC:  begin c_we = 1'b1; c_val = lv; end
                `KEYER_TSTP: begin z_we = 1'b1; z_val = ~lv; end
                default: ;
            endcase
        end
        `KEYER_MAJ_PINR: begin
            case (ir[8:6])
                `KEYER_OUTR: begin pin_valid = 1'b1; pin_op = 3'd0; pin_data = {7'd0, A[0]}; end
                `KEYER_INR:  begin wr_en = 1'b1; wr_val = {15'd0, lv}; z_we = 1'b1; z_val = ~lv; end
                default: ;
            endcase
        end
        `KEYER_MAJ_XFER: begin
            wait_tmo = ir[`KEYER_XFER_TBIT];
            case (ir[8:5])
                // PUSH/POP: the base effect happens only when the base condition holds
                `KEYER_PUSH: begin is_wait = 1'b1; wait_base = ~out_full; push_req = ~out_full; end
                `KEYER_POP:  begin is_wait = 1'b1; wait_base = ~in_empty; pop_req = ~in_empty; wr_en = ~in_empty; wr_val = {8'd0, in_head}; end
                `KEYER_RDS:  begin wr_en = 1'b1; wr_val = rds_word; end
                `KEYER_RDCYC: begin wr_en = 1'b1; wr_val = cyc; end
                `KEYER_SETT: do_sett = 1'b1;
                `KEYER_RDT:  begin wr_en = 1'b1; wr_val = tm_diff; end
                `KEYER_PUSHNB: begin push_req = ~out_full; c_we = 1'b1; c_val = ~out_full; end
                `KEYER_POPNB:  begin pop_req = ~in_empty; wr_en = ~in_empty; wr_val = {8'd0, in_head}; c_we = 1'b1; c_val = ~in_empty; end
                `KEYER_OUTB:  begin pin_valid = 1'b1; pin_op = 3'd5; pin_data = A[7:0]; end
                `KEYER_INB:   begin wr_en = 1'b1; wr_val = {8'd0, level[7:0]}; z_we = 1'b1; z_val = (level[7:0] == 8'd0); end
                `KEYER_INW:   begin wr_en = 1'b1; wr_val = level[15:0]; z_we = 1'b1; z_val = (level[15:0] == 16'd0); end
                `KEYER_OUTOE: begin pin_valid = 1'b1; pin_op = 3'd6; pin_data = A[7:0]; end
                `KEYER_RDLR:  begin wr_en = 1'b1; wr_val = {8'd0, lr_cur}; end
                `KEYER_JMPR:  pc_next = A[7:0];
                `KEYER_CAPC:  cr_ctrl_we = 1'b1;
                // serializer (15.2); the T bit makes TX TXC RX WT timeout forms
                // and is ignored by CFG and ST. Undefined functions: no effect.
                `KEYER_SER: begin
                    wr_val = ser_word;
                    case (ser_g)
                        `KEYER_SERG_CFG: ser_cfg_req = 1'b1;
                        `KEYER_SERG_TX, `KEYER_SERG_TXC: begin
                            is_wait = 1'b1; wait_base = ~ser_tx_full; ser_tx_req = ~ser_tx_full;
                        end
                        `KEYER_SERG_RX: begin
                            is_wait = 1'b1; wait_base = ser_rx_valid | ser_rx_end;
                            ser_rx_req = wait_base; wr_en = wait_base;
                            z_we = wait_base; z_val = ~ser_rx_valid;
                        end
                        `KEYER_SERG_ST: wr_en = 1'b1;
                        `KEYER_SERG_WT: begin is_wait = 1'b1; wait_base = ser_tx_idle & ~ser_tx_full; end
                        default: ;
                    endcase
                end
                default: ;
            endcase
        end
        `KEYER_MAJ_MISC: begin
            case (ir[11:8])
                `KEYER_NOP:   ;
                `KEYER_HALT:  do_halt = 1'b1;
                `KEYER_RET:   pc_next = lr_cur;
                `KEYER_WAITD: begin done = tm_reached; dl_we = 1'b1; end   // DEADLINE <= DEADLINE + k
                `KEYER_DELAY: begin
                    if (dly_cur == 8'd0) begin
                        if (imm8 == 8'd0) done = 1'b1;
                        else begin done = 1'b0; delay_load = 1'b1; end
                    end else begin
                        delay_dec = 1'b1;
                        done = (dly_cur == 8'd1);
                    end
                end
                `KEYER_SETC:  begin c_we = 1'b1; c_val = 1'b1; end
                `KEYER_CLC:   begin c_we = 1'b1; c_val = 1'b0; end
                `KEYER_START: do_start = 1'b1;
                `KEYER_STOP:  do_stop = 1'b1;
                `KEYER_SETD:  dl_we = 1'b1;                                // DEADLINE <= NOW + k
                // SERI n / SERIC n: SERTX / SERTXC with the byte n; no timeout form
                `KEYER_SERI, `KEYER_SERIC: begin
                    is_wait = 1'b1; wait_base = ~ser_tx_full; ser_tx_req = ~ser_tx_full;
                end
                default: ;
            endcase
        end
        default: ;
        endcase

        // Blocking waits (7.2-7.4). The base form completes on its condition
        // and leaves C alone. The timeout form (T bit set) also completes when
        // reached(NOW, DEADLINE), with C = 0 if the base condition held and
        // C = 1 otherwise; the base effect above is already gated by it.
        if (is_wait) begin
            done = wait_base | (wait_tmo & tm_reached);
            c_we = wait_tmo; c_val = ~wait_base;
        end

        // nothing leaves the core unless the thread really executes this cycle
        if (!exec_io) begin
            pin_valid = 1'b0;
            cr_ctrl_we = 1'b0;
        end
    end

    // serializer strobes, from the io copy like the pin and CAPC strobes
    always @(*) begin
        ser_cfg_we = exec_io & ser_cfg_req;
        ser_tx_we  = exec_io & ser_tx_req;
        ser_rx_ack = exec_io & ser_rx_req;
    end

    // FIFO strobes: one per thread, from the io copy
    always @(*) begin
        inbox_pop   = x_io & {NT{pop_req}};
        outbox_push = x_io & {NT{push_req}};
    end

    // ---- state update ------------------------------------------------------
    wire commit = exec & done;

    // Write enables. wr_en, z_we, lr_we and do_sett are only ever set by
    // instructions that complete (done = 1; formal D1), so their enables use
    // "thread t executes" where the other enables use "thread t commits",
    // which keeps done (the timer comparator and the wait conditions) off the
    // register-file write path. The register-file enable is pre-decoded:
    // rf_pre[8t+i] (thread t executes and ra = i) depends only on flops and ir.
    wire [7:0]      ra_hot = 8'd1 << ra;
    wire [8*NT-1:0] rf_pre;
    wire [8*NT-1:0] rf_we  = rf_pre & {8*NT{wr_en}};
    wire [NT-1:0]   c_pc   = x_pc & {NT{done}};     // thread t commits (PC group)
    wire [NT-1:0]   c_tm   = x_tm & {NT{done}};     // thread t commits (timer group)
    genvar g;
    generate
        for (g = 0; g < NT; g = g + 1) begin : g_rf_pre
            assign rf_pre[8*g +: 8] = ra_hot & {8{x_rf[g]}};
        end
        // the serializer's symbol tick and period (15.1), from the timer flops
        for (g = 0; g < NT; g = g + 1) begin : g_tm_out
            assign tm_tick[g]            = (period[g] != 16'd0) & (prescale[g] == 16'd0);
            assign tm_period[16*g +: 16] = period[g];
        end
    endgenerate

    integer r, tt, tp;                   // one loop variable per always block

    // register file (4)
    always @(posedge clk) begin
        if (!rst_n) begin
            for (r = 0; r < 8*NT; r = r + 1) regs[r] <= 16'd0;
        end else begin
            for (r = 0; r < 8*NT; r = r + 1)
                if (rf_we[r]) regs[r] <= wr_val;
        end
    end

    // timers: every thread, every cycle, running or not (2.3). Tick rule
    // (6.1): with the timer enabled, prescale counts period-1 .. 0 and NOW
    // advances when it wraps. SETT and SETD/WAITD (6.2) and the soft reset
    // (3.2) override it, in that order.
    always @(posedge clk) begin
        if (!rst_n) begin
            for (tt = 0; tt < NT; tt = tt + 1) begin
                period[tt] <= 16'd0; prescale[tt] <= 16'd0; now[tt] <= 16'd0; deadline[tt] <= 16'd0;
            end
        end else begin
            for (tt = 0; tt < NT; tt = tt + 1) begin
                if (period[tt] != 16'd0) begin
                    prescale[tt] <= ((prescale[tt] == 16'd0) ? period[tt] : prescale[tt]) - 16'd1;
                    if (prescale[tt] == 16'd0) now[tt] <= now[tt] + 16'd1;
                end
                if (c_tm[tt] & dl_we) deadline[tt] <= tm_sum;
                if (x_tm[tt] & do_sett) begin
                    period[tt]   <= A;
                    prescale[tt] <= a_zero ? 16'd0 : a_dec;
                    now[tt]      <= 16'd0;
                    deadline[tt] <= 16'd0;
                end
                if (host_rst[tt]) begin
                    period[tt] <= 16'd0; prescale[tt] <= 16'd0; now[tt] <= 16'd0; deadline[tt] <= 16'd0;
                end
            end
        end
    end

    // PC, LR, flags, DELAY count, blocked, running, halted
    always @(posedge clk) begin
        if (!rst_n) begin
            running <= {NT{1'b0}}; halted <= {NT{1'b0}}; blocked <= {NT{1'b0}};
            for (tp = 0; tp < NT; tp = tp + 1) begin
                pc[tp] <= 8'd0; lr[tp] <= 8'd0; fz[tp] <= 1'b0; fc[tp] <= 1'b0;
                delay[tp] <= 8'd0;
            end
        end else begin
            for (tp = 0; tp < NT; tp = tp + 1) begin
                // the executing thread: PC, flags, LR (2.2, 4)
                if (c_pc[tp]) begin
                    pc[tp] <= pc_next;
                    if (c_we) fc[tp] <= c_val;
                end
                if (x_pc[tp] & z_we)  fz[tp] <= z_val;
                if (x_pc[tp] & lr_we) lr[tp] <= pc_inc;
                if (sel_pc[tp]) blocked[tp] <= x_pc[tp] & ~done;
                // delay counter (7.6)
                if (x_pc[tp]) begin
                    if (delay_load) delay[tp] <= imm8;
                    else if (delay_dec) delay[tp] <= dly_cur - 8'd1;
                    if (done) delay[tp] <= 8'd0;
                end
            end

            // run / halt, core-initiated first, host last (2.3)
            for (tp = 0; tp < NT; tp = tp + 1) begin
                if (x_pc[tp]  & do_halt)  begin running[tp] <= 1'b0; halted[tp] <= 1'b1; end
                if (xo_pc[tp] & do_start) begin running[tp] <= 1'b1; halted[tp] <= 1'b0; end
                if (xo_pc[tp] & do_stop)  begin running[tp] <= 1'b0; delay[tp] <= 8'd0; end
            end
            if (host_run_we) begin
                for (tp = 0; tp < NT; tp = tp + 1) begin
                    if (host_run_val[tp]) begin
                        running[tp] <= 1'b1; halted[tp] <= 1'b0;
                    end else begin
                        running[tp] <= 1'b0; delay[tp] <= 8'd0;
                    end
                end
            end
            // host PC write and soft reset (3.2); the FIFOs are emptied in the top
            for (tp = 0; tp < NT; tp = tp + 1) begin
                if (host_pc_we[tp] && !running[tp]) pc[tp] <= host_pc_val;
                if (host_rst[tp]) begin
                    lr[tp] <= 8'd0; fz[tp] <= 1'b0; fc[tp] <= 1'b0;
                    delay[tp] <= 8'd0;
                end
            end
        end
    end

    assign dbg_retire = commit;
    assign dbg_tid    = tid;
    assign dbg_pc     = pc_cur;
    assign dbg_ir     = ir;

`ifdef FORMAL
    // Properties proved by formal/run_core_pdr.sh (abc pdr on core_pdr.sby;
    // formal/core.sby is the same task for smtbmc). f_past_valid guards $past.
    // They refer to the slot owner through tid (binary), independently of
    // the one-hot copies the implementation uses.
    reg f_past_valid = 1'b0;
    always @(posedge clk) f_past_valid <= 1'b1;
    always @(*) if (!f_past_valid) assume(!rst_n);

    // S1. Every thread-select copy is one-hot and equals the decode of tid
    //     (bit t set iff tid == t), in every cycle after the first reset.
    // D1. The fields whose write enables use "executes" instead of
    //     "commits" are only set by instructions that complete.
    integer fs;
    always @(*) if (f_past_valid) begin
        assert(sel_rf != {NT{1'b0}} && (sel_rf & (sel_rf - SEL_RESET)) == {NT{1'b0}});
        assert(sel_tm != {NT{1'b0}} && (sel_tm & (sel_tm - SEL_RESET)) == {NT{1'b0}});
        assert(sel_pc != {NT{1'b0}} && (sel_pc & (sel_pc - SEL_RESET)) == {NT{1'b0}});
        assert(sel_io != {NT{1'b0}} && (sel_io & (sel_io - SEL_RESET)) == {NT{1'b0}});
        if (wr_en || z_we || lr_we || do_sett) assert(done);
    end
    always @(*) for (fs = 0; fs < NT; fs = fs + 1) if (f_past_valid) begin
        assert(sel_rf[fs] == (tid == fs));
        assert(sel_tm[fs] == (tid == fs));
        assert(sel_pc[fs] == (tid == fs));
        assert(sel_io[fs] == (tid == fs));
    end

    // Timer decode and reference values, written from SEMANTICS 6 and 7
    // independently of the shared adder in the execute logic.
    wire [3:0]  f_maj   = ir[15:12];
    wire        f_waitd = (f_maj == `KEYER_MAJ_MISC) && (ir[11:8] == `KEYER_WAITD);
    wire        f_setd  = (f_maj == `KEYER_MAJ_MISC) && (ir[11:8] == `KEYER_SETD);
    wire        f_sett  = (f_maj == `KEYER_MAJ_XFER) && (ir[8:5] == `KEYER_SETT);
    wire        f_pwait = (f_maj == `KEYER_MAJ_PIN) &&
                          (ir[11:8] == `KEYER_WT0 || ir[11:8] == `KEYER_WT1 ||
                           ir[11:8] == `KEYER_WTR || ir[11:8] == `KEYER_WTF);
    wire        f_xwait = (f_maj == `KEYER_MAJ_XFER) &&
                          (ir[8:5] == `KEYER_PUSH || ir[8:5] == `KEYER_POP);
    wire        f_tform = (f_pwait && ir[`KEYER_PIN_TBIT]) || (f_xwait && ir[`KEYER_XFER_TBIT]);
    wire        f_bform = (f_pwait && !ir[`KEYER_PIN_TBIT]) || (f_xwait && !ir[`KEYER_XFER_TBIT]);
    wire        f_start = (f_maj == `KEYER_MAJ_MISC) && (ir[11:8] == `KEYER_START);
    wire        f_stop  = (f_maj == `KEYER_MAJ_MISC) && (ir[11:8] == `KEYER_STOP);
    wire [15:0] f_now   = now[tid];
    wire [15:0] f_dl    = deadline[tid];
    wire [15:0] f_d0    = f_now - f_dl;                         // NOW - DEADLINE
    wire [15:0] f_dk    = f_now - (f_dl + {8'd0, ir[7:0]});    // NOW - (DEADLINE + k)
    wire        f_reached   = !f_d0[15];                        // reached(NOW, DEADLINE)
    wire        f_reached_k = !f_dk[15];                        // reached(NOW, DEADLINE + k)
    wire [15:0] f_rs    = regs[{tid, ir[11:9]}];                // SETT operand
    // base condition of a wait (7.2, 7.3)
    wire        f_lv    = lvl32[ir[4:0]];
    wire        f_lv2   = lvl232[ir[4:0]];
    reg         f_base;
    always @(*) begin
        f_base = 1'b0;
        if (f_maj == `KEYER_MAJ_PIN) begin
            if (ir[11:8] == `KEYER_WT0) f_base = !f_lv;
            if (ir[11:8] == `KEYER_WT1) f_base = f_lv;
            if (ir[11:8] == `KEYER_WTR) f_base = f_lv && !f_lv2;
            if (ir[11:8] == `KEYER_WTF) f_base = !f_lv && f_lv2;
        end else if (ir[8:5] == `KEYER_PUSH) f_base = !outbox_full[tid];
        else f_base = !inbox_empty[tid];
    end
    reg  [NT-1:0] f_sett_t;                                     // thread t commits SETT
    integer fi;
    always @(*) for (fi = 0; fi < NT; fi = fi + 1) f_sett_t[fi] = commit && f_sett && tid == fi;

    // Arithmetic expectations are registered whole ($past(x + 1), not
    // $past(x) + 1): the check is then an equality of two registers, which
    // keeps PDR fast (about 1 s instead of minutes).
    integer ft, fr;
    always @(posedge clk) if (f_past_valid && $past(rst_n) && rst_n) begin
        // T1. A reached deadline completes the wait within the slot: WAITD k
        //     when reached(NOW, DEADLINE + k), a timeout form when
        //     reached(NOW, DEADLINE). Also stated as the exact completion
        //     rule (7.1-7.5), which forbids completing early.
        if (exec && f_waitd && f_reached_k) assert(done);
        if (exec && f_tform && f_reached) assert(done);
        if (f_waitd) assert(done == f_reached_k);
        if (f_tform) assert(done == (f_base || f_reached));
        if (f_bform) assert(done == f_base);
        // T8. A timeout-form wait whose base condition does not hold has no
        //     base effect: no FIFO push or pop and no register write.
        if (f_tform && !f_base) assert(inbox_pop == {NT{1'b0}} && outbox_push == {NT{1'b0}});
        if ($past(f_tform && !f_base))
            for (fr = 0; fr < 8*NT; fr = fr + 1) assert(regs[fr] == $past(regs[fr]));
        // T9. A thread that executes and does not complete (2.2, 7.1)
        //     changes no state but its DELAY count: no register is written,
        //     its flags and LR keep their values unless the host soft-resets
        //     it in that cycle, and no pin command, FIFO pop or push, CAPC
        //     strobe or serializer strobe leaves the core. Generally, a
        //     thread's flags change only when it commits or is soft-reset.
        //     (A blocked timeout-form wait never writes C.)
        if (exec && !done)
            assert(!pin_valid && !cr_ctrl_we && inbox_pop == {NT{1'b0}} && outbox_push == {NT{1'b0}}
                   && !ser_cfg_we && !ser_tx_we && !ser_rx_ack);
        if ($past(exec && !done))
            for (fr = 0; fr < 8*NT; fr = fr + 1) assert(regs[fr] == $past(regs[fr]));
        for (ft = 0; ft < NT; ft = ft + 1) begin
            // T2. A disabled timer (period 0) never changes NOW, except that
            //     SETT and the soft reset restart it (T3).
            if ($past(period[ft] == 16'd0) && !$past(f_sett_t[ft] || host_rst[ft]))
                assert(now[ft] == $past(now[ft]));
            // T3. NOW changes only by +1 on a tick (period != 0, prescale = 0)
            //     or to 0 on SETT or soft reset; a due tick is never lost;
            //     SETT and the soft reset leave NOW = DEADLINE = 0.
            if (now[ft] != $past(now[ft]))
                assert((now[ft] == $past(now[ft] + 16'd1)
                        && $past(period[ft] != 16'd0 && prescale[ft] == 16'd0))
                       || (now[ft] == 16'd0 && $past(f_sett_t[ft] || host_rst[ft])));
            if ($past(period[ft] != 16'd0 && prescale[ft] == 16'd0)
                && !$past(f_sett_t[ft] || host_rst[ft]))
                assert(now[ft] == $past(now[ft] + 16'd1));
            if ($past(f_sett_t[ft] || host_rst[ft]))
                assert(now[ft] == 16'd0 && deadline[ft] == 16'd0);
            // T4. DEADLINE changes only through SETD, a completed WAITD, SETT
            //     or the soft reset.
            if (deadline[ft] != $past(deadline[ft]))
                assert($past(host_rst[ft])
                       || $past(commit && tid == ft && (f_setd || f_waitd || f_sett)));
            // T5. A completed WAITD k advances DEADLINE by exactly k; SETD k
            //     sets it to NOW + k.
            if ($past(commit && tid == ft && f_waitd) && !$past(host_rst[ft]))
                assert(deadline[ft] == $past(deadline[ft] + {8'd0, ir[7:0]}));
            if ($past(commit && tid == ft && f_setd) && !$past(host_rst[ft]))
                assert(deadline[ft] == $past(now[ft] + {8'd0, ir[7:0]}));
            // T6. A timeout-form wait reports C = 0 if its base condition held
            //     and C = 1 otherwise; a base-form wait leaves C alone.
            if ($past(commit && tid == ft && f_tform) && !$past(host_rst[ft]))
                assert(fc[ft] == !$past(f_base));
            if ($past(commit && tid == ft && f_bform) && !$past(host_rst[ft]))
                assert(fc[ft] == $past(fc[ft]));
            // T7. period changes only through SETT and the soft reset; SETT
            //     loads period = rs, prescale = rs - 1 (0 if rs = 0); otherwise
            //     an enabled timer's prescale counts down and reloads
            //     period - 1 after 0 (6.1), and a disabled one holds.
            if ($past(host_rst[ft]))
                assert(period[ft] == 16'd0 && prescale[ft] == 16'd0);
            else if ($past(f_sett_t[ft]))
                assert(period[ft] == $past(f_rs) &&
                       prescale[ft] == $past(f_rs == 16'd0 ? 16'd0 : f_rs - 16'd1));
            else begin
                assert(period[ft] == $past(period[ft]));
                if ($past(period[ft] == 16'd0))
                    assert(prescale[ft] == $past(prescale[ft]));
                else
                    assert(prescale[ft] == $past(prescale[ft] == 16'd0 ? period[ft] - 16'd1
                                                                       : prescale[ft] - 16'd1));
            end
            // P3. The PC changes only when that thread commits an instruction
            //     or the host writes it while stopped.
            if (!$past(commit && tid == ft) && !$past(host_pc_we[ft] && !running[ft]))
                assert(pc[ft] == $past(pc[ft]));
            // P4. A thread starts only through the host or the other thread's START.
            if (!$past(running[ft]) && running[ft])
                assert($past(host_run_we && host_run_val[ft]) || $past(exec && do_start && otid == ft));
            // P5. Halted implies not running, unless the host restarted it this cycle.
            if (halted[ft] && !$past(host_run_we))
                assert(!running[ft]);
            // T9 (per thread, see above).
            if ($past(exec && !done && tid == ft) && !$past(host_rst[ft]))
                assert(fc[ft] == $past(fc[ft]) && fz[ft] == $past(fz[ft]) && lr[ft] == $past(lr[ft]));
            if (fc[ft] != $past(fc[ft]) || fz[ft] != $past(fz[ft]))
                assert($past(commit && tid == ft) || $past(host_rst[ft]));
            // P8. START and STOP act on the other thread, never on the
            //     executing one (9). After a thread commits START or STOP,
            //     with no host RUN write in that cycle: the other thread's
            //     running bit is 1 (START) or 0 (STOP); START clears its
            //     halted bit, STOP leaves it and clears its DELAY count; every
            //     other thread, the executing one included, keeps its running
            //     and halted bits.
            if ($past(commit && (f_start || f_stop)) && !$past(host_run_we)) begin
                if (ft == $past(otid)) begin
                    assert(running[ft] == $past(f_start));
                    if ($past(f_start)) assert(!halted[ft]);
                    else assert(halted[ft] == $past(halted[ft]) && delay[ft] == 8'd0);
                end else
                    assert(running[ft] == $past(running[ft]) && halted[ft] == $past(halted[ft]));
            end
        end
        // P6. Thread parity: the executing thread alternates every cycle
        //     (in general, advances by one mod NTHREADS).
        assert(tid == $past(tid) + TID_ONE);
        // P7. Only the executing thread's slot can commit, and a blocked
        //     thread's PC does not move.
        if ($past(exec && !done)) assert(pc[$past(tid)] == $past(pc_cur));
    end
`endif
endmodule
