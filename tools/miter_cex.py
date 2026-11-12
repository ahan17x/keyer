#!/usr/bin/env python3
"""Replay a counterexample of the reset-sequence miter (tools/mutate.py
prove-equivalents) and show what it is.

    python3 tools/miter_cex.py test/sim_build/mutation/prove_<id> [-n CYCLES]

A counterexample from abc is a claim about a reduced circuit. Before it is
read as "the mutant is not equivalent" it is checked twice here, and then it
has to be judged by a person against docs/SEMANTICS.md:

1. on the miter itself: the input sequence is simulated on the AIGER file
   Yosys wrote (miter.aig, before any abc transformation) by the small
   simulator below; a bad-state output must be 1 in some frame with every
   constraint (the wrapper's assumptions) 1 in that frame and all before;
2. on the RTL: the same pad sequence drives src/ and the mutant's sources
   under Icarus, with the program memory replaced by the free read port the
   miter uses, and the first cycle is reported in which a pad or the
   memory port differs (the wrapper's own comparison: uo_out, uio_out,
   uio_oe, the memory's write enable and address, and the write data while
   the enable is high, from the release of reset on).

Then the host traffic is printed: every edge of rst_n and CS_n and the bits
MOSI carried on each rising SCK edge, as bytes. That is what is compared
with the contract. The miter knows SEMANTICS 10.1's pad timing and nothing
else: a trace that reads the program memory while a thread runs (10.4:
unspecified bytes) is a counterexample of the miter and refutes nothing
(docs/BUGS.md 58), while a trace inside the contract in which src/ itself
is wrong is a finding about the design (BUGS 56).

Registers without a reset start at 0 in the miter and at x under Icarus; an
x in a compared value is printed and counts as a difference.
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
FILES = ["keyer_fifo.v", "keyer_pins.v", "keyer_core.v", "keyer_host.v", "keyer_capture.v", "keyer_ser.v",
         "tt_um_ahan17x_keyer.v"]
N_IN = 35       # clk, rst_n, ui_in[0..7], uio_in[0..7], ena, rd[0..15]: the order of miter.aim


def read_frames(cex):
    """The input vectors of a `write_cex -a` file, one string per frame."""
    return [t for t in open(cex).read().split("\n") if len(t) == N_IN and set(t) <= set("01")]


# ---------------------------------------------------------------- the miter, simulated

def load_aig(path):
    data = open(path, "rb").read()
    nl = data.index(b"\n")
    hdr = [int(x) for x in data[:nl].split()[1:]]
    I, L, O, A = hdr[1:5]
    B = hdr[5] if len(hdr) > 5 else 0
    C = hdr[6] if len(hdr) > 6 else 0
    pos, lines = nl + 1, []
    for _ in range(L + O + B + C):
        e = data.index(b"\n", pos)
        lines.append(data[pos:e].split())
        pos = e + 1
    if any(len(l) > 1 and l[1] != b"0" for l in lines[:L]):
        raise ValueError("a latch does not start at 0: the miter is written with -zinit")
    nxt = [int(l[0]) for l in lines[:L]]
    bad = [int(l[0]) for l in lines[L + O:L + O + B]]
    con = [int(l[0]) for l in lines[L + O + B:]]
    ands = []
    for i in range(A):
        d = []
        for _ in range(2):
            x = s = 0
            while True:
                b = data[pos]
                pos += 1
                x |= (b & 0x7F) << s
                s += 7
                if not b & 0x80:
                    break
            d.append(x)
        r0 = 2 * (I + L + i + 1) - d[0]
        ands.append((r0, r0 - d[1]))
    return I, L, nxt, bad, con, ands


def aig_replay(aig, frames):
    """(first frame with a bad output while every constraint has held, that
    output's index, first frame in which a constraint fails), each None if
    there is none."""
    I, L, nxt, bad, con, ands = load_aig(aig)
    assert I == N_IN, I
    v = [0] * (1 + I + L + len(ands))
    lat = [0] * L
    base = 1 + I + L
    lit = lambda x: v[x >> 1] ^ (x & 1)
    broken = None
    for t, fr in enumerate(frames):
        v[1:1 + I] = [int(c) for c in fr]
        v[1 + I:base] = lat
        for k, (a, b) in enumerate(ands):
            v[base + k] = (v[a >> 1] ^ (a & 1)) & (v[b >> 1] ^ (b & 1))
        if broken is None and not all(lit(c) for c in con):
            broken = t
        hit = [i for i, b in enumerate(bad) if lit(b)]
        if hit and broken is None:
            return t, hit[0], None
        lat = [lit(n) for n in nxt]
    return None, None, broken


# ---------------------------------------------------------------- the RTL, simulated

BENCH = """`timescale 1ns/1ps
`default_nettype none
module keyer_imem(input wire clk, input wire we, input wire [7:0] addr, input wire [15:0] wdata,
                  output wire [15:0] rdata);
    assign rdata = tb.rd;
    wire _unused = &{clk, we, addr, wdata};
endmodule
module tb;
    reg clk = 0, rst_n = 0, ena = 1;
    reg [7:0] ui_in = 0, uio_in = 0;
    reg [15:0] rd = 0;
    wire [7:0] uo_out, uio_out, uio_oe;
    tt_um_ahan17x_keyer dut(.ui_in(ui_in), .uo_out(uo_out), .uio_in(uio_in), .uio_out(uio_out),
                            .uio_oe(uio_oe), .ena(ena), .clk(clk), .rst_n(rst_n));
    task step(input r, input [7:0] ui, input [7:0] uio, input e, input [15:0] d);
        begin
            rst_n = r; ui_in = ui; uio_in = uio; ena = e; rd = d; #4;
            $display("F %b %b %b %b %h %h", uo_out, uio_out, uio_oe, dut.u_imem.we, dut.u_imem.addr, dut.u_imem.wdata);
            #1 clk = 1; #5 clk = 0;
        end
    endtask
    initial begin
@STEPS@        $finish;
    end
endmodule
"""


def _fields(fr):
    b = lambda s: int(s[::-1], 2)
    return dict(rst_n=int(fr[1]), ui=b(fr[2:10]), uio=b(fr[10:18]), ena=int(fr[18]), rd=b(fr[19:35]))


def rtl_run(src, frames, out, name, define=None):
    """The compared values of one design, one tuple per frame."""
    steps = "".join("        step(1'b%d, 8'd%d, 8'd%d, 1'b%d, 16'd%d);\n" % (f["rst_n"], f["ui"], f["uio"], f["ena"], f["rd"])
                    for f in map(_fields, frames))
    tb = os.path.join(out, "cex_tb.v")
    open(tb, "w").write(BENCH.replace("@STEPS@", steps))
    vvp = os.path.join(out, "cex_%s.vvp" % name)
    cmd = ["iverilog", "-g2005", "-I", src, "-o", vvp] + (["-D" + define] if define else []) + [tb] + \
          [os.path.join(src, f) for f in FILES]
    subprocess.run(cmd, check=True, cwd=out)
    res = subprocess.run(["vvp", "-n", vvp], check=True, cwd=out, capture_output=True, text=True).stdout
    return [tuple(l.split()[1:]) for l in res.split("\n") if l.startswith("F ")]


def compared(row):
    """What the wrapper asserts on: the write data only with the write enable."""
    uo, uio_out, uio_oe, we, addr, wd = row
    return (uo, uio_out, uio_oe, we, addr, wd if we != "0" else "-")


def first_difference(frames, gold, mut):
    live, prev = False, 1
    for t, fr in enumerate(frames):
        r = int(fr[1])
        release = r and not prev
        if (live or release) and compared(gold[t]) != compared(mut[t]):
            return t
        live, prev = live or release, r
    return None


def spi_events(frames):
    """Edges of rst_n and CS_n, and the bytes MOSI carried while CS_n was low."""
    ev, bits, prev = [], [], None
    flush = lambda why: bits and ev.append("      MOSI %s%s" % (" ".join("".join(bits[i:i + 8]) for i in range(0, len(bits), 8)), why))
    for t, fr in enumerate(frames):
        f = _fields(fr)
        cur = (f["rst_n"], (f["ui"] >> 2) & 1, f["ui"] & 1)
        if prev is not None:
            if prev[0] != cur[0]:
                ev.append("%5d rst_n %s" % (t, "rises" if cur[0] else "falls"))
            if prev[1] != cur[1]:
                flush("")
                bits = []
                ev.append("%5d CS_n %s%s" % (t, "rises" if cur[1] else "falls", " (SCK is high)" if cur[2] else ""))
            if not prev[2] and cur[2] and not cur[1]:
                bits.append(str((f["ui"] >> 1) & 1))
                if len(bits) % 8 == 0:
                    ev.append("%5d   eighth SCK rise of byte %d" % (t, len(bits) // 8 - 1))
        prev = cur
    flush(" (CS_n still low at the end)")
    return ev


def replay(work, define=None):
    """Replay work/reset_miter/cex.txt. Returns a dict: frames, aig_frame,
    aig_output, constraint_broken, first_difference, gold, mut, events."""
    work = os.path.abspath(work)
    out = os.path.join(work, "reset_miter")
    frames = read_frames(os.path.join(out, "cex.txt"))
    aig_frame, aig_output, broken = aig_replay(os.path.join(out, "miter.aig"), frames)
    gold = rtl_run(SRC, frames, out, "gold", define)
    mut = rtl_run(os.path.join(work, "src"), frames, out, "mut", define)
    return dict(frames=frames, aig_frame=aig_frame, aig_output=aig_output, constraint_broken=broken,
                first_difference=first_difference(frames, gold, mut), gold=gold, mut=mut, events=spi_events(frames))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("work", help="the mutant's work directory (it holds src/ and reset_miter/)")
    ap.add_argument("-n", type=int, default=3, help="cycles to print before the first difference")
    ap.add_argument("-D", dest="define", help="a define the mutant was built with")
    a = ap.parse_args()
    r = replay(a.work, a.define)
    print("%d frames" % len(r["frames"]))
    if r["aig_frame"] is None:
        print("on the miter: NOT reproduced (no bad output with the constraints held%s)"
              % ("" if r["constraint_broken"] is None else "; a constraint fails in frame %d" % r["constraint_broken"]))
    else:
        print("on the miter: assertion %d fails in frame %d, every assumption held" % (r["aig_output"], r["aig_frame"]))
    print("host traffic (frame, event):")
    print("\n".join(r["events"]))
    d = r["first_difference"]
    if d is None:
        print("on the RTL: no difference at the pads or the memory port")
        return 1
    print("on the RTL: first difference in frame %d   (uo_out uio_out uio_oe we addr wdata)" % d)
    for t in range(max(0, d - a.n), d + 1):
        f = _fields(r["frames"][t])
        print("%5d rst_n=%d sck=%d mosi=%d csn=%d ui=%02x uio=%02x rd=%04x" %
              (t, f["rst_n"], f["ui"] & 1, (f["ui"] >> 1) & 1, (f["ui"] >> 2) & 1, f["ui"], f["uio"], f["rd"]))
        print("        design %s" % " ".join(r["gold"][t]))
        print("        mutant %s" % " ".join(r["mut"][t]))
    return 0 if r["aig_frame"] is not None else 1


if __name__ == "__main__":
    sys.exit(main())
