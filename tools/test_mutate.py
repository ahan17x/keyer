"""Tests of the mutation tool itself: the generator on a small module, and
controls for the Yosys equivalence step (a checker that called a real fault
"equivalent" would hide a survivor; docs/BUGS.md row 10 is the precedent).
Run: python3 -m pytest tools/test_mutate.py -q
"""

import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mutate as M  # noqa: E402

SNIPPET = """`default_nettype none
module snip (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] a,
    output reg  [7:0] q
);
    // a comment with a + b and 1'b1 in it
    wire       go_en = a[0] & ~a[1];
    wire [7:0] sum   = a + 8'd3;
    reg  [7:0] mem [0:3];
    integer i;
    always @(posedge clk) begin
        if (!rst_n) q <= 8'd0;
        else if (go_en) q <= (a < 8'd9) ? sum : q;
        for (i = 0; i < 4; i = i + 1) mem[i] <= mem[i] | q;
    end
`ifdef FORMAL
    always @(*) assert(q != 8'd255);
`endif
`ifndef SYNTHESIS
    initial q = 8'd1;
`endif
`ifdef KEYER_IMEM_FLOPS
    wire spare = a[2] ^ a[3];
`endif
endmodule
"""


def snippet_mutants(tmp_path, text=SNIPPET):
    p = tmp_path / "snip.v"
    p.write_text(text)
    return M.mutants_of(str(p), {})


def test_operators_on_a_small_module(tmp_path):
    ms = snippet_mutants(tmp_path)
    got = {(m["line"], m["op"], m["before"].strip(), m["after"]) for m in ms}
    # operator swaps, a removed negation, constants, on the two wire initialisers
    assert (9, "op", "&", "|") in got and (9, "neg", "~", "") in got
    assert (10, "op", "+", "-") in got and (10, "const", "8'd3", "8'd2") in got
    # stuck-at on the one-bit enable, not on the data bus
    assert (9, "stuck", "a[0] & ~a[1]", "1'b0") in got and (9, "stuck", "a[0] & ~a[1]", "1'b1") in got
    assert not any(m["line"] == 10 and m["op"] == "stuck" for m in ms)
    # the reset branch: negation removed, condition inverted, stuck both ways, reset value
    assert {(14, "neg", "!", ""), (14, "cond", "!rst_n", "!(!rst_n)"), (14, "stuck", "!rst_n", "1'b0"),
            (14, "stuck", "!rst_n", "1'b1"), (14, "const", "8'd0", "8'd1")} <= got
    # the enable, the relational operator (two replacements) and the ternary
    assert (15, "stuck", "go_en", "1'b0") in got and (15, "op", "<", "<=") in got and (15, "op", "<", ">=") in got
    assert (15, "cond", "(a < 8'd9)", "!((a < 8'd9))") in got
    # a `for` header is structure; its body is logic
    assert [(m["op"], m["before"].strip()) for m in ms if m["line"] == 16] == [("op", "|")]


def test_what_is_left_alone(tmp_path):
    ms = snippet_mutants(tmp_path)
    lines = {m["line"] for m in ms}
    assert not lines & {1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13}        # header, ports, comment, declarations
    assert not lines & {18, 19, 20, 21, 22, 23}                   # FORMAL and simulation-only blocks
    assert all("<=" not in (m["before"], m["after"]) or m["line"] == 15 for m in ms)   # assignments are not relational
    assert all(m["define"] == ("KEYER_IMEM_FLOPS" if m["line"] == 25 else None) for m in ms)
    assert {m["op"] for m in ms if m["line"] == 25} == {"op", "stuck"}
    # nothing inside an index
    assert not any("[" in m["text"] and m["text"].count("[") != SNIPPET.split("\n")[m["line"] - 1].count("[") for m in ms)


def test_each_mutant_changes_one_line_and_ids_are_stable(tmp_path):
    ms = snippet_mutants(tmp_path)
    src = SNIPPET.split("\n")
    assert len({m["id"] for m in ms}) == len(ms)
    for m in ms:
        assert m["text"] != src[m["line"] - 1]
    # the same faults keep their ids when lines are inserted above them
    shifted = snippet_mutants(tmp_path, SNIPPET.replace("    // a comment", "    wire unrelated;\n\n    // a comment"))
    assert {m["id"] for m in shifted} == {m["id"] for m in ms}
    assert {m["line"] for m in shifted} != {m["line"] for m in ms}


def test_literal_flip():
    assert M.flip_literal("1'b0") == "1'b1" and M.flip_literal("1'b1") == "1'b0"
    assert M.flip_literal("16'hFFFF") == "16'hFFFE" and M.flip_literal("8'h4B") == "8'h4A"
    assert M.flip_literal("12'd4095") == "12'd4094" and M.flip_literal("3'd0") == "3'd1"
    assert M.flip_literal("4'bxx01") is None


def test_the_real_sources():
    ms = M.all_mutants(M.DEFAULT_FILES)                 # asserts that the ids are unique
    assert len(ms) > 1000
    by_file = {}
    for m in ms:
        by_file.setdefault(m["file"], 0)
        by_file[m["file"]] += 1
    assert set(by_file) == set(M.DEFAULT_FILES)
    for m in ms:
        raw = open(os.path.join(M.SRC, m["file"])).read().split("\n")[m["line"] - 1]
        assert m["text"] != raw and len(m["text"].split("\n")) == 1
    # the flop memory is tested with its define, the macro instantiation without
    imem = [m for m in ms if m["file"] == "keyer_imem.v"]
    assert {m["define"] for m in imem} == {None, "KEYER_IMEM_FLOPS"}


@pytest.mark.skipif(shutil.which("yosys") is None, reason="yosys not installed")
def test_equivalence_step_controls(tmp_path, monkeypatch):
    """Real faults must not be proven equivalent; rewrites that change
    nothing must be, at the module level and (for a fault on an output the
    top does not connect) at the top level; the macro branch is never judged."""
    monkeypatch.setattr(M, "WORK", str(tmp_path))

    def variant(needle, replacement, file="keyer_core.v", define=None):
        src = open(os.path.join(M.SRC, file)).read().split("\n")
        n = [i for i, l in enumerate(src) if needle in l]
        assert len(n) == 1, (needle, n)
        return dict(id="ctl", file=file, line=n[0] + 1, define=define, text=src[n[0]].replace(needle, replacement))

    def verdict(mt):
        work = os.path.join(str(tmp_path), "w")
        M.prepare(work, mt)
        return M.yosys_equiv(work, mt)

    assert verdict(variant("wire exec_io = |x_io;", "wire exec_io = &x_io;")) is None
    assert verdict(variant("assign fetch_addr = pc_oth;", "assign fetch_addr = pc_oth | 8'd0;")) == "module"
    assert verdict(variant("assign dbg_retire = commit;", "assign dbg_retire = ~commit;")) == "top"
    assert verdict(variant(".A_DLY      (1'b1),", ".A_DLY      (1'b0),", file="keyer_imem.v")) is None


@pytest.mark.skipif(shutil.which("yosys") is None or shutil.which("yosys-abc") is None, reason="yosys or yosys-abc not installed")
def test_reset_miter_controls(tmp_path, monkeypatch):
    """The reset-sequence miter (prove-equivalents): a fault visible at a pad
    from the first cycle after reset gets a counterexample, a change on an
    output the top does not connect is proved, and the macro instantiation
    is skipped. Both runs take seconds (the proof of a real documented
    equivalent can take minutes and is not a unit test)."""
    monkeypatch.setattr(M, "WORK", str(tmp_path))

    def variant(needle, replacement, file="keyer_core.v", define=None):
        src = open(os.path.join(M.SRC, file)).read().split("\n")
        n = [i for i, l in enumerate(src) if needle in l]
        assert len(n) == 1, (needle, n)
        return dict(id="ctl", file=file, line=n[0] + 1, define=define, text=src[n[0]].replace(needle, replacement))

    def verdict(mt, timeout=600):
        work = os.path.join(str(tmp_path), "w")
        M.prepare(work, mt)
        return M.reset_miter(work, mt, timeout)

    v, detail = verdict(variant("tx_shift <= 8'd0; miso <= 1'b0;", "tx_shift <= 8'd0; miso <= 1'b1;", file="keyer_host.v"))
    assert v == "cex", (v, detail)                     # MISO high after reset (SEMANTICS 10.1)
    v, detail = verdict(variant("assign dbg_retire = commit;", "assign dbg_retire = ~commit;"))
    assert v == "proved", (v, detail)
    v, detail = verdict(variant(".A_DLY      (1'b1),", ".A_DLY      (1'b0),", file="keyer_imem.v"))
    assert v == "skipped", (v, detail)

