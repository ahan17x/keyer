#!/usr/bin/env python3
"""Mutation testing of the Keyer RTL.

    python3 tools/mutate.py list   [FILE ...]                 # the mutants, one per line
    python3 tools/mutate.py run    [FILE ...] [-j N] [--only ID,...] [--rerun STATUS,...]
    python3 tools/mutate.py report                            # refresh docs/mutation/summary.md

Results: docs/mutation/results.tsv, one row per mutant (it is also the store a
later run resumes from: only mutants without a row are run unless --rerun
says otherwise), and docs/mutation/summary.md.

A mutant is the design with one single-line fault. For each one the tool
compiles the design (a mutant that does not compile is `invalid` and leaves
the count), runs the cocotb suite (test/) until the first failing test, and
runs the formal proof of the mutated module if it has one (formal/). A mutant
is `killed` if a cocotb test fails or a proof fails; otherwise it is checked
against the original with Yosys: if every output and every register input is
proven equivalent it is `equivalent`, as it is when tools/mutate_equivalents.md
lists it with a reason; what is left has `survived`. The exit status of `run`
is non-zero while any mutant survives.

Mutation operators (all confined to one source line):
  op      operator swap: + -, & |, ^ -> |, && ||, == !=, the relational
          operators among themselves, << >>
  neg     a negation removed: ~x -> x, !x -> x
  const   a sized literal with its lowest bit flipped (1'b0 <-> 1'b1, 8'd16 -> 8'd17)
  cond    a condition inverted: if (c) -> if (!(c)), c ? a : b -> !(c) ? a : b
  stuck   stuck-at-0 and stuck-at-1 on every `if` condition (this covers every
          reset branch and every register enable), on every single-line
          assignment to a one-bit or enable-like signal, and on every
          enable-like input port connection of an instance

Not mutated: comments, `ifdef FORMAL blocks (the properties, not the design),
`ifndef SYNTHESIS blocks (simulation-only initialisation), declarations up
to their `=`, index and range expressions inside [ ], `for` headers and
generate loops (elaboration structure), and the port-only macro stub src/RM_IHPSG13_1P_256x16_c2_bm_bist.v (simulation
uses the vendored model, so nothing there is exercised). Code under
`ifdef KEYER_IMEM_FLOPS is mutated and tested with that define set.

IDs are stable across edits that do not touch the mutated line: a hash of the
file name, the text of the line, which copy of that text it is in the file,
the operator and the replacement.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
OUT = os.path.join(ROOT, "docs", "mutation")
WORK = os.path.join(ROOT, "test", "sim_build", "mutation")
EQUIV_DOC = os.path.join(ROOT, "tools", "mutate_equivalents.md")
STUB = "RM_IHPSG13_1P_256x16_c2_bm_bist.v"
DEFAULT_FILES = ["keyer_isa.vh", "keyer_fifo.v", "keyer_imem.v", "keyer_pins.v", "keyer_core.v",
                 "keyer_host.v", "keyer_capture.v", "tt_um_ahan17x_keyer.v"]
DESIGN = ["keyer_fifo.v", "keyer_imem.v", "keyer_pins.v", "keyer_core.v", "keyer_host.v",
          "keyer_capture.v", "tt_um_ahan17x_keyer.v"]
TOP = "tt_um_ahan17x_keyer"

# The proofs that read each file (a header mutant reaches the core's proof).
FORMAL = {
    "keyer_fifo.v": "fifo", "keyer_pins.v": "pins", "keyer_core.v": "core",
    "keyer_capture.v": "capture", "keyer_isa.vh": "core",
}

# cocotb tests run first because they are short and kill most mutants.
QUICK = ("test_id_and_registers|test_capture_registers|test_fifo_roundtrip_and_status"
         "|test_lockstep_alu_and_branches|test_lockstep_pins_timer_delay"
         "|test_lockstep_replay_host_waveform_and_loopback_capture|test_lockstep_random_programs")

# Signals wider than one bit that are still enables, strobes, selects or
# resets (one-bit signals all get the stuck-at mutants).
REST = "^(?!.*(%s))" % QUICK            # every other test

ENABLE_NAME = re.compile(
    r"(^|_)(we|re|en|ok|valid|push|pop|rst|rst_n|clear|clr|sel|hot|pre|pulse|busy|allowed)(\d*)($|_)"
    r"|^(x|c|xo|run)_")

TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<star>@\s*\(\s*\*\s*\))
  | (?P<attr>\(\*.*?\*\))
  | (?P<lit>\d*'[sS]?[bBdDhHoO][0-9a-fA-F_xXzZ?]+)
  | (?P<num>\d[\d_]*)
  | (?P<macro>`[A-Za-z_][A-Za-z0-9_]*)
  | (?P<id>[$A-Za-z_][A-Za-z0-9_$]*)
  | (?P<str>"(?:\\.|[^"\\])*")
  | (?P<op><<<|>>>|===|!==|==|!=|<=|>=|&&|\|\||<<|>>|\+:|-:|~&|~\||~\^|\^~|[-+*/%&|^~!<>=?:;,.(){}\[\]@\#])
""", re.X)

DECL = {"input", "output", "inout", "wire", "reg", "integer", "genvar", "parameter", "localparam"}
OPERAND_END = {"id", "num", "lit", "macro"}
SWAPS = {
    "+": ["-"], "-": ["+"], "&": ["|"], "|": ["&"], "^": ["|"], "&&": ["||"], "||": ["&&"],
    "==": ["!="], "!=": ["=="], "<": ["<=", ">="], ">": [">=", "<="], ">=": [">", "<"],
    "<<": [">>"], ">>": ["<<"],
}


class Tok:
    __slots__ = ("kind", "text", "start", "end", "depth", "index")

    def __init__(self, kind, text, start, end, depth, index):
        self.kind, self.text, self.start, self.end, self.depth, self.index = kind, text, start, end, depth, index


def tokenize(line):
    """Tokens with their nesting depth; `index` marks tokens inside [ ]."""
    toks, pos, stack = [], 0, []
    while pos < len(line):
        m = TOKEN.match(line, pos)
        if not m:
            pos += 1
            continue
        kind = m.lastgroup
        text = m.group()
        if kind != "ws":
            if text in ")]}" and stack:
                stack.pop()
            toks.append(Tok(kind, text, m.start(), m.end(), len(stack), "[" in stack))
            if text in "([{":
                stack.append(text)
        pos = m.end()
    return toks


def blank_comments(lines):
    """Comments replaced by spaces, columns preserved."""
    out, in_block = [], False
    for line in lines:
        res, i = [], 0
        while i < len(line):
            if in_block:
                j = line.find("*/", i)
                if j < 0:
                    res.append(" " * (len(line) - i))
                    i = len(line)
                else:
                    res.append(" " * (j + 2 - i))
                    i, in_block = j + 2, False
            elif line.startswith("//", i):
                res.append(" " * (len(line) - i))
                i = len(line)
            elif line.startswith("/*", i):
                in_block = True
                res.append("  ")
                i += 2
            else:
                res.append(line[i])
                i += 1
        out.append("".join(res))
    return out


def regions(lines):
    """Per line: (skip, define) from the `ifdef structure."""
    stack, out = [], []
    for line in lines:
        s = line.strip()
        m = re.match(r"`(ifdef|ifndef)\s+(\w+)", s)
        if m:
            stack.append([m.group(1) == "ifdef", m.group(2)])
            out.append((True, None))
            continue
        if s.startswith("`else"):
            stack[-1][0] = not stack[-1][0]
            out.append((True, None))
            continue
        if s.startswith("`endif"):
            stack.pop()
            out.append((True, None))
            continue
        skip, define = False, None
        for positive, name in stack:
            if name == "FORMAL" and positive:
                skip = True
            elif name == "SYNTHESIS" and not positive:
                skip = True
            elif name == "KEYER_IMEM_FLOPS" and positive:
                define = name
        out.append((skip, define))
    return out


def flip_literal(text):
    m = re.match(r"(\d*)'([sS]?)([bBdDhHoO])([0-9a-fA-F_]+)$", text)
    if not m:
        return None
    width, sign, base, digits = m.groups()
    radix = {"b": 2, "d": 10, "h": 16, "o": 8}[base.lower()]
    value = int(digits.replace("_", ""), radix) ^ 1
    fmt = {2: "{:b}", 10: "{:d}", 16: "{:X}", 8: "{:o}"}[radix].format(value)
    if radix != 10:
        fmt = fmt.rjust(len(digits.replace("_", "")), "0")
    return "%s'%s%s%s" % (width, sign, base, fmt)


def port_table(files):
    """module -> {port: (direction, range text or None)} from the module headers."""
    table = {}
    for path in files:
        lines = blank_comments(open(path).read().split("\n"))
        mod = None
        for line in lines:
            m = re.match(r"\s*module\s+(\w+)", line)
            if m:
                mod = m.group(1)
                table[mod] = {}
                continue
            if mod is None:
                continue
            m = re.match(r"\s*(input|output|inout)\s+(?:wire|reg)?\s*(\[[^\]]+\])?\s*(\w+)\s*,?\s*$", line)
            if m:
                table[mod][m.group(3)] = (m.group(1), m.group(2))
            if re.match(r"\s*\);", line):
                mod = None
    return table


def declared_width(lines, name):
    """Range text of a signal declared in this file: '' for one bit, None if not found."""
    pat = re.compile(r"\b(?:input|output|inout|wire|reg)\b[^;=]*?(\[[^\]]+\])?\s*(?:[\w\s,]*,\s*)?\b%s\b" % re.escape(name))
    for line in lines:
        m = pat.search(line)
        if m and re.search(r"\b(input|output|inout|wire|reg)\b", line):
            first = re.search(r"\b(?:input|output|inout|wire|reg)\b\s*(?:wire|reg)?\s*(\[[^\]]+\])?", line)
            return first.group(1) or ""
    return None


def const_bits(rng, bit):
    """A constant of the declared width with every bit = bit."""
    if not rng:
        return "1'b%d" % bit
    hi, lo = rng[1:-1].split(":")
    return "{((%s)-(%s)+1){1'b%d}}" % (hi.strip(), lo.strip(), bit)


def mutants_of(path, ports):
    name = os.path.basename(path)
    raw = open(path).read().split("\n")
    code = blank_comments(raw)
    reg = regions(code)
    seen_text = {}
    out = []

    def add(lineno, start, end, repl, op, define):
        line = raw[lineno]
        key = line.strip()
        copy = seen_text.setdefault((lineno, key), sum(1 for i in range(lineno) if raw[i].strip() == key))
        h = hashlib.sha1(("%s|%s|%d|%s|%d|%s" % (name, key, copy, op, start - (len(line) - len(line.lstrip())), repl)).encode()).hexdigest()[:8]
        stem = name.replace("keyer_", "").replace("tt_um_ahan17x_keyer", "top").split(".")[0]
        out.append(dict(id="%s-%s" % (stem, h), file=name, line=lineno + 1, col=start + 1, op=op,
                        before=line[start:end], after=repl, define=define,
                        text=line[:start] + repl + line[end:]))

    instance_mod = None
    for n, line in enumerate(code):
        skip, define = reg[n]
        if skip or not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("`"):
            if not stripped.startswith("`define"):
                continue
        toks = [t for t in tokenize(line) if t.kind != "attr"]
        if not toks:
            continue
        first = toks[0].text
        # which instance are we inside (for port-connection mutants)
        m = re.match(r"\s*(\w+)\s+(?:#\s*\(.*\)\s*)?(\w+)\s*\(\s*$", line)
        if m and m.group(1) in ports:
            instance_mod = m.group(1)
        if first in ("module", "endmodule", "generate", "endgenerate", "genvar", "always", "for",
                     "function", "endfunction") and first != "always":
            if first == "for":
                # the header is elaboration structure; the body on the same line is logic
                close = next((t for t in toks if t.text == ")" and t.depth == 0), None)
                toks = [t for t in toks if close and t.start > close.start]
                if not toks:
                    continue
            else:
                continue
        if first == "always":
            toks = [t for t in toks[1:] if t.kind not in ("star",)]
            # drop the event control @( ... )
            if toks and toks[0].text == "@":
                close = next((t for t in toks[1:] if t.text == ")" and t.depth == 0), None)
                toks = [t for t in toks if close and t.start > close.start]
            if not toks:
                continue
        # a declaration is left alone up to its initialiser
        if first in DECL:
            eq = next((t for t in toks if t.text == "=" and t.depth == 0), None)
            if eq is None:
                continue
            toks = [t for t in toks if t.start > eq.start]
        if first == "`define":
            toks = toks[2:]

        # ---- token-level operators
        for i, t in enumerate(toks):
            prev = toks[i - 1] if i else None
            binary = prev is not None and (prev.kind in OPERAND_END or prev.text in ")]}")
            if t.index:
                continue                                  # index and range expressions are structure
            if t.kind == "lit":
                repl = flip_literal(t.text)
                if repl:
                    add(n, t.start, t.end, repl, "const", define)
            elif t.kind == "op":
                if t.text in ("+", "-") and not binary:
                    continue
                if t.text == "<" and not binary:
                    continue
                if t.text in SWAPS:
                    for repl in SWAPS[t.text]:
                        add(n, t.start, t.end, repl, "op", define)
                elif t.text == "<=" and t.depth > 0 and binary:
                    # relational only: inside parentheses (a non-blocking assignment is at depth 0)
                    for repl in ("<", ">"):
                        add(n, t.start, t.end, repl, "op", define)
                elif t.text in ("~", "!") and not binary:
                    add(n, t.start, t.end, "", "neg", define)

        # ---- if conditions: inverted, stuck at 0, stuck at 1
        for i, t in enumerate(toks):
            if t.kind == "id" and t.text == "if" and i + 1 < len(toks) and toks[i + 1].text == "(":
                op = toks[i + 1]
                close = next((u for u in toks[i + 2:] if u.text == ")" and u.depth == op.depth), None)
                if close is None:
                    continue
                cond = line[op.end:close.start]
                add(n, op.end, close.start, "!(%s)" % cond, "cond", define)
                add(n, op.end, close.start, "1'b0", "stuck", define)
                add(n, op.end, close.start, "1'b1", "stuck", define)

        # ---- ternary conditions inverted
        for i, t in enumerate(toks):
            if t.text == "?" and t.kind == "op":
                j = i - 1
                while j >= 0:
                    u = toks[j]
                    if u.depth < t.depth or (u.depth == t.depth and u.text in ("=", "<=", ",", "?", ":", "{")):
                        break
                    j -= 1
                cond_toks = toks[j + 1:i]
                if not cond_toks:
                    continue
                a, b = cond_toks[0].start, cond_toks[-1].end
                add(n, a, b, "!(%s)" % line[a:b], "cond", define)

        # ---- stuck-at on single-line assignments to one-bit or enable-like signals
        m = re.match(r"(\s*(?:assign\s+|wire\s+(?:\[[^\]]+\]\s*)?)?)(\w+)(\s*(?:<=|=)\s*)([^;]+);\s*$", line)
        if m and m.group(2) not in DECL and not re.match(r"\s*(parameter|localparam|reg|integer)\b", line):
            target, rhs = m.group(2), m.group(4).strip()
            pure_const = re.fullmatch(r"\{?[\w']*'[bdhoBDHO][0-9a-fA-F_]+\}?|\{\w+\{1'b[01]\}\}", rhs) is not None
            rng = declared_width(code, target)
            if rng is not None and not pure_const and (rng == "" or ENABLE_NAME.search(target)):
                a = m.start(4)
                b = a + len(m.group(4).rstrip())
                for bit in (0, 1):
                    add(n, a, b, const_bits(rng, bit), "stuck", define)

        # ---- stuck-at on enable-like input ports of an instance
        if instance_mod:
            for pm in re.finditer(r"\.(\w+)\s*\(", line):
                port = pm.group(1)
                info = ports.get(instance_mod, {}).get(port)
                if not info or info[0] != "input" or not ENABLE_NAME.search(port):
                    continue
                depth, k = 1, pm.end()
                while k < len(line) and depth:
                    depth += {"(": 1, ")": -1}.get(line[k], 0)
                    k += 1
                expr = line[pm.end():k - 1]
                if re.fullmatch(r"\s*[\w']*'[bdhoBDHO][0-9a-fA-F_]+\s*", expr):
                    continue                              # already a constant: the const operator has it
                rng = info[1]
                if rng and not re.fullmatch(r"\[\d+:\d+\]", rng):
                    continue                              # parametric width: not expressible here
                for bit in (0, 1):
                    add(n, pm.end(), k - 1, const_bits(rng, bit), "stuck", define)
        if re.search(r"\)\s*;", line) and instance_mod and not re.search(r"\.\w+\s*\(", line.split(");")[-1]):
            if re.match(r"\s*\);\s*$", line) or line.rstrip().endswith(");"):
                instance_mod = None

    # the same replacement can be produced twice (stuck on an if and on its line); keep the first
    uniq, seen = [], set()
    for mt in out:
        key = (mt["line"], mt["text"])
        if key not in seen and mt["text"] != raw[mt["line"] - 1]:
            seen.add(key)
            uniq.append(mt)
    return uniq


def all_mutants(files):
    paths = [os.path.join(SRC, f) for f in DESIGN + ["keyer_isa.vh"]]
    ports = port_table([p for p in paths if p.endswith(".v")])
    res = []
    for f in files:
        res += mutants_of(os.path.join(SRC, os.path.basename(f)), ports)
    ids = {}
    for mt in res:
        assert mt["id"] not in ids, "duplicate mutant id %s" % mt["id"]
        ids[mt["id"]] = mt
    return res


# ---------------------------------------------------------------- running

def env_with_venv():
    env = dict(os.environ)
    venv = os.path.join(ROOT, ".venv", "bin")
    if os.path.isdir(venv):
        env["PATH"] = venv + os.pathsep + env["PATH"]
    return env


def run_cmd(cmd, cwd, timeout, env=None, kill_on=None):
    """Run cmd; returns (status, output) with status in ok / fail / timeout.
    kill_on: a regex; the process is stopped at the first output line matching it."""
    start = time.time()
    env = dict(env or env_with_venv())
    env["PWD"] = cwd                    # test/Makefile builds its paths from $(PWD)
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True, shell=isinstance(cmd, str))
    lines, hit = [], None
    try:
        import selectors
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        while True:
            if time.time() - start > timeout:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                return "timeout", "".join(lines)
            if not sel.select(timeout=1.0):
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            lines.append(line)
            if kill_on and hit is None:
                m = kill_on.search(line)
                if m:
                    hit = m
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    return "fail", "".join(lines)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    proc.wait()
    return ("ok" if proc.returncode == 0 else "fail"), "".join(lines)


FAILED = re.compile(r"(?:^|\s)((?:test\w*)\.(?:test_\w+))\s+(?:failed|errored)|\*\*\s+((?:test\w*)\.(?:test_\w+))\s+(?:FAIL|ERROR)")


def prepare(work, mt):
    """A scratch copy of src/ (with the mutant applied) and of formal/."""
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(os.path.join(work, "src"))
    for f in os.listdir(SRC):
        if f.endswith((".v", ".vh")):
            shutil.copy(os.path.join(SRC, f), os.path.join(work, "src", f))
    if mt is not None:
        p = os.path.join(work, "src", mt["file"])
        lines = open(p).read().split("\n")
        lines[mt["line"] - 1] = mt["text"]
        open(p, "w").write("\n".join(lines))
    os.makedirs(os.path.join(work, "formal"))
    fdir = os.path.join(ROOT, "formal")
    for f in os.listdir(fdir):
        if f.endswith((".sby", ".sv", ".sh")):
            shutil.copy(os.path.join(fdir, f), os.path.join(work, "formal", f))


def compile_ok(work, define):
    cmd = ["iverilog", "-g2005", "-I", os.path.join(work, "src"), "-o", os.devnull]
    if define:
        cmd.append("-D" + define)
    cmd += [os.path.join(work, "src", f) for f in [STUB] + DESIGN]
    return run_cmd(cmd, work, 120)


def cocotb(work, define, test_filter, timeout):
    cmd = ["make", "SRC_DIR=" + os.path.join(work, "src"), "SIM_BUILD=" + os.path.join(work, "sim_build"),
           "COCOTB_RESULTS_FILE=" + os.path.join(work, "results.xml")]
    if test_filter:
        # cocotb's Makefile puts the value on a shell command line unquoted
        cmd.append("COCOTB_TEST_FILTER='%s'" % test_filter)
    env = env_with_venv()
    if define:
        env["COMPILE_ARGS"] = "-D" + define
    status, out = run_cmd(cmd, os.path.join(ROOT, "test"), timeout, env=env, kill_on=FAILED)
    m = FAILED.search(out)
    if m:
        return "killed", (m.group(1) or m.group(2))
    if status == "timeout":
        return "timeout", "cocotb"
    if status != "ok" or "FAIL=0" not in out:
        return "error", out[-2000:]
    return "pass", ""


def formal(work, group, timeout):
    fdir = os.path.join(work, "formal")
    if group == "core":
        status, out = run_cmd(["sh", "./run_core_pdr.sh"], fdir, timeout)
        if "Property proved" in out:
            return "pass", ""
        if status == "timeout":
            return "timeout", "formal core"
        if "failed" in out or "was asserted" in out:
            return "killed", "formal: core"
        return "error", out[-2000:]
    status, out = run_cmd(["yowasp-sby", "-f", group + ".sby"], fdir, timeout)
    done = re.findall(r"DONE \((\w+)", out)
    want = {"fifo": 2, "pins": 1, "capture": 2}[group]
    if status == "timeout":
        return "timeout", "formal " + group
    if len(done) == want and all(d == "PASS" for d in done):
        return "pass", ""
    if any(d == "FAIL" for d in done):
        return "killed", "formal: " + group
    return "error", out[-2000:]


EQUIV_YS = """
read_verilog {defs} -I{gold} {gold_files}
hierarchy -top {top}
proc; flatten; memory_map; opt_clean
rename {top} gold
design -stash gold
read_verilog {defs} -I{gate} {gate_files}
hierarchy -top {top}
proc; flatten; memory_map; opt_clean
rename {top} gate
design -stash gate
design -copy-from gold -as gold gold
design -copy-from gate -as gate gate
equiv_make gold gate equiv
hierarchy -top equiv
equiv_simple
equiv_induct
equiv_status -assert
"""

# For the check at the top level the program memory is replaced by this
# stand-in: a register of everything the memory is given, and read data that
# is a function of that register. The two sides' registers are matched by
# name, so the proof obligation is "the memory sees the same inputs in every
# cycle and its read data is used the same way", whatever a real memory does.
IMEM_STANDIN = """
module keyer_imem (input wire clk, input wire we, input wire [7:0] addr,
                   input wire [15:0] wdata, output wire [15:0] rdata);
    reg [24:0] seen;
    always @(posedge clk) seen <= {we, addr, wdata};
    assign rdata = seen[15:0] ^ {seen[23:16], seen[23:16]} ^ {16{seen[24]}};
endmodule
"""


def yosys_equiv(work, mt, timeout=600):
    """Is the mutant equivalent to the original: every output and every
    register input equal for any input and any common state? Tried on the
    mutated module alone, then on the flattened top (which also discharges
    faults on signals the top does not use). Returns the level that proved
    it, or None. A mutant inside the macro instantiation cannot be checked
    (the macro is a black box) and is never reported equivalent here."""
    gold, gate = SRC, os.path.join(work, "src")
    in_macro_branch = mt["file"] == "keyer_imem.v" and not mt["define"]
    if in_macro_branch:
        return None
    standin = os.path.join(work, "imem_standin.v")
    open(standin, "w").write(IMEM_STANDIN)
    attempts = []
    if mt["file"].endswith(".v") and mt["file"] != TOP + ".v":
        defs = "-D" + mt["define"] if mt["define"] else ""
        attempts.append(("module", mt["file"][:-2], [mt["file"]], [mt["file"]], defs))
    if mt["file"] != "keyer_imem.v":
        rest = [f for f in DESIGN if f != "keyer_imem.v"]
        attempts.append(("top", TOP, rest, rest, ""))
    for level, top, gold_names, gate_names, defs in attempts:
        extra = " " + standin if level == "top" else ""
        script = EQUIV_YS.format(defs=defs, gold=gold, gate=gate, top=top,
                                 gold_files=" ".join(os.path.join(gold, f) for f in gold_names) + extra,
                                 gate_files=" ".join(os.path.join(gate, f) for f in gate_names) + extra)
        ys = os.path.join(work, "equiv_%s.ys" % level)
        open(ys, "w").write(script)
        status, out = run_cmd(["yosys", "-q", "-l", os.path.join(work, "equiv_%s.log" % level), ys], work, timeout)
        if status == "ok":
            return level
    return None


def documented_equivalents():
    ids = {}
    if os.path.exists(EQUIV_DOC):
        for line in open(EQUIV_DOC):
            m = re.match(r"\|\s*`?([a-z0-9_]+-[0-9a-f]{8})`?\s*\|", line)
            if m:
                ids[m.group(1)] = line.split("|")[-2].strip()
    return ids


def run_one(mt, args, base):
    work = os.path.join(WORK, mt["id"])
    rec = dict(mt)
    t0 = time.time()
    try:
        prepare(work, mt)
        status, out = compile_ok(work, mt["define"])
        if status != "ok":
            rec.update(status="invalid", cocotb="-", formal="-", note=out.strip().split("\n")[0][:200])
            return rec
        # cocotb: the short tests first, then the whole suite, stopping at the first failure
        c, who = cocotb(work, mt["define"], QUICK, base["quick"] * 4 + 120)
        if c == "pass":
            c, who = cocotb(work, mt["define"], REST, base["rest"] * 4 + 300)
        rec["cocotb"] = {"pass": "survived", "killed": who, "timeout": "timeout", "error": "error"}[c]
        if c == "error":
            rec["note"] = who[-300:]
        # the proof of the mutated module, independently of the simulation result
        group = FORMAL.get(mt["file"])
        if group and not (args.no_formal_if_killed and c == "killed"):
            f, what = formal(work, group, base.get("formal_" + group, 60) * 4 + 240)
            rec["formal"] = {"pass": "survived", "killed": "killed", "timeout": "timeout", "error": "error"}[f]
            if f == "error":
                rec["note"] = (rec.get("note", "") + " | formal: " + what)[-300:]
        else:
            rec["formal"] = "-"
        killed = c in ("killed", "timeout") or rec["formal"] in ("killed", "timeout")
        if killed:
            rec["status"] = "killed"
        elif "error" in (rec["cocotb"], rec["formal"]):
            rec["status"] = "error"
        else:
            level = yosys_equiv(work, mt)
            rec["status"] = "equivalent" if level else "survived"
            if level:
                rec["note"] = "proved by Yosys at the %s level" % level
        return rec
    finally:
        rec["seconds"] = round(time.time() - t0, 1)
        rec["before"], rec["after"] = rec["before"].strip(), rec["after"] or "(removed)"
        if not args.keep and rec.get("status") not in ("error",):
            shutil.rmtree(work, ignore_errors=True)


def baseline(args):
    """The unmutated design through every stage: it must pass, and the times
    set the per-mutant timeouts."""
    work = os.path.join(WORK, "baseline")
    base = {}
    for define in (None, "KEYER_IMEM_FLOPS"):
        prepare(work, None)
        status, out = compile_ok(work, define)
        assert status == "ok", "baseline does not compile:\n" + out
        t = time.time()
        c, who = cocotb(work, define, QUICK, 900)
        assert c == "pass", "baseline fails the short tests (%s): %s" % (define, who)
        q = time.time() - t
        t = time.time()
        c, who = cocotb(work, define, REST, 3600)
        assert c == "pass", "baseline fails the suite (%s): %s" % (define, who)
        r = time.time() - t
        if define is None:
            base["quick"], base["rest"] = q, r
        print("baseline%s: short tests %.0f s, rest of the suite %.0f s" % (" with " + define if define else "", q, r), flush=True)
    for group in sorted(set(FORMAL.values())):
        t = time.time()
        f, what = formal(work, group, 1800)
        assert f == "pass", "baseline proof %s does not pass: %s" % (group, what)
        base["formal_" + group] = time.time() - t
        print("baseline: formal %s %.0f s" % (group, base["formal_" + group]), flush=True)
    shutil.rmtree(work, ignore_errors=True)
    return base


COLUMNS = ("id", "file", "line", "col", "op", "before", "after", "status", "cocotb", "formal", "note")
HEADER = "id\tfile\tline\tcol\toperator\tbefore\tafter\tstatus\tcocotb (first failing test)\tformal\tnote"
RESULTS = os.path.join(OUT, "results.tsv")


def load_results():
    """docs/mutation/results.tsv is both the report and the store a later run resumes from."""
    res = {}
    if os.path.exists(RESULTS):
        for line in open(RESULTS).read().split("\n")[1:]:
            if line:
                rec = dict(zip(COLUMNS, line.split("\t")))
                rec["line"], rec["col"] = int(rec["line"]), int(rec["col"])
                res[rec["id"]] = rec
    return res


def save_results(res, order=None):
    os.makedirs(OUT, exist_ok=True)
    order = order or {}
    rows = sorted(res.values(), key=lambda r: (order.get(r["id"], 1 << 30), r["id"]))
    with open(RESULTS + ".tmp", "w") as f:
        f.write(HEADER + "\n")
        for r in rows:
            f.write("\t".join(str(r.get(c, "")).replace("\t", " ").replace("\n", " ") for c in COLUMNS) + "\n")
    os.replace(RESULTS + ".tmp", RESULTS)


def cmd_list(args):
    for mt in all_mutants(args.files or DEFAULT_FILES):
        print("%-18s %s:%d:%d  %-5s  %s  ->  %s%s" % (mt["id"], mt["file"], mt["line"], mt["col"], mt["op"],
                                                    mt["before"].strip() or "(nothing)", mt["after"] or "(removed)",
                                                    "  [%s]" % mt["define"] if mt["define"] else ""))
    return 0


def cmd_run(args):
    mutants = all_mutants(args.files or DEFAULT_FILES)
    results = load_results()
    order = {mt["id"]: i for i, mt in enumerate(all_mutants(DEFAULT_FILES))}
    for stale in [k for k in results if k not in order]:
        del results[stale]                         # the line it mutated has changed or gone
    if args.only:
        want = set(args.only.split(","))
        todo = [mt for mt in mutants if mt["id"] in want]
    elif args.rerun:
        want = set(args.rerun.split(","))
        todo = [mt for mt in mutants if mt["id"] not in results or results[mt["id"]]["status"] in want or "all" in want]
    else:
        todo = [mt for mt in mutants if mt["id"] not in results]
    print("%d mutants, %d to run, %d workers" % (len(mutants), len(todo), args.jobs), flush=True)
    if not todo:
        return cmd_report(args)
    os.makedirs(WORK, exist_ok=True)
    bfile = os.path.join(WORK, "baseline.json")
    if args.reuse_baseline and os.path.exists(bfile):
        base = json.load(open(bfile))
    else:
        base = baseline(args)
        json.dump(base, open(bfile, "w"))
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(run_one, mt, args, base): mt for mt in todo}
        for fut in concurrent.futures.as_completed(futures):
            rec = fut.result()
            results[rec["id"]] = rec
            done += 1
            print("[%d/%d] %-18s %-10s cocotb=%s formal=%s (%.0f s)" % (
                done, len(todo), rec["id"], rec["status"], rec.get("cocotb"), rec.get("formal"), rec["seconds"]), flush=True)
            if done % 10 == 0:
                save_results(results, order)
    save_results(results, order)
    return cmd_report(args)


def cmd_report(args):
    results = load_results()
    doc = documented_equivalents()
    order = {mt["id"]: i for i, mt in enumerate(all_mutants(DEFAULT_FILES))}
    rows = sorted((r for r in results.values() if r["id"] in order), key=lambda r: order[r["id"]])
    for r in rows:
        if r["status"] == "survived" and r["id"] in doc:
            r["status"], r["note"] = "equivalent", "tools/mutate_equivalents.md"
        elif r["status"] == "equivalent" and r.get("note") == "tools/mutate_equivalents.md" and r["id"] not in doc:
            r["status"], r["note"] = "survived", ""
    save_results({r["id"]: r for r in rows}, order)
    files = []
    for r in rows:
        if r["file"] not in files:
            files.append(r["file"])
    lines = ["| File | Mutants | Invalid | Killed | by cocotb | by a proof | by both | Equivalent | Survived | Score |",
             "|---|---|---|---|---|---|---|---|---|---|"]

    def by_cocotb(r):
        return r.get("cocotb") not in ("survived", "-", "", None, "error")

    def by_proof(r):
        return r.get("formal") in ("killed", "timeout")

    def stat(sel):
        n = len(sel)
        inv = sum(r["status"] == "invalid" for r in sel)
        killed = [r for r in sel if r["status"] == "killed"]
        kc = sum(by_cocotb(r) for r in killed)
        kf = sum(by_proof(r) for r in killed)
        kb = sum(by_cocotb(r) and by_proof(r) for r in killed)
        eq = sum(r["status"] == "equivalent" for r in sel)
        sv = sum(r["status"] in ("survived", "error") for r in sel)
        denom = n - inv - eq
        score = "%.1f%%" % (100.0 * len(killed) / denom) if denom else "n/a"
        return [n, inv, len(killed), kc, kf, kb, eq, sv, score]

    for fn in files:
        lines.append("| `%s` | %s |" % (fn, " | ".join(str(x) for x in stat([r for r in rows if r["file"] == fn]))))
    lines.append("| **all** | %s |" % " | ".join("**%s**" % x for x in stat(rows)))
    byop = ["| Operator | Mutants | Invalid | Killed | Equivalent | Survived |", "|---|---|---|---|---|---|"]
    for op in ("op", "neg", "const", "cond", "stuck"):
        s = stat([r for r in rows if r["op"] == op])
        byop.append("| %s | %d | %d | %d | %d | %d |" % (op, s[0], s[1], s[2], s[6], s[7]))
    proofs = ["| Proof | Mutants of the file | Killed by the proof | of those, not killed by any cocotb test |", "|---|---|---|---|"]
    for fn, group in FORMAL.items():
        sel = [r for r in rows if r["file"] == fn and r["status"] != "invalid"]
        proofs.append("| %s (`%s`) | %d | %d | %d |" % (group, fn, len(sel), sum(by_proof(r) for r in sel),
                                                      sum(by_proof(r) and not by_cocotb(r) for r in sel)))
    tests = {}
    for r in rows:
        if r["status"] == "killed" and by_cocotb(r):
            tests[r["cocotb"]] = tests.get(r["cocotb"], 0) + 1
    first = ["| First failing cocotb test | Mutants |", "|---|---|"]
    first += ["| `%s` | %d |" % kv for kv in sorted(tests.items(), key=lambda kv: -kv[1])]
    summary = "\n\n".join("\n".join(t) for t in (lines, byop, proofs, first)) + "\n"
    open(os.path.join(OUT, "summary.md"), "w").write(summary)
    print(summary)
    left = [r for r in rows if r["status"] in ("survived", "error")]
    for r in left:
        print("%s %-18s %s:%d  %s -> %s" % (r["status"].upper(), r["id"], r["file"], r["line"], r["before"], r["after"]))
    missing = len(order) - len(rows)
    if missing:
        print("%d mutants have not been run" % missing)
    return 1 if left or missing else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("files", nargs="*")
    p = sub.add_parser("run")
    p.add_argument("files", nargs="*")
    p.add_argument("-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    p.add_argument("--only", help="comma-separated mutant ids")
    p.add_argument("--rerun", help="also rerun mutants whose last status is one of these (e.g. survived,error; all = every mutant)")
    p.add_argument("--keep", action="store_true", help="keep the scratch directory of every mutant")
    p.add_argument("--reuse-baseline", action="store_true",
                   help="skip the run of the unmutated design and reuse its recorded times (only right after a run that did it)")
    p.add_argument("--no-formal-if-killed", action="store_true",
                   help="skip the proof for mutants a cocotb test already killed (faster; the report then cannot say what the proofs alone catch)")
    sub.add_parser("report")
    args = ap.parse_args(argv)
    return {"list": cmd_list, "run": cmd_run, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
