#!/usr/bin/env python3
"""Keyer assembler.

Usage: keyerasm.py input.s [-o out.hex] [-l out.lst] [--check-timing [-v]]

Syntax
  label:            labels end with a colon; may share a line with an instruction
  mnemonic a, b     operands separated by commas; registers r0-r7
  ; comment         also '#' and '//'
  .org ADDR         set the location counter
  .word V, V, ...   raw 16-bit words
  .equ NAME, EXPR   define a symbol (also: NAME = EXPR)
  LDW rd, imm16     pseudo-instruction, expands to LDI + LDIH

Pins may be written as numbers, as uio0..uio7 / ui0..ui7 / uo0..uo7, or as
symbols. Branch targets are labels or expressions giving an absolute address;
the assembler converts them to relative offsets and checks the range.
Expressions support + - * / % << >> & | ^ ~ and parentheses.
"""

import argparse
import ast
import operator
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyer_isa as isa  # noqa: E402


class AsmError(Exception):
    pass


_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv, ast.Div: operator.floordiv, ast.Mod: operator.mod,
    ast.LShift: operator.lshift, ast.RShift: operator.rshift, ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_, ast.BitXor: operator.xor,
}
_UNOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Invert: operator.invert}


def _eval(node, symbols):
    if isinstance(node, ast.Expression):
        return _eval(node.body, symbols)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, str)):
        v = node.value
        if isinstance(v, str):
            if len(v) != 1:
                raise AsmError("bad character literal %r" % v)
            return ord(v)
        return v
    if isinstance(node, ast.Name):
        if node.id in symbols:
            return symbols[node.id]
        if node.id in isa.PIN_NAMES:
            return isa.PIN_NAMES[node.id]
        raise AsmError("undefined symbol %s" % node.id)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        return _BINOPS[type(node.op)](_eval(node.left, symbols), _eval(node.right, symbols))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNOPS:
        return _UNOPS[type(node.op)](_eval(node.operand, symbols))
    raise AsmError("unsupported expression")


def eval_expr(text, symbols):
    text = text.strip()
    text = re.sub(r"\b0b([01_]+)\b", lambda m: str(int(m.group(1), 2)), text)
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        raise AsmError("cannot parse expression %r" % text)
    return _eval(tree, symbols)


_REG = re.compile(r"^r([0-7])$", re.I)


def _split_operands(s):
    out, cur, depth, quote = [], "", 0, None
    for ch in s:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            cur += ch
        elif ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _strip_comment(line):
    out, quote = "", None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            out += ch
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            out += ch
        elif ch in ";#" or line.startswith("//", i):
            break
        else:
            out += ch
        i += 1
    return out


class Line:
    __slots__ = ("no", "text", "label", "op", "args", "addr", "words")

    def __init__(self, no, text):
        self.no, self.text = no, text
        self.label = self.op = None
        self.args = []
        self.addr = 0
        self.words = []


def parse(source):
    lines = []
    for no, raw in enumerate(source.splitlines(), 1):
        text = _strip_comment(raw).strip()
        ln = Line(no, raw.rstrip("\n"))
        if not text:
            lines.append(ln)
            continue
        m = re.match(r"^([A-Za-z_.$][\w.$]*)\s*:\s*(.*)$", text)
        if m:
            ln.label, text = m.group(1), m.group(2).strip()
        m = re.match(r"^([A-Za-z_][\w]*)\s*=\s*(.+)$", text)
        if m and m.group(1).upper() not in isa.BY_NAME and m.group(1).upper() not in isa.ALIASES:
            ln.op, ln.args = ".equ", [m.group(1), m.group(2)]
            lines.append(ln)
            continue
        if text:
            parts = text.split(None, 1)
            ln.op = parts[0].upper() if not parts[0].startswith(".") else parts[0].lower()
            ln.args = _split_operands(parts[1]) if len(parts) > 1 else []
        lines.append(ln)
    return lines


def _size(ln):
    if ln.op is None or ln.op in (".equ", ".org"):
        return 0
    if ln.op == ".word":
        return len(ln.args)
    if ln.op == "LDW":
        return 2
    return 1


def assemble(source, origin=0, symbols=None):
    """Returns (words: dict addr->int, symbols: dict, listing: list[str]).

    `symbols` pre-defines names (like -D on a C compiler); a .equ in the source
    of the same name is ignored so tests can override firmware defaults."""
    return assemble_lines(source, origin, symbols)[:3]


def assemble_lines(source, origin=0, symbols=None):
    """assemble(), and as a fourth value the parsed source lines with their
    addresses and words (for tools/keytiming.py)."""
    lines = parse(source)
    predefined = dict(symbols or {})
    symbols = dict(predefined)
    # pass 1: addresses and labels
    pc = origin
    for ln in lines:
        if ln.op == ".org":
            pc = eval_expr(ln.args[0], symbols)
        if ln.label:
            if ln.label in symbols:
                raise AsmError("line %d: duplicate label %s" % (ln.no, ln.label))
            symbols[ln.label] = pc
        if ln.op == ".equ" and ln.args[0] not in predefined:
            try:
                symbols[ln.args[0]] = eval_expr(ln.args[1], symbols)
            except AsmError:
                pass  # forward reference; resolved in pass 2
        ln.addr = pc
        pc += _size(ln)
    # pass 2: encode
    _encode_line.predefined = predefined
    words = {}
    listing = []
    for ln in lines:
        try:
            ln.words = _encode_line(ln, symbols)
        except (AsmError, ValueError, KeyError) as e:
            raise AsmError("line %d: %s\n    %s" % (ln.no, e, ln.text))
        for i, w in enumerate(ln.words):
            a = ln.addr + i
            if a in words:
                raise AsmError("line %d: address 0x%X assembled twice" % (ln.no, a))
            if a > 0x7FF:
                raise AsmError("line %d: address 0x%X beyond 11-bit space" % (ln.no, a))
            words[a] = w
        hexpart = " ".join("%04X" % w for w in ln.words)
        listing.append("%04X  %-10s %s" % (ln.addr, hexpart, ln.text) if ln.words
                       else "      %-10s %s" % ("", ln.text))
    return words, symbols, listing, lines


def _pin(text, symbols):
    v = eval_expr(text, symbols)
    if not 0 <= v <= 23:
        raise AsmError("pin %d out of range 0..23" % v)
    return v


def _reg(text):
    m = _REG.match(text.strip())
    if not m:
        raise AsmError("expected register r0..r7, got %r" % text)
    return int(m.group(1))


def _encode_line(ln, symbols):
    op = ln.op
    if op is None:
        return []
    if op == ".org":
        return []
    if op == ".equ":
        if ln.args[0] not in symbols or ln.args[0] not in getattr(_encode_line, "predefined", ()):
            symbols[ln.args[0]] = eval_expr(ln.args[1], symbols)
        return []
    if op == ".word":
        return [eval_expr(a, symbols) & 0xFFFF for a in ln.args]
    if op.startswith("."):
        raise AsmError("unknown directive %s" % op)
    if op == "LDW":
        if len(ln.args) != 2:
            raise AsmError("LDW takes rd, imm16")
        rd = _reg(ln.args[0])
        v = eval_expr(ln.args[1], symbols)
        if not -32768 <= v <= 65535:
            raise AsmError("LDW value %d out of range" % v)
        v &= 0xFFFF
        return [isa.encode("LDI", rd=rd, imm=v & 0xFF), isa.encode("LDIH", rd=rd, imm=v >> 8)]
    name = isa.ALIASES.get(op, op)
    if name not in isa.BY_NAME:
        raise AsmError("unknown mnemonic %s" % op)
    ins = isa.BY_NAME[name]
    if len(ln.args) != len(ins.operands):
        raise AsmError("%s takes %d operand(s): %s" % (
            name, len(ins.operands), ", ".join(o for o, _ in ins.operands)))
    ops = {}
    for text, (opname, fname) in zip(ln.args, ins.operands):
        if fname in ("rd", "rs", "r"):
            ops[opname] = _reg(text)
        elif fname == "pin":
            ops[opname] = _pin(text, symbols)
        elif fname in ("off8", "off6", "off5"):
            target = eval_expr(text, symbols)
            ops[opname] = target - (ln.addr + 1)
        elif fname == "abs11":
            ops[opname] = eval_expr(text, symbols)
        elif fname == "simm8":
            v = eval_expr(text, symbols)
            if 128 <= v <= 255:
                v -= 256  # allow 0xFF-style immediates
            ops[opname] = v
        elif fname in ("imm8", "n8"):
            v = eval_expr(text, symbols)
            if -128 <= v < 0:
                v &= 0xFF
            ops[opname] = v
        else:
            raise AsmError("internal: field %s" % fname)
    try:
        return [isa.encode(name, **ops)]
    except ValueError as e:
        raise AsmError(str(e))


def to_hex(words, size=256):
    top = max(words) + 1 if words else 0
    size = max(size, top)
    return "\n".join("%04X" % words.get(a, 0) for a in range(size)) + "\n"


def to_list(words, size=256):
    top = max(words) + 1 if words else 0
    return [words.get(a, 0) for a in range(max(size, top))]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Keyer assembler")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", help="hex output ($readmemh format)")
    ap.add_argument("-l", "--listing", help="listing file")
    ap.add_argument("--size", type=int, default=256, help="words in output image")
    ap.add_argument("-D", action="append", default=[], metavar="NAME=VALUE", help="predefine a symbol")
    ap.add_argument("--check-timing", action="store_true",
                    help="static timing check of every WAITD (tools/keytiming.py); exit status 1 on a fault")
    ap.add_argument("-v", "--verbose", action="store_true", help="with --check-timing: print the worst path")
    a = ap.parse_args(argv)
    with open(a.input) as f:
        src = f.read()
    predefs = {}
    for d in a.D:
        k, _, v = d.partition("=")
        predefs[k.strip()] = int(v, 0)
    try:
        words, symbols, listing, lines = assemble_lines(src, symbols=predefs)
    except AsmError as e:
        sys.stderr.write("error: %s\n" % e)
        return 1
    if a.check_timing:
        import keytiming
        prog, results = keytiming.analyse(words, lines)
        text, faults = keytiming.report(prog, results, verbose=a.verbose, name=os.path.basename(a.input))
        print(text)
        return 1 if faults else 0
    if a.output:
        with open(a.output, "w") as f:
            f.write(to_hex(words, a.size))
    if a.listing:
        with open(a.listing, "w") as f:
            f.write("\n".join(listing) + "\n\nSymbols:\n")
            for k, v in sorted(symbols.items(), key=lambda kv: kv[1]):
                f.write("  %-20s 0x%04X\n" % (k, v))
    if not a.output and not a.listing:
        print("\n".join(listing))
    n = len(words)
    sys.stderr.write("%d words (%.0f%% of 256)\n" % (n, 100.0 * n / 256))
    return 0


if __name__ == "__main__":
    sys.exit(main())
