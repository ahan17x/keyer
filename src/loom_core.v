/*
 * Loom core: two barrel-interleaved hardware threads, 16-bit datapath.
 *
 * Thread 0 executes on even cycles, thread 1 on odd cycles. The instruction
 * for the executing thread was fetched on the previous cycle from
 * imem_rdata (one-cycle synchronous memory). Every instruction completes in
 * its slot unless it is blocking, in which case the PC does not advance and
 * it is re-evaluated at the thread's next slot.
 *
 * Semantics are defined by docs/isa.md and tools/loomsim.py; cocotb runs both
 * in lockstep.
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none
`include "loom_isa.vh"

module loom_core (
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
    input  wire [7:0]  inbox0_rdata,
    input  wire [7:0]  inbox1_rdata,
    input  wire [1:0]  inbox_empty,
    input  wire [1:0]  inbox_full,
    output reg  [1:0]  inbox_pop,
    output reg  [7:0]  outbox_wdata,
    output reg  [1:0]  outbox_push,
    input  wire [1:0]  outbox_full,
    input  wire [1:0]  outbox_empty,
    // host control
    input  wire        host_run_we,
    input  wire [1:0]  host_run_val,
    input  wire [1:0]  host_rst,         // one-cycle pulses: soft reset of thread n
    input  wire [1:0]  host_pc_we,
    input  wire [7:0]  host_pc_val,
    output reg  [1:0]  running,
    output reg  [1:0]  halted,
    output reg  [1:0]  blocked,
    // debug / trace (unused in silicon)
    output wire        dbg_retire,
    output wire        dbg_tid,
    output wire [7:0]  dbg_pc,
    output wire [15:0] dbg_ir
);
    // ---- thread state ------------------------------------------------------
    reg [15:0] regs [0:15];              // {tid, r}
    reg [7:0]  pc    [0:1];
    reg [7:0]  lr    [0:1];
    reg        fz    [0:1];
    reg        fc    [0:1];
    reg [15:0] period [0:1];
    reg [15:0] count  [0:1];
    reg        tick   [0:1];
    reg [7:0]  delay  [0:1];
    reg [15:0] cyc;

    wire       tid   = cyc[0];
    wire       otid  = ~cyc[0];
    assign fetch_addr = pc[otid];

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
    wire        T    = tick[tid];
    wire [7:0]  inbox_rdata = tid ? inbox1_rdata : inbox0_rdata;
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

    // ---- execute (combinational) -------------------------------------------
    reg        done;
    reg        wr_en;
    reg [15:0] wr_val;
    reg        z_we, z_val, c_we, c_val;
    reg [7:0]  pc_next;
    reg        lr_we;
    reg        do_sett, do_clrt, do_halt, do_start, do_stop;
    reg        delay_load, delay_dec;
    reg [16:0] sum;

    always @(*) begin
        done = 1'b1;
        wr_en = 1'b0; wr_val = 16'd0;
        z_we = 1'b0; z_val = 1'b0; c_we = 1'b0; c_val = 1'b0;
        pc_next = pc_inc;
        lr_we = 1'b0;
        do_sett = 1'b0; do_clrt = 1'b0; do_halt = 1'b0; do_start = 1'b0; do_stop = 1'b0;
        delay_load = 1'b0; delay_dec = 1'b0;
        pin_valid = 1'b0; pin_op = 3'd0; pin_pin = pin; pin_data = 8'd0;
        inbox_pop = 2'b00; outbox_push = 2'b00; outbox_wdata = A[7:0];
        sum = 17'd0;

        case (maj)
        `LOOM_MAJ_ALU2: begin
            wr_en = 1'b1; z_we = 1'b1;
            case (ir[5:2])
                `LOOM_ADD: begin sum = {1'b0, A} + {1'b0, B}; wr_val = sum[15:0]; c_we = 1'b1; c_val = sum[16]; end
                `LOOM_SUB: begin sum = {1'b0, A} - {1'b0, B}; wr_val = sum[15:0]; c_we = 1'b1; c_val = sum[16]; end
                `LOOM_AND: wr_val = A & B;
                `LOOM_OR:  wr_val = A | B;
                `LOOM_XOR: wr_val = A ^ B;
                `LOOM_MOV: wr_val = B;
                `LOOM_CMP: begin sum = {1'b0, A} - {1'b0, B}; wr_val = sum[15:0]; wr_en = 1'b0; c_we = 1'b1; c_val = sum[16]; end
                `LOOM_TST: begin wr_val = A & B; wr_en = 1'b0; end
                `LOOM_ADC: begin sum = {1'b0, A} + {1'b0, B} + {16'd0, C}; wr_val = sum[15:0]; c_we = 1'b1; c_val = sum[16]; end
                `LOOM_SBC: begin sum = {1'b0, A} - {1'b0, B} - {16'd0, C}; wr_val = sum[15:0]; c_we = 1'b1; c_val = sum[16]; end
                default: begin wr_en = 1'b0; z_we = 1'b0; end
            endcase
            z_val = (wr_val == 16'd0);
        end
        `LOOM_MAJ_ALU1: begin
            wr_en = 1'b1; z_we = 1'b1;
            case (ir[8:5])
                `LOOM_SHL:  begin wr_val = {A[14:0], 1'b0}; c_we = 1'b1; c_val = A[15]; end
                `LOOM_SHR:  begin wr_val = {1'b0, A[15:1]}; c_we = 1'b1; c_val = A[0]; end
                `LOOM_RCL:  begin wr_val = {A[14:0], C};    c_we = 1'b1; c_val = A[15]; end
                `LOOM_RCR:  begin wr_val = {C, A[15:1]};    c_we = 1'b1; c_val = A[0]; end
                `LOOM_NOT:  wr_val = ~A;
                `LOOM_NEG:  begin wr_val = 16'd0 - A; c_we = 1'b1; c_val = (A != 16'd0); end
                `LOOM_INC:  begin sum = {1'b0, A} + 17'd1; wr_val = sum[15:0]; c_we = 1'b1; c_val = sum[16]; end
                `LOOM_DEC:  begin wr_val = A - 16'd1; c_we = 1'b1; c_val = (A == 16'd0); end
                `LOOM_SWAP: wr_val = {A[7:0], A[15:8]};
                `LOOM_REV8: wr_val = {A[15:8], A[0], A[1], A[2], A[3], A[4], A[5], A[6], A[7]};
                `LOOM_DJNZ: begin
                    wr_val = A - 16'd1; z_we = 1'b0;
                    if (wr_val != 16'd0) pc_next = pc_rel5;
                end
                default: begin wr_en = 1'b0; z_we = 1'b0; end
            endcase
            z_val = (wr_val == 16'd0);
        end
        `LOOM_MAJ_ADDI: begin
            sum = {1'b0, A} + {1'b0, simm8};
            wr_en = 1'b1; wr_val = sum[15:0]; z_we = 1'b1; z_val = (wr_val == 16'd0); c_we = 1'b1; c_val = sum[16];
        end
        `LOOM_MAJ_ANDI: begin wr_en = 1'b1; wr_val = A & {8'd0, imm8}; z_we = 1'b1; z_val = (wr_val == 16'd0); end
        `LOOM_MAJ_ORI:  begin wr_en = 1'b1; wr_val = A | {8'd0, imm8}; z_we = 1'b1; z_val = (wr_val == 16'd0); end
        `LOOM_MAJ_XORI: begin wr_en = 1'b1; wr_val = A ^ {8'd0, imm8}; z_we = 1'b1; z_val = (wr_val == 16'd0); end
        `LOOM_MAJ_LDI:  begin wr_en = 1'b1; wr_val = {8'd0, imm8}; end
        `LOOM_MAJ_LDIH: begin wr_en = 1'b1; wr_val = {imm8, A[7:0]}; end
        `LOOM_MAJ_CMPI: begin
            sum = {1'b0, A} - {9'd0, imm8};
            z_we = 1'b1; z_val = (sum[15:0] == 16'd0); c_we = 1'b1; c_val = sum[16];
        end
        `LOOM_MAJ_BCC: begin
            case (ir[11:9])
                `LOOM_COND_RA:  pc_next = pc_rel8;
                `LOOM_COND_EQ:  if (Z)  pc_next = pc_rel8;
                `LOOM_COND_NE:  if (!Z) pc_next = pc_rel8;
                `LOOM_COND_CS:  if (C)  pc_next = pc_rel8;
                `LOOM_COND_CC:  if (!C) pc_next = pc_rel8;
                `LOOM_COND_FE:  if (in_empty)  pc_next = pc_rel8;
                `LOOM_COND_FNE: if (!in_empty) pc_next = pc_rel8;
                `LOOM_COND_TP:  if (T)  pc_next = pc_rel8;
                default: ;
            endcase
        end
        `LOOM_MAJ_BPIN: if (blv == ir[11]) pc_next = pc_rel6;
        `LOOM_MAJ_JMP: begin
            pc_next = ir[7:0];
            lr_we = ir[11];
        end
        `LOOM_MAJ_PIN: begin
            case (ir[11:8])
                `LOOM_SET:  begin pin_valid = 1'b1; pin_op = 3'd0; pin_data = 8'd1; end
                `LOOM_CLR:  begin pin_valid = 1'b1; pin_op = 3'd0; pin_data = 8'd0; end
                `LOOM_OEN:  begin pin_valid = 1'b1; pin_op = 3'd1; end
                `LOOM_OEF:  begin pin_valid = 1'b1; pin_op = 3'd2; end
                `LOOM_OD:   begin pin_valid = 1'b1; pin_op = 3'd3; end
                `LOOM_PP:   begin pin_valid = 1'b1; pin_op = 3'd4; end
                `LOOM_WT0:  done = ~lv;
                `LOOM_WT1:  done = lv;
                `LOOM_WTR:  done = lv & ~lv2;
                `LOOM_WTF:  done = ~lv & lv2;
                `LOOM_WRC:  begin pin_valid = 1'b1; pin_op = 3'd0; pin_data = {7'd0, C}; end
                `LOOM_RDC:  begin c_we = 1'b1; c_val = lv; end
                `LOOM_TSTP: begin z_we = 1'b1; z_val = ~lv; end
                default: ;
            endcase
        end
        `LOOM_MAJ_PINR: begin
            case (ir[8:6])
                `LOOM_OUTR: begin pin_valid = 1'b1; pin_op = 3'd0; pin_data = {7'd0, A[0]}; end
                `LOOM_INR:  begin wr_en = 1'b1; wr_val = {15'd0, lv}; z_we = 1'b1; z_val = ~lv; end
                default: ;
            endcase
        end
        `LOOM_MAJ_XFER: begin
            case (ir[8:5])
                `LOOM_PUSH: begin done = ~out_full; outbox_push[tid] = ~out_full; end
                `LOOM_POP:  begin done = ~in_empty; inbox_pop[tid] = ~in_empty; wr_en = ~in_empty; wr_val = {8'd0, inbox_rdata}; end
                `LOOM_RDS:  begin wr_en = 1'b1; wr_val = {9'd0, tid, running[otid], T, out_full, out_empty, in_full, in_empty}; end
                `LOOM_RDCYC: begin wr_en = 1'b1; wr_val = cyc; end
                `LOOM_SETT: do_sett = 1'b1;
                `LOOM_RDT:  begin wr_en = 1'b1; wr_val = count[tid]; end
                `LOOM_PUSHNB: begin outbox_push[tid] = ~out_full; c_we = 1'b1; c_val = ~out_full; end
                `LOOM_POPNB:  begin inbox_pop[tid] = ~in_empty; wr_en = ~in_empty; wr_val = {8'd0, inbox_rdata}; c_we = 1'b1; c_val = ~in_empty; end
                `LOOM_OUTB:  begin pin_valid = 1'b1; pin_op = 3'd5; pin_data = A[7:0]; end
                `LOOM_INB:   begin wr_en = 1'b1; wr_val = {8'd0, level[7:0]}; z_we = 1'b1; z_val = (level[7:0] == 8'd0); end
                `LOOM_INW:   begin wr_en = 1'b1; wr_val = level[15:0]; z_we = 1'b1; z_val = (level[15:0] == 16'd0); end
                `LOOM_OUTOE: begin pin_valid = 1'b1; pin_op = 3'd6; pin_data = A[7:0]; end
                `LOOM_RDLR:  begin wr_en = 1'b1; wr_val = {8'd0, lr[tid]}; end
                `LOOM_JMPR:  pc_next = A[7:0];
                default: ;
            endcase
        end
        `LOOM_MAJ_MISC: begin
            case (ir[11:8])
                `LOOM_NOP:   ;
                `LOOM_HALT:  do_halt = 1'b1;
                `LOOM_RET:   pc_next = lr[tid];
                `LOOM_WAITT: begin done = T; do_clrt = T; end
                `LOOM_DELAY: begin
                    if (delay[tid] == 8'd0) begin
                        if (imm8 == 8'd0) done = 1'b1;
                        else begin done = 1'b0; delay_load = 1'b1; end
                    end else begin
                        delay_dec = 1'b1;
                        done = (delay[tid] == 8'd1);
                    end
                end
                `LOOM_SETC:  begin c_we = 1'b1; c_val = 1'b1; end
                `LOOM_CLC:   begin c_we = 1'b1; c_val = 1'b0; end
                `LOOM_START: do_start = 1'b1;
                `LOOM_STOP:  do_stop = 1'b1;
                `LOOM_CLRT:  do_clrt = 1'b1;
                default: ;
            endcase
        end
        default: ;
        endcase

        // nothing leaves the core unless the thread really executes this cycle
        if (!exec) begin
            pin_valid = 1'b0; inbox_pop = 2'b00; outbox_push = 2'b00;
        end
    end

    // ---- state update ------------------------------------------------------
    wire commit = exec & done;
    integer t;

    always @(posedge clk) begin
        if (!rst_n) begin
            cyc <= 16'd0;
            running <= 2'b00; halted <= 2'b00; blocked <= 2'b00;
            for (t = 0; t < 2; t = t + 1) begin
                pc[t] <= 8'd0; lr[t] <= 8'd0; fz[t] <= 1'b0; fc[t] <= 1'b0;
                period[t] <= 16'd0; count[t] <= 16'd0; tick[t] <= 1'b0; delay[t] <= 8'd0;
            end
            for (t = 0; t < 16; t = t + 1) regs[t] <= 16'd0;
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

            // timers (both threads, every cycle)
            for (t = 0; t < 2; t = t + 1) begin
                if (exec && do_sett && (tid == t[0])) begin
                    period[t] <= A;
                    count[t]  <= (A == 16'd0) ? 16'd0 : A - 16'd1;
                    tick[t]   <= 1'b0;
                end else begin
                    if (period[t] != 16'd0)
                        count[t] <= (count[t] == 16'd0) ? period[t] - 16'd1 : count[t] - 16'd1;
                    if (period[t] != 16'd0 && count[t] == 16'd0)
                        tick[t] <= 1'b1;
                    else if (exec && do_clrt && (tid == t[0]))
                        tick[t] <= 1'b0;
                end
            end

            // run / halt, core-initiated first, host last
            if (exec && do_halt)  begin running[tid]  <= 1'b0; halted[tid] <= 1'b1; end
            if (exec && do_start) begin running[otid] <= 1'b1; halted[otid] <= 1'b0; end
            if (exec && do_stop)  begin running[otid] <= 1'b0; delay[otid] <= 8'd0; end
            if (host_run_we) begin
                for (t = 0; t < 2; t = t + 1) begin
                    if (host_run_val[t]) begin
                        running[t] <= 1'b1; halted[t] <= 1'b0;
                    end else begin
                        running[t] <= 1'b0; delay[t] <= 8'd0;
                    end
                end
            end
            for (t = 0; t < 2; t = t + 1) begin
                if (host_pc_we[t] && !running[t]) pc[t] <= host_pc_val;
                if (host_rst[t]) begin
                    lr[t] <= 8'd0; fz[t] <= 1'b0; fc[t] <= 1'b0;
                    period[t] <= 16'd0; count[t] <= 16'd0; tick[t] <= 1'b0; delay[t] <= 8'd0;
                end
            end
        end
    end

    assign dbg_retire = commit;
    assign dbg_tid    = tid;
    assign dbg_pc     = pc_cur;
    assign dbg_ir     = ir;
endmodule
