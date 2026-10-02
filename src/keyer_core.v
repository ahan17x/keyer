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
    // debug / trace (unused in silicon)
    output wire        dbg_retire,
    output wire [$clog2(NTHREADS)-1:0] dbg_tid,
    output wire [7:0]  dbg_pc,
    output wire [15:0] dbg_ir
);
    localparam TW = $clog2(NTHREADS);    // thread id width
    localparam NT = NTHREADS;
    localparam [TW-1:0] TID_ONE = 1;

    // ---- thread state ------------------------------------------------------
    reg [15:0] regs [0:8*NT-1];          // {tid, r}
    reg [7:0]  pc    [0:NT-1];
    reg [7:0]  lr    [0:NT-1];
    reg        fz    [0:NT-1];
    reg        fc    [0:NT-1];
    // timer (section 6): NOW counts ticks, one tick every `period` cycles
    reg [15:0] period   [0:NT-1];        // 0 = timer disabled, NOW frozen
    reg [15:0] prescale [0:NT-1];        // cycles left before the next tick
    reg [15:0] now      [0:NT-1];        // NOW
    reg [15:0] deadline [0:NT-1];        // DEADLINE
    reg [7:0]  delay  [0:NT-1];
    reg [15:0] cyc;

    wire [TW-1:0] tid  = cyc[TW-1:0];    // slot owner
    wire [TW-1:0] otid = tid + TID_ONE;  // next slot's owner = "the other thread"
    assign fetch_addr = pc[otid];
    assign pc0_out    = pc[0];
    assign pc1_out    = pc[1];

    wire exec = running[tid] & fetch_ok;

    // ---- decode ------------------------------------------------------------
    wire [15:0] ir   = imem_rdata;
    wire [3:0]  maj  = ir[15:12];
    wire [2:0]  ra   = ir[11:9];
    wire [2:0]  rb   = ir[8:6];
    wire [15:0] A    = regs[{tid, ra}];
    wire [15:0] B    = regs[{tid, rb}];
    wire [7:0]  imm8 = ir[7:0];
    wire [15:0] simm8 = {{8{ir[7]}}, ir[7:0]};
    wire [4:0]  pin  = ir[4:0];
    wire [4:0]  bpin = ir[10:6];
    wire        Z    = fz[tid];
    wire        C    = fc[tid];
    wire [7:0]  in_head   = inbox_rdata[8*tid +: 8];
    wire        in_empty  = inbox_empty[tid];
    wire        in_full   = inbox_full[tid];
    wire        out_full  = outbox_full[tid];
    wire        out_empty = outbox_empty[tid];

    wire [31:0] lvl32  = {8'd0, level};
    wire [31:0] lvl232 = {8'd0, level2};
    wire        lv     = lvl32[pin];
    wire        lv2    = lvl232[pin];
    wire        blv    = lvl32[bpin];

    wire [7:0] pc_cur  = pc[tid];
    wire [7:0] pc_inc  = pc_cur + 8'd1;
    wire [7:0] pc_rel8 = pc_inc + imm8;                     // sext(off8) wraps the same way
    wire [7:0] pc_rel6 = pc_inc + {{2{ir[5]}}, ir[5:0]};
    wire [7:0] pc_rel5 = pc_inc + {{3{ir[4]}}, ir[4:0]};

    wire [15:0] a_dec  = A - 16'd1;                          // DEC, DJNZ, SETT
    wire        a_zero = (A == 16'd0);

    // ---- timer datapath of the executing thread (section 6.2) --------------
    // One adder and one subtractor serve every timer instruction:
    //   tm_sum  = base + k, base = NOW for SETD and DEADLINE otherwise,
    //             k = imm8 for SETD and WAITD and 0 otherwise;
    //   tm_diff = NOW - tm_sum.
    // WAITD: tm_sum is the target DEADLINE + k, tm_reached = reached(NOW, target).
    // SETD:  tm_sum = NOW + k is the new DEADLINE (tm_diff unused).
    // Any other instruction: tm_sum = DEADLINE, tm_diff = NOW - DEADLINE (RDT)
    // and tm_reached = reached(NOW, DEADLINE) (BDR, RDS bit 4, timeout forms).
    // reached(a, b) is bit 15 of a - b being 0 (section 1).
    wire        tm_misc    = (maj == `KEYER_MAJ_MISC);
    wire        tm_setd    = tm_misc & (ir[11:8] == `KEYER_SETD);
    wire        tm_waitd   = tm_misc & (ir[11:8] == `KEYER_WAITD);
    wire [15:0] tm_now     = now[tid];
    wire [15:0] tm_base    = tm_setd ? tm_now : deadline[tid];
    wire [7:0]  tm_k       = (tm_setd | tm_waitd) ? imm8 : 8'd0;
    wire [15:0] tm_sum     = tm_base + {8'd0, tm_k};
    wire [15:0] tm_diff    = tm_now - tm_sum;
    wire        tm_reached = ~tm_diff[15];

    // ---- RDS status word (section 11) --------------------------------------
    wire [15:0] tid16 = {{(16-TW){1'b0}}, tid};
    wire [15:0] rds_word = {7'd0, rep_active, cap_active, tid16[0], running[otid], tm_reached,
                            out_full, out_empty, in_full, in_empty}
                         | ((tid16 >> 1) << 9);
    assign cr_ctrl_val = A[3:0];                             // CAPC rs: rs[3:0]

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
        inbox_pop = {NT{1'b0}}; outbox_push = {NT{1'b0}}; outbox_wdata = A[7:0];
        cr_ctrl_we = 1'b0;
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
                `KEYER_PUSH: begin is_wait = 1'b1; wait_base = ~out_full; outbox_push[tid] = ~out_full; end
                `KEYER_POP:  begin is_wait = 1'b1; wait_base = ~in_empty; inbox_pop[tid] = ~in_empty; wr_en = ~in_empty; wr_val = {8'd0, in_head}; end
                `KEYER_RDS:  begin wr_en = 1'b1; wr_val = rds_word; end
                `KEYER_RDCYC: begin wr_en = 1'b1; wr_val = cyc; end
                `KEYER_SETT: do_sett = 1'b1;
                `KEYER_RDT:  begin wr_en = 1'b1; wr_val = tm_diff; end
                `KEYER_PUSHNB: begin outbox_push[tid] = ~out_full; c_we = 1'b1; c_val = ~out_full; end
                `KEYER_POPNB:  begin inbox_pop[tid] = ~in_empty; wr_en = ~in_empty; wr_val = {8'd0, in_head}; c_we = 1'b1; c_val = ~in_empty; end
                `KEYER_OUTB:  begin pin_valid = 1'b1; pin_op = 3'd5; pin_data = A[7:0]; end
                `KEYER_INB:   begin wr_en = 1'b1; wr_val = {8'd0, level[7:0]}; z_we = 1'b1; z_val = (level[7:0] == 8'd0); end
                `KEYER_INW:   begin wr_en = 1'b1; wr_val = level[15:0]; z_we = 1'b1; z_val = (level[15:0] == 16'd0); end
                `KEYER_OUTOE: begin pin_valid = 1'b1; pin_op = 3'd6; pin_data = A[7:0]; end
                `KEYER_RDLR:  begin wr_en = 1'b1; wr_val = {8'd0, lr[tid]}; end
                `KEYER_JMPR:  pc_next = A[7:0];
                `KEYER_CAPC:  cr_ctrl_we = 1'b1;
                default: ;
            endcase
        end
        `KEYER_MAJ_MISC: begin
            case (ir[11:8])
                `KEYER_NOP:   ;
                `KEYER_HALT:  do_halt = 1'b1;
                `KEYER_RET:   pc_next = lr[tid];
                `KEYER_WAITD: begin done = tm_reached; dl_we = 1'b1; end   // DEADLINE <= DEADLINE + k
                `KEYER_DELAY: begin
                    if (delay[tid] == 8'd0) begin
                        if (imm8 == 8'd0) done = 1'b1;
                        else begin done = 1'b0; delay_load = 1'b1; end
                    end else begin
                        delay_dec = 1'b1;
                        done = (delay[tid] == 8'd1);
                    end
                end
                `KEYER_SETC:  begin c_we = 1'b1; c_val = 1'b1; end
                `KEYER_CLC:   begin c_we = 1'b1; c_val = 1'b0; end
                `KEYER_START: do_start = 1'b1;
                `KEYER_STOP:  do_stop = 1'b1;
                `KEYER_SETD:  dl_we = 1'b1;                                // DEADLINE <= NOW + k
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
        if (!exec) begin
            pin_valid = 1'b0; inbox_pop = {NT{1'b0}}; outbox_push = {NT{1'b0}};
            cr_ctrl_we = 1'b0;
        end
    end

    // ---- state update ------------------------------------------------------
    wire commit = exec & done;
    integer t;

    always @(posedge clk) begin
        if (!rst_n) begin
            cyc <= 16'd0;
            running <= {NT{1'b0}}; halted <= {NT{1'b0}}; blocked <= {NT{1'b0}};
            for (t = 0; t < NT; t = t + 1) begin
                pc[t] <= 8'd0; lr[t] <= 8'd0; fz[t] <= 1'b0; fc[t] <= 1'b0;
                period[t] <= 16'd0; prescale[t] <= 16'd0; now[t] <= 16'd0; deadline[t] <= 16'd0;
                delay[t] <= 8'd0;
            end
            for (t = 0; t < 8*NT; t = t + 1) regs[t] <= 16'd0;
        end else begin
            cyc <= cyc + 16'd1;

            // register file, flags, pc, lr
            if (commit) begin
                if (wr_en) regs[{tid, ra}] <= wr_val;
                if (z_we) fz[tid] <= z_val;
                if (c_we) fc[tid] <= c_val;
                pc[tid] <= pc_next;
                if (lr_we) lr[tid] <= pc_inc;
            end
            blocked[tid] <= exec & ~done;

            // delay counter
            if (exec) begin
                if (delay_load) delay[tid] <= imm8;
                else if (delay_dec) delay[tid] <= delay[tid] - 8'd1;
                if (done) delay[tid] <= 8'd0;
            end

            // timers: every thread, every cycle, running or not (2.3). Tick
            // rule (6.1): with the timer enabled, prescale counts period-1 .. 0
            // and NOW advances when it wraps. SETT (6.2) and the soft reset
            // below override it.
            for (t = 0; t < NT; t = t + 1) begin
                if (period[t] != 16'd0) begin
                    prescale[t] <= ((prescale[t] == 16'd0) ? period[t] : prescale[t]) - 16'd1;
                    if (prescale[t] == 16'd0) now[t] <= now[t] + 16'd1;
                end
                if (commit && (tid == t[TW-1:0])) begin
                    if (dl_we) deadline[t] <= tm_sum;
                    if (do_sett) begin
                        period[t]   <= A;
                        prescale[t] <= a_zero ? 16'd0 : a_dec;
                        now[t]      <= 16'd0;
                        deadline[t] <= 16'd0;
                    end
                end
            end

            // run / halt, core-initiated first, host last
            if (exec && do_halt)  begin running[tid]  <= 1'b0; halted[tid] <= 1'b1; end
            if (exec && do_start) begin running[otid] <= 1'b1; halted[otid] <= 1'b0; end
            if (exec && do_stop)  begin running[otid] <= 1'b0; delay[otid] <= 8'd0; end
            if (host_run_we) begin
                for (t = 0; t < NT; t = t + 1) begin
                    if (host_run_val[t]) begin
                        running[t] <= 1'b1; halted[t] <= 1'b0;
                    end else begin
                        running[t] <= 1'b0; delay[t] <= 8'd0;
                    end
                end
            end
            // host PC write and soft reset (3.2); the FIFOs are emptied in the top
            for (t = 0; t < NT; t = t + 1) begin
                if (host_pc_we[t] && !running[t]) pc[t] <= host_pc_val;
                if (host_rst[t]) begin
                    lr[t] <= 8'd0; fz[t] <= 1'b0; fc[t] <= 1'b0;
                    period[t] <= 16'd0; prescale[t] <= 16'd0; now[t] <= 16'd0; deadline[t] <= 16'd0;
                    delay[t] <= 8'd0;
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
    reg f_past_valid = 1'b0;
    always @(posedge clk) f_past_valid <= 1'b1;
    always @(*) if (!f_past_valid) assume(!rst_n);

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
