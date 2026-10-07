#!/usr/bin/env python3
"""Mutation testing of the Keyer RTL.

    python3 tools/mutate.py list   [FILE ...]
    python3 tools/mutate.py run    [FILE ...] [-j N] [--out F.jsonl] [--resume] [--sample N --seed S]
                                   [--shard I/N] [--only ID,...] [--timeout SECONDS]
    python3 tools/mutate.py report [F.jsonl ...] [--md FILE]
    python3 tools/mutate.py prove-equivalents [F.jsonl ...] [-j N] [--only ID,...] [--timeout SECONDS]
                                   [--out F.jsonl] [--update]

A mutant is the design with one single-line fault. `run` first takes the
unmutated design through every check (it must pass; the times order the
checks), then for each mutant runs the checks fastest first and stops at the
first one that fails: each cocotb test of test/ on its own, and the formal
proof of the mutated module if it has one. One line of JSON per mutant is
appended to the output file as it finishes (id, file, line, operator, status,
killing check, wall time), so a stopped run continues with --resume.

Status:
  killed      a cocotb test reported a failure in its results.xml, or a proof
              reported a counterexample. Nothing else is a kill: a k-induction
              proof that merely stops closing (sby's UNKNOWN) is noted and
              the remaining checks decide.
  error       the mutant did not compile, a check timed out, wrote no
              results.xml or died; reported separately, never counted as killed.
  equivalent  no check failed and Yosys proved every output and register
              input equal to the original's (module level, then the flattened
              top), or tools/mutate_equivalents.md lists the id with a reason.
  survived    no check failed and it is not known to be equivalent.
`report` merges result files, prints the score (killed / (killed + survived)),
the survivors and the errors, and exits non-zero while any of either remain.
`prove-equivalents` takes the mutants that only tools/mutate_equivalents.md
vouches for (status survived in the result files, id in the table) and tries
the reset-sequence miter on each (reset_miter below): a proof that the
mutant and the design agree at the pads and at the program memory's port
in every cycle from the release of reset on, for every input sequence the
host contract allows. --update rewrites the result rows: a proved mutant
becomes `equivalent` with the proof in its note, a counterexample is a
survivor again (the mutant is not equivalent: it needs a test), a timeout
leaves the row as it is (reason only). The macro instantiation
(keyer_imem.v without KEYER_IMEM_FLOPS) is outside the miter and is skipped.

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
generate loops (elaboration structure), and the port-only macro stub
src/RM_IHPSG13_1P_256x16_c2_bm_bist.v (simulation uses the vendored model).
Code under `ifdef KEYER_IMEM_FLOPS is mutated and tested with that define set.

IDs are stable across edits that do not touch the mutated line: a hash of the
file name, the text of the line, which copy of that text it is in the file,
the operator and the replacement.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
WORK = os.path.join(ROOT, "test", "sim_build", "mutation")
EQUIV_DOC = os.path.join(ROOT, "tools", "mutate_equivalents.md")
STUB = "RM_IHPSG13_1P_256x16_c2_bm_bist.v"
DEFAULT_FILES = ["keyer_isa.vh", "keyer_fifo.v", "keyer_imem.v", "keyer_pins.v", "keyer_core.v",
                 "keyer_host.v", "keyer_capture.v", "keyer_ser.v", "tt_um_ahan17x_keyer.v"]
DESIGN = ["keyer_fifo.v", "keyer_imem.v", "keyer_pins.v", "keyer_core.v", "keyer_host.v",
          "keyer_capture.v", "keyer_ser.v", "tt_um_ahan17x_keyer.v"]
TOP = "tt_um_ahan17x_keyer"

# The proofs that read each file (a header mutant reaches the core's proof).
FORMAL = {
    "keyer_fifo.v": "fifo", "keyer_pins.v": "pins", "keyer_core.v": "core",
    "keyer_capture.v": "capture", "keyer_isa.vh": "core", "keyer_ser.v": "ser",
}
# The sby tasks a mutant runs, when not all of the file's tasks (None: all).
# formal/ser.sby's cover tasks only show the proofs are not vacuous; a
# mutant needs only the proofs.
FORMAL_TASKS = {"ser": ["stuff", "crc_any", "crc16", "crc32", "rt_nrzi", "rt_manch"]}

# Signals wider than one bit that are still enables, strobes, selects or
# resets (one-bit signals all get the stuck-at mutants).
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


def run_cmd(cmd, cwd, timeout, env=None):
    """Run cmd in its own process group; returns (status, output), status in
    ok / fail / timeout / signal. The whole group is killed on a timeout."""
    env = dict(env or env_with_venv())
    env["PWD"] = cwd                    # test/Makefile builds its paths from $(PWD)
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=max(1, timeout))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        out, _ = proc.communicate()
        return "timeout", out or ""
    if proc.returncode < 0:
        return "signal", out or ""
    return ("ok" if proc.returncode == 0 else "fail"), out or ""


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
        if f.endswith((".sby", ".sv", ".vh", ".sh")):
            shutil.copy(os.path.join(fdir, f), os.path.join(work, "formal", f))


def compile_ok(work, define):
    cmd = ["iverilog", "-g2005", "-I", os.path.join(work, "src"), "-o", os.devnull]
    if define:
        cmd.append("-D" + define)
    cmd += [os.path.join(work, "src", f) for f in [STUB] + DESIGN]
    return run_cmd(cmd, work, 120)


def read_results(path):
    """results.xml -> {module.test: (verdict, seconds)}, verdict pass / fail / skip; None if unreadable."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    res = {}
    for tc in root.iter("testcase"):
        name = "%s.%s" % (tc.get("classname"), tc.get("name"))
        if tc.find("failure") is not None or tc.find("error") is not None:
            verdict = "fail"
        elif tc.find("skipped") is not None:
            verdict = "skip"
        else:
            verdict = "pass"
        res[name] = (verdict, float(tc.get("time") or 0))
    return res


def cocotb(work, define, test, timeout):
    """Run the cocotb suite (test None) or one test ('module.name'). Returns
    (verdict, detail): pass / fail (a test reported a failure) / error."""
    xml = os.path.join(work, "results.xml")
    if os.path.exists(xml):
        os.remove(xml)
    cmd = ["make", "SRC_DIR=" + os.path.join(work, "src"), "SIM_BUILD=" + os.path.join(work, "sim_build"),
           "COCOTB_RESULTS_FILE=" + xml]
    if test:
        module, name = test.split(".")
        # cocotb's Makefile puts the filter on a shell command line unquoted; $$ is make's $
        cmd += ["COCOTB_TEST_MODULES=" + module, "COCOTB_TEST_FILTER='%s$$'" % name]
    env = env_with_venv()
    if define:
        env["COMPILE_ARGS"] = "-D" + define
    status, out = run_cmd(cmd, os.path.join(ROOT, "test"), timeout, env=env)
    if status in ("timeout", "signal"):
        return "error", status
    res = read_results(xml)
    if res is None:
        return "error", "no results.xml"
    ran = {k: v for k, v in res.items() if v[0] != "skip"}
    if test and test not in ran:
        return "error", "the test did not run"
    failed = [k for k, v in ran.items() if v[0] == "fail"]
    if failed:
        return "fail", failed[0]
    return ("pass", res) if ran else ("error", "no test ran")


def formal(work, group, timeout):
    """pass / fail (a counterexample) / error."""
    fdir = os.path.join(work, "formal")
    if group == "core":
        status, out = run_cmd(["sh", "./run_core_pdr.sh"], fdir, timeout)
        if status in ("timeout", "signal"):
            return "error", status
        if "Property proved" in out:
            return "pass", ""
        if re.search(r"Output \d+ of miter .* was asserted|Property failed|was asserted in frame", out):
            return "fail", "formal:core"
        return "error", "proof did not run: " + out.strip()[-120:]
    if group == "ser":
        tasks = FORMAL_TASKS["ser"]
        status, out = run_cmd(["sh", "./run_ser.sh"] + tasks, fdir, timeout)
        if status in ("timeout", "signal"):
            return "error", status
        done = re.findall(r"^SER \w+ (\w+)$", out, re.M)
        if len(done) == len(tasks) and all(d == "PASS" for d in done):
            return "pass", ""
        if "FAIL" in done:
            return "fail", "formal:ser"
        if done and set(done) <= {"PASS", "UNKNOWN"}:
            return "inconclusive", "proof did not close"
        return "error", "proof did not run: " + " ".join(done)
    tasks = FORMAL_TASKS.get(group)
    status, out = run_cmd(["yowasp-sby", "--yosys", "yowasp-yosys", "-f", group + ".sby"] + (tasks or []), fdir, timeout)
    if status in ("timeout", "signal"):
        return "error", status
    done = re.findall(r"DONE \((\w+)", out)
    want = len(tasks) if tasks else {"fifo": 2, "pins": 1, "capture": 2}[group]
    if len(done) == want and all(d == "PASS" for d in done):
        return "pass", ""
    if "FAIL" in done:
        return "fail", "formal:" + group
    if done and set(done) <= {"PASS", "UNKNOWN"}:
        return "inconclusive", "induction did not close"     # no counterexample from reset: not a kill
    return "error", "proof did not run: " + " ".join(done)


# Only state (flop outputs) and ports are matched between the two sides:
# internal wire names are hidden first, so a fault on a signal nothing
# observable depends on does not leave an unprovable point.
EQUIV_YS = """
read_verilog {defs} -I{gold} {gold_files}
hierarchy -top {top}
proc; flatten; memory_map; opt_clean
select -set keep x:* t:$*dff* %co:+[Q] w:* %i %u
rename -hide w:* @keep %d
opt_clean
rename {top} gold
design -stash gold
read_verilog {defs} -I{gate} {gate_files}
hierarchy -top {top}
proc; flatten; memory_map; opt_clean
select -set keep x:* t:$*dff* %co:+[Q] w:* %i %u
rename -hide w:* @keep %d
opt_clean
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



# ---------------------------------------------------------------- reset-sequence miter
#
# Two copies of the chip (gold: src/, gate: the mutant) in one wrapper, with
# the same pads and the same program-memory read data (a free input: the
# memory instance is everted out of each copy, so its port becomes outputs
# of the design and its read data an input, and the proof is "the memory
# sees the same inputs in every cycle and its read data is used the same
# way", whatever a real memory does). Asserted, in every cycle from the
# release of the first reset on: uo_out, uio_out, uio_oe, the memory's we
# and addr, and its wdata when we is set, are equal. Assumed (SEMANTICS
# 10.1, D-038): reset is asserted at power-up and every reset lasts at least
# three cycles; CS_n is high in the two cycles before a release; each SCK
# half-period is at least four cycles; CS_n stays high at least four cycles
# between transactions; SCK is low when CS_n falls. The power-up state is
# all zeros on both sides (every flop, the FIFO storage included); from
# then on every reset the host applies is checked from every reachable
# state, so a reset value the mutant changes is seen whenever the register
# can hold something else before a reset. What this cannot see: a register
# that the design resets but never writes otherwise, read with a non-zero
# power-up value. Engine: abc's signal correspondence (scorr, which merges
# the registers of the two copies that stay equal by induction) and then
# PDR on the AIGER model, as formal/run_core_pdr.sh.

RESET_STANDIN = """`default_nettype none
module keyer_imem (
    input  wire        clk,
    input  wire        we,
    input  wire [7:0]  addr,
    input  wire [15:0] wdata,
    output wire [15:0] rdata
);
    assign rdata = 16'd0;
    wire _unused = &{clk, we, addr, wdata};
endmodule
"""

RESET_WRAP = r"""module eqwrap(input wire clk, input wire rst_n, input wire ena, input wire [7:0] ui_in, input wire [7:0] uio_in,
              input wire [15:0] rd);
    wire [7:0] g_uo, m_uo, g_uio_out, m_uio_out, g_uio_oe, m_uio_oe, g_addr, m_addr;
    wire g_we, m_we;
    wire [15:0] g_wd, m_wd;
    gold g(.clk(clk), .rst_n(rst_n), .ena(ena), .ui_in(ui_in), .uio_in(uio_in), .u_imem_rdata(rd),
           .uo_out(g_uo), .uio_out(g_uio_out), .uio_oe(g_uio_oe),
           .u_imem_we(g_we), .u_imem_addr(g_addr), .u_imem_wdata(g_wd));
    gate m(.clk(clk), .rst_n(rst_n), .ena(ena), .ui_in(ui_in), .uio_in(uio_in), .u_imem_rdata(rd),
           .uo_out(m_uo), .uio_out(m_uio_out), .uio_oe(m_uio_oe),
           .u_imem_we(m_we), .u_imem_addr(m_addr), .u_imem_wdata(m_wd));
    reg [2:0] rh = 3'b111;      // rst_n in the previous three cycles (power-up: as after a long reset)
    reg [1:0] ch = 2'b11;       // CS_n in the previous two cycles
    reg       live = 1'b0;      // a reset has been released
    reg       started = 1'b0;
    reg       sck_q = 1'b0, csn_q = 1'b1;
    reg [2:0] sck_hold = 3'd4;  // cycles SCK has held its level, saturating at 4
    reg [2:0] csn_hold = 3'd4;  // cycles CS_n has held its level, saturating at 4
    wire sck = ui_in[0], csn = ui_in[2];
    always @(posedge clk) begin
        rh <= {rh[1:0], rst_n};
        ch <= {ch[0], csn};
        started <= 1'b1;
        if (rst_n && !rh[0]) live <= 1'b1;
        sck_q <= sck;
        csn_q <= csn;
        sck_hold <= (sck != sck_q) ? 3'd1 : (sck_hold == 3'd4 ? 3'd4 : sck_hold + 3'd1);
        csn_hold <= (csn != csn_q) ? 3'd1 : (csn_hold == 3'd4 ? 3'd4 : csn_hold + 3'd1);
    end
    wire release = rst_n & ~rh[0];
    always @(*) begin
        if (!started) assume(!rst_n);
        if (release) assume(rh == 3'b000 && ch == 2'b11);
        if (sck != sck_q) assume(sck_hold == 3'd4);
        if (csn_q && !csn) assume(csn_hold == 3'd4 && !sck);
        if (live || release) begin
            assert(g_uo == m_uo);
            assert(g_uio_out == m_uio_out);
            assert(g_uio_oe == m_uio_oe);
            assert(g_we == m_we);
            assert(g_addr == m_addr);
            if (g_we) assert(g_wd == m_wd);     // write data matters only with the write enable
        end
    end
endmodule
"""

RESET_SIDE = """read_verilog {defs} -I{src} {files} {standin}
hierarchy -top {top}
proc
opt_clean
setattr -mod -set keep_hierarchy 1 keyer_imem
flatten -noscopeinfo
memory_map
opt_clean
expose -evert -sep _ c:u_imem
opt_clean
rename {top} {name}
design -stash {name}
"""

RESET_MITER = """design -copy-from gold -as gold gold
design -copy-from gate -as gate gate
read_verilog -formal -sv {wrap}
hierarchy -check -top eqwrap
proc
flatten -noscopeinfo
opt_clean
opt
async2sync
setundef -undriven -init -zero
techmap
opt -fast -nodffe -nosdff
dffunmap
abc -g AND
opt_clean
stat
write_aiger -zinit -map {out}/miter.aim {out}/miter.aig
"""


def reset_miter(work, mt, timeout=1800):
    """The reset-sequence miter of the mutant in work/src against src/.
    Returns (verdict, detail): proved / cex (detail: the frame) / unknown
    (timeout, or the engine gave no verdict) / error (the model did not
    build) / skipped (the macro instantiation)."""
    if mt["file"] == "keyer_imem.v" and not mt["define"]:
        return "skipped", "the macro instantiation is outside the miter"
    out = os.path.join(work, "reset_miter")
    os.makedirs(out, exist_ok=True)
    standin = os.path.join(out, "imem_standin.v")
    open(standin, "w").write(RESET_STANDIN)
    wrap = os.path.join(out, "wrap.sv")
    open(wrap, "w").write(RESET_WRAP)
    files = [f for f in DESIGN if f != "keyer_imem.v"]
    defs = "-D" + mt["define"] if mt["define"] else ""
    ys = ""
    for name, src in (("gold", SRC), ("gate", os.path.join(work, "src"))):
        ys += RESET_SIDE.format(defs=defs, src=src, files=" ".join(os.path.join(src, f) for f in files),
                                standin=standin, top=TOP, name=name)
    ys += RESET_MITER.format(wrap=wrap, out=out)
    open(os.path.join(out, "miter.ys"), "w").write(ys)
    t0 = time.time()
    status, _ = run_cmd(["yosys", "-q", "-l", os.path.join(out, "yosys.log"), os.path.join(out, "miter.ys")], out, timeout)
    if status != "ok" or not os.path.exists(os.path.join(out, "miter.aig")):
        return "error", "the model did not build (%s): see %s/yosys.log" % (status, out)
    left = timeout - (time.time() - t0)
    status, res = run_cmd(["yosys-abc", "-c", "read_aiger %s/miter.aig; fold; strash; scorr; print_stats; pdr; write_cex -a %s/cex.txt" % (out, out)],
                          out, max(60, left))
    open(os.path.join(out, "abc.log"), "w").write(res)
    if "Property proved" in res:
        return "proved", "%.0f s" % (time.time() - t0)
    m = re.search(r"was asserted in frame (\d+)", res)
    if m:
        return "cex", "counterexample at frame %s (the inputs are in %s/cex.txt)" % (m.group(1), out)
    if status == "timeout":
        return "unknown", "timeout after %d s" % timeout
    st = re.search(r"lat =\s*(\d+)\s+and =\s*(\d+)", res)
    if st and st.group(1) == "0":
        # scorr merged the two copies completely: the properties are constants
        # that pdr does not judge (UNDECIDED); a combinational check does
        status, res2 = run_cmd(["yosys-abc", "-c", "read_aiger %s/miter.aig; fold; strash; scorr; iprove" % out], out, 600)
        open(os.path.join(out, "abc.log"), "a").write(res2)
        if "UNSATISFIABLE" in res2:
            return "proved", "%.0f s, every register merged by signal correspondence" % (time.time() - t0)
        if "SATISFIABLE" in res2:
            return "cex", "a constant difference after signal correspondence (see %s/abc.log)" % out
    return "unknown", "no verdict: " + res.strip()[-120:]


def documented_equivalents():
    ids = {}
    if os.path.exists(EQUIV_DOC):
        for line in open(EQUIV_DOC):
            m = re.match(r"\|\s*`?([a-z0-9_]+-[0-9a-f]{8})`?\s*\|", line)
            if m:
                ids[m.group(1)] = line.split("|")[-2].strip()
    return ids


def baseline(configs, groups):
    """The unmutated design through every check. Everything must pass; the
    times decide the order of the checks and the default timeout."""
    work = os.path.join(WORK, "baseline")
    base = {"tests": {}, "formal": {}}
    for define in configs:
        prepare(work, None)
        status, out = compile_ok(work, define)
        assert status == "ok", "the unmutated design does not compile:\n" + out
        verdict, res = cocotb(work, define, None, 3600)
        assert verdict == "pass", "the unmutated design fails the cocotb suite (%s): %s" % (define, res)
        base["tests"][define or ""] = {k: v[1] for k, v in res.items() if v[0] == "pass"}
        print("baseline%s: %d cocotb tests, %.0f s" % (" with " + define if define else "", len(base["tests"][define or ""]),
                                                      sum(base["tests"][define or ""].values())), flush=True)
    for group in groups:
        t = time.time()
        verdict, what = formal(work, group, 1800)
        assert verdict == "pass", "the proof %s does not pass on the unmutated design: %s" % (group, what)
        base["formal"][group] = time.time() - t
        print("baseline: formal %s %.0f s" % (group, base["formal"][group]), flush=True)
    shutil.rmtree(work, ignore_errors=True)
    return base


def run_one(mt, args, base):
    work = os.path.join(WORK, mt["id"])
    rec = dict(id=mt["id"], file=mt["file"], line=mt["line"], op=mt["op"], before=mt["before"].strip(),
               after=mt["after"] or "(removed)", status="error", killed_by="", note="")
    t0 = time.time()
    tests = base["tests"][mt["define"] or ""]
    checks = [(sec, "cocotb", name) for name, sec in tests.items()]
    group = FORMAL.get(mt["file"])
    if group:
        checks.append((base["formal"][group], "formal", group))
    checks.sort()
    limit = args.timeout or 4 * sum(c[0] for c in checks) + 300
    inconclusive = ""
    try:
        prepare(work, mt)
        status, out = compile_ok(work, mt["define"])
        if status != "ok":
            rec["note"] = "does not compile: " + (out.strip().split("\n") or [""])[0][:160]
            return rec
        for _, kind, name in checks:
            left = limit - (time.time() - t0)
            if left <= 0:
                rec["note"] = "timeout after %d s" % limit
                return rec
            verdict, detail = cocotb(work, mt["define"], name, left) if kind == "cocotb" else formal(work, name, left)
            if verdict == "fail":
                rec["status"], rec["killed_by"] = "killed", (name if kind == "cocotb" else "formal:" + name)
                return rec
            if verdict == "error":
                rec["note"] = "%s %s: %s" % (kind, name, detail)
                return rec
            if verdict == "inconclusive":
                inconclusive = "the %s proof no longer closes (no counterexample); " % name
        level = None if args.no_equiv else yosys_equiv(work, mt)
        rec["status"] = "equivalent" if level else "survived"
        rec["note"] = inconclusive + ("proved by Yosys at the %s level" % level if level else "")
        return rec
    except Exception as e:                           # a harness fault is an error, never a kill
        rec["status"], rec["note"] = "error", "harness: %r" % (e,)
        return rec
    finally:
        rec["seconds"] = round(time.time() - t0, 1)
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


def read_jsonl(paths):
    res = {}
    for p in paths:
        if os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if line:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue                     # a line cut off by a kill
                    res[r["id"]] = r
    return res


def select(args):
    mutants = all_mutants(args.files or DEFAULT_FILES)
    if args.only:
        want = set(args.only.split(","))
        return [m for m in mutants if m["id"] in want]
    if args.sample:
        mutants = sorted(random.Random(args.seed).sample(mutants, min(args.sample, len(mutants))),
                         key=lambda m: (m["file"], m["line"], m["col"]))
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        mutants = [m for k, m in enumerate(mutants) if k % n == i]
    return mutants


def cmd_list(args):
    for mt in select(args):
        print("%-18s %s:%d:%d  %-5s  %s  ->  %s%s" % (mt["id"], mt["file"], mt["line"], mt["col"], mt["op"],
                                                    mt["before"].strip() or "(nothing)", mt["after"] or "(removed)",
                                                    "  [%s]" % mt["define"] if mt["define"] else ""))
    return 0


def cmd_run(args):
    todo = select(args)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    if args.resume:
        done_ids = {k for k, r in read_jsonl([args.out]).items() if not (args.retry_errors and r["status"] == "error")}
        todo = [m for m in todo if m["id"] not in done_ids]
        print("resuming: %d results already in %s" % (len(done_ids), args.out), flush=True)
    else:
        open(args.out, "w").close()
    print("%d mutants to run, %d workers" % (len(todo), args.jobs), flush=True)
    if not todo:
        return 0
    os.makedirs(WORK, exist_ok=True)
    configs = sorted({m["define"] for m in todo}, key=lambda d: d or "")
    groups = sorted({FORMAL[m["file"]] for m in todo if m["file"] in FORMAL})
    bfile = os.path.join(WORK, "baseline.json")
    base = json.load(open(bfile)) if args.reuse_baseline and os.path.exists(bfile) else None
    if base is None or any((c or "") not in base["tests"] for c in configs) or any(g not in base["formal"] for g in groups):
        base = baseline(configs, groups)
        json.dump(base, open(bfile, "w"))
    lock, n, t0 = threading.Lock(), [0], time.time()

    def work(mt):
        rec = run_one(mt, args, base)
        with lock, open(args.out, "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            n[0] += 1
            print("[%d/%d] %-18s %-10s %-45s %5.0f s  %s" % (n[0], len(todo), rec["id"], rec["status"], rec["killed_by"],
                                                           rec["seconds"], rec["note"]), flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        list(pool.map(work, todo))
    print("wall time %.0f s for %d mutants with %d workers" % (time.time() - t0, len(todo), args.jobs))
    return 0


def cmd_report(args):
    rows = list(read_jsonl(args.results or [DEFAULT_OUT]).values())
    doc = documented_equivalents()
    for r in rows:
        if r["status"] == "survived" and r["id"] in doc:
            r["status"], r["note"] = "equivalent", "tools/mutate_equivalents.md"
    rows.sort(key=lambda r: (r["file"], r["line"], r["id"]))

    def stat(sel):
        c = {k: sum(r["status"] == k for r in sel) for k in ("killed", "survived", "equivalent", "error")}
        denom = c["killed"] + c["survived"]
        return [len(sel), c["killed"], c["survived"], c["equivalent"], c["error"],
                "%.1f%%" % (100.0 * c["killed"] / denom) if denom else "n/a"]

    out = ["| File | Mutants | Killed | Survived | Equivalent | Error | Score |", "|---|---|---|---|---|---|---|"]
    for fn in sorted({r["file"] for r in rows}):
        out.append("| `%s` | %s |" % (fn, " | ".join(str(x) for x in stat([r for r in rows if r["file"] == fn]))))
    out.append("| **all** | %s |" % " | ".join("**%s**" % x for x in stat(rows)))
    eq = [r for r in rows if r["status"] == "equivalent"]
    if eq:
        kinds = (("proved by Yosys at the module level", "module level"), ("proved by Yosys at the top level", "top level"),
                 ("reset-sequence miter", "reset-sequence miter"), ("tools/mutate_equivalents.md", "written reason only"))
        parts = ["%d %s" % (sum(k in r["note"] for r in eq), label) for k, label in kinds]
        out += ["", "Equivalent: " + ", ".join(parts) + "."]
    out += ["", "| Operator | Mutants | Killed | Survived | Equivalent | Error | Score |", "|---|---|---|---|---|---|---|"]
    for op in ("op", "neg", "const", "cond", "stuck"):
        out.append("| %s | %s |" % (op, " | ".join(str(x) for x in stat([r for r in rows if r["op"] == op]))))
    by = {}
    for r in rows:
        if r["status"] == "killed":
            by[r["killed_by"]] = by.get(r["killed_by"], 0) + 1
    out += ["", "| Killing check (the fastest that fails) | Mutants |", "|---|---|"]
    out += ["| `%s` | %d |" % kv for kv in sorted(by.items(), key=lambda kv: -kv[1])]
    secs = [r.get("seconds", 0) for r in rows]
    out += ["", "%d mutants, %.0f s of checks in total, %.1f s per mutant on average (max %.0f s)." % (
        len(rows), sum(secs), sum(secs) / max(1, len(secs)), max(secs or [0]))]
    left = [r for r in rows if r["status"] in ("survived", "error")]
    if left:
        out += ["", "| Status | Id | Where | Mutation | Note |", "|---|---|---|---|---|"]
        out += ["| %s | `%s` | `%s:%d` | `%s` -> `%s` | %s |" % (r["status"], r["id"], r["file"], r["line"], r["before"].replace("|", "\\|"),
                                                              r["after"].replace("|", "\\|"), r["note"]) for r in left]
    text = "\n".join(out) + "\n"
    print(text)
    if args.md:
        open(args.md, "w").write(text)
    return 1 if left else 0


def cmd_prove_equivalents(args):
    paths = args.results or [DEFAULT_OUT]
    rows = read_jsonl(paths)
    doc = documented_equivalents()
    if args.only:
        want = [i for i in args.only.split(",")]
    else:
        want = sorted(i for i, r in rows.items() if r["status"] == "survived" and i in doc)
    mutants = {m["id"]: m for m in all_mutants(DEFAULT_FILES)}
    missing = [i for i in want if i not in mutants]
    if missing:
        print("not a mutant of the present sources (the line changed?): " + " ".join(missing))
    todo = [mutants[i] for i in want if i in mutants]
    print("%d mutants backed only by a written reason; %d workers, %d s each" % (len(todo), args.jobs, args.timeout), flush=True)
    os.makedirs(WORK, exist_ok=True)
    lock, results = threading.Lock(), {}

    def work(mt):
        wdir = os.path.join(WORK, "prove_" + mt["id"])
        t0 = time.time()
        try:
            prepare(wdir, mt)
            verdict, detail = reset_miter(wdir, mt, args.timeout)
        except Exception as e:                      # a harness fault is not a verdict
            verdict, detail = "error", "harness: %r" % (e,)
        rec = dict(id=mt["id"], file=mt["file"], line=mt["line"], verdict=verdict, detail=detail,
                   seconds=round(time.time() - t0, 1))
        with lock:
            results[mt["id"]] = rec
            if args.out:
                with open(args.out, "a") as f:
                    f.write(json.dumps(rec, sort_keys=True) + "\n")
            print("[%d/%d] %-18s %-8s %5.0f s  %s" % (len(results), len(todo), rec["id"], verdict, rec["seconds"], detail), flush=True)
        if not args.keep and verdict != "cex":
            shutil.rmtree(wdir, ignore_errors=True)

    if args.out and not args.resume:
        open(args.out, "w").close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        list(pool.map(work, todo))
    counts = {k: sum(r["verdict"] == k for r in results.values()) for k in ("proved", "cex", "unknown", "error", "skipped")}
    print("proved %(proved)d, counterexample %(cex)d, unknown %(unknown)d, error %(error)d, skipped %(skipped)d" % counts)
    if args.update:
        for p in paths:
            if not os.path.exists(p):
                continue
            lines = []
            for line in open(p):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                res = results.get(r["id"])
                if res and res["verdict"] == "proved":
                    r["status"], r["note"] = "equivalent", "proved by the reset-sequence miter (%.0f s)" % res["seconds"]
                elif res and res["verdict"] == "cex":
                    r["status"], r["note"] = "survived", "reset-sequence miter: %s; not equivalent, needs a test" % res["detail"]
                lines.append(json.dumps(r, sort_keys=True))
            open(p, "w").write("\n".join(lines) + "\n")
            print("updated " + p)
    return 1 if counts["cex"] or counts["error"] else 0


DEFAULT_OUT = os.path.join(WORK, "results.jsonl")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "run"):
        p = sub.add_parser(name)
        p.add_argument("files", nargs="*")
        p.add_argument("--only", help="comma-separated mutant ids")
        p.add_argument("--sample", type=int, help="a random subset of this many mutants")
        p.add_argument("--seed", type=int, default=1, help="seed of --sample (default 1)")
        p.add_argument("--shard", help="I/N: every N-th mutant starting at the I-th (0-based)")
    p.add_argument("-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    p.add_argument("--out", default=DEFAULT_OUT, help="JSONL result file (default test/sim_build/mutation/results.jsonl)")
    p.add_argument("--resume", action="store_true", help="keep the results already in --out and run only the others")
    p.add_argument("--retry-errors", action="store_true", help="with --resume: run the mutants whose result is an error again")
    p.add_argument("--timeout", type=float, help="seconds allowed per mutant (default: 4 x the unmutated run + 300)")
    p.add_argument("--keep", action="store_true", help="keep the scratch directory of every mutant")
    p.add_argument("--no-equiv", action="store_true", help="skip the Yosys equivalence check of mutants nothing kills")
    p.add_argument("--reuse-baseline", action="store_true", help="reuse the recorded run of the unmutated design")
    p = sub.add_parser("report")
    p.add_argument("results", nargs="*")
    p.add_argument("--md", help="also write the report to this file")
    p = sub.add_parser("prove-equivalents")
    p.add_argument("results", nargs="*", help="result files (default test/sim_build/mutation/results.jsonl)")
    p.add_argument("--only", help="comma-separated mutant ids (default: every documented equivalent with status survived)")
    p.add_argument("-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    p.add_argument("--timeout", type=float, default=1800, help="seconds per mutant for the model build and the proof (default 1800)")
    p.add_argument("--out", help="JSONL file for the verdicts (one line per mutant)")
    p.add_argument("--resume", action="store_true", help="append to --out instead of truncating it")
    p.add_argument("--update", action="store_true", help="rewrite the result rows with the verdicts (see the module docstring)")
    p.add_argument("--keep", action="store_true", help="keep the scratch directory of every mutant (a counterexample's is always kept)")
    args = ap.parse_args(argv)
    return {"list": cmd_list, "run": cmd_run, "report": cmd_report, "prove-equivalents": cmd_prove_equivalents}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
