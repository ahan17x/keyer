"""Keyer ISA encoding table. Single source of truth.

Every instruction is 16 bits. Bits [15:12] are the major opcode. Field
positions per major (msb:lsb):

  0 ALU2   rd[11:9] rs[8:6] f[5:2]  x[1:0]
  1 ALU1   rd[11:9] f[8:5]  x[4:0]
  2..8     rd[11:9] x[8]    imm[7:0]        (CMPI uses the register as a source)
  9 Bcc    c[11:9]  x[8]    off8[7:0]
  A BPIN   l[11]    pin[10:6] off6[5:0]
  B JMP    c[11]    abs11[10:0]
  C PIN    f[11:8]  x[7:5]  pin[4:0]
  D PINR   r[11:9]  f[8:6]  x[5]  pin[4:0]
  E XFER   r[11:9]  f[8:5]  x[4:0]
  F MISC   f[11:8]  imm8[7:0]

The assembler (keyerasm.py), the simulator (keyersim.py) and the generated
Verilog header (src/keyer_isa.vh) all derive from the tables below.
"""

from collections import namedtuple

ISA_VERSION = 0x01

# Operand kinds and the field they live in, per major.
#   name -> (lsb, width, signed)
FIELDS = {
    0x0: {"rd": (9, 3, False), "rs": (6, 3, False), "f": (2, 4, False)},
    0x1: {"rd": (9, 3, False), "f": (5, 4, False), "off5": (0, 5, True)},
    0x2: {"rd": (9, 3, False), "simm8": (0, 8, True)},
    0x3: {"rd": (9, 3, False), "imm8": (0, 8, False)},
    0x4: {"rd": (9, 3, False), "imm8": (0, 8, False)},
    0x5: {"rd": (9, 3, False), "imm8": (0, 8, False)},
    0x6: {"rd": (9, 3, False), "imm8": (0, 8, False)},
    0x7: {"rd": (9, 3, False), "imm8": (0, 8, False)},
    0x8: {"rs": (9, 3, False), "imm8": (0, 8, False)},
    0x9: {"c": (9, 3, False), "off8": (0, 8, True)},
    0xA: {"l": (11, 1, False), "pin": (6, 5, False), "off6": (0, 6, True)},
    0xB: {"c": (11, 1, False), "abs11": (0, 11, False)},
    0xC: {"f": (8, 4, False), "pin": (0, 5, False)},
    0xD: {"r": (9, 3, False), "f": (6, 3, False), "pin": (0, 5, False)},
    0xE: {"r": (9, 3, False), "f": (5, 4, False)},
    0xF: {"f": (8, 4, False), "n8": (0, 8, False)},
}

Instr = namedtuple("Instr", "name major sub operands flags blocking desc")
# operands: tuple of (assembly operand name, field name). Field "f"/"c"/"l"
# values are fixed by `sub` (or the tuple in `sub` for BPIN/JMP).

_I = []


def _add(name, major, sub, operands=(), flags="", blocking=False, desc=""):
    _I.append(Instr(name, major, sub, tuple(operands), flags, blocking, desc))


# --- ALU2: rd op= rs -------------------------------------------------------
for f, (n, fl, d) in enumerate([
    ("ADD", "ZC", "rd = rd + rs"),
    ("SUB", "ZC", "rd = rd - rs"),
    ("AND", "Z", "rd = rd & rs"),
    ("OR", "Z", "rd = rd | rs"),
    ("XOR", "Z", "rd = rd ^ rs"),
    ("MOV", "Z", "rd = rs"),
    ("CMP", "ZC", "flags(rd - rs)"),
    ("TST", "Z", "flags(rd & rs)"),
    ("ADC", "ZC", "rd = rd + rs + C"),
    ("SBC", "ZC", "rd = rd - rs - C"),
]):
    _add(n, 0x0, f, (("rd", "rd"), ("rs", "rs")), fl, desc=d)

# --- ALU1: rd = op rd ------------------------------------------------------
for f, (n, fl, d) in enumerate([
    ("SHL", "ZC", "C = rd[15]; rd <<= 1"),
    ("SHR", "ZC", "C = rd[0]; rd >>= 1"),
    ("RCL", "ZC", "rotate left through carry"),
    ("RCR", "ZC", "rotate right through carry"),
    ("NOT", "Z", "rd = ~rd"),
    ("NEG", "ZC", "rd = -rd"),
    ("INC", "ZC", "rd = rd + 1"),
    ("DEC", "ZC", "rd = rd - 1"),
    ("SWAP", "Z", "swap bytes"),
    ("REV8", "Z", "bit-reverse low byte"),
]):
    _add(n, 0x1, f, (("rd", "rd"),), fl, desc=d)
_add("DJNZ", 0x1, 10, (("rd", "rd"), ("off", "off5")), "", desc="rd -= 1; if rd != 0: PC = PC+1+off (flags unchanged)")

# --- immediates ------------------------------------------------------------
_add("ADDI", 0x2, None, (("rd", "rd"), ("imm", "simm8")), "ZC", desc="rd += sext(imm8)")
_add("ANDI", 0x3, None, (("rd", "rd"), ("imm", "imm8")), "Z", desc="rd &= zext(imm8)")
_add("ORI", 0x4, None, (("rd", "rd"), ("imm", "imm8")), "Z", desc="rd |= zext(imm8)")
_add("XORI", 0x5, None, (("rd", "rd"), ("imm", "imm8")), "Z", desc="rd ^= zext(imm8)")
_add("LDI", 0x6, None, (("rd", "rd"), ("imm", "imm8")), "", desc="rd = zext(imm8)")
_add("LDIH", 0x7, None, (("rd", "rd"), ("imm", "imm8")), "", desc="rd[15:8] = imm8")
_add("CMPI", 0x8, None, (("rs", "rs"), ("imm", "imm8")), "ZC", desc="flags(rs - zext(imm8))")

# --- branches --------------------------------------------------------------
COND = {"RA": 0, "EQ": 1, "NE": 2, "CS": 3, "CC": 4, "FE": 5, "FNE": 6, "TP": 7}
COND_DESC = {
    "RA": "always", "EQ": "Z=1", "NE": "Z=0", "CS": "C=1", "CC": "C=0",
    "FE": "inbox empty", "FNE": "inbox not empty", "TP": "timer tick pending",
}
for cn, cv in COND.items():
    _add("B" + cn, 0x9, cv, (("off", "off8"),), desc="branch if " + COND_DESC[cn])
_add("BP0", 0xA, 0, (("pin", "pin"), ("off", "off6")), desc="branch if level[pin]=0")
_add("BP1", 0xA, 1, (("pin", "pin"), ("off", "off6")), desc="branch if level[pin]=1")
_add("JMP", 0xB, 0, (("addr", "abs11"),), desc="PC = addr")
_add("CALL", 0xB, 1, (("addr", "abs11"),), desc="LR = PC+1; PC = addr")

# --- pin, immediate pin index ---------------------------------------------
for f, (n, blk, d) in enumerate([
    ("SET", False, "pinwrite(p, 1)"),
    ("CLR", False, "pinwrite(p, 0)"),
    ("OEN", False, "PP: uio_oe[p] = 1"),
    ("OEF", False, "PP: uio_oe[p] = 0"),
    ("OD", False, "pin mode = open-drain"),
    ("PP", False, "pin mode = push-pull"),
    ("WT0", True, "wait level[p] = 0"),
    ("WT1", True, "wait level[p] = 1"),
    ("WTR", True, "wait rising edge"),
    ("WTF", True, "wait falling edge"),
    ("WRC", False, "pinwrite(p, C)"),
    ("RDC", False, "C = level[p]"),
    ("TSTP", False, "Z = (level[p] == 0)"),
]):
    _add(n, 0xC, f, (("pin", "pin"),), "C" if n == "RDC" else ("Z" if n == "TSTP" else ""), blk, d)

# --- pin with register -----------------------------------------------------
_add("OUTR", 0xD, 0, (("pin", "pin"), ("rs", "r")), desc="pinwrite(p, rs[0])")
_add("INR", 0xD, 1, (("rd", "r"), ("pin", "pin")), "Z", desc="rd = level[p]")

# --- transfer --------------------------------------------------------------
for f, (n, op, fl, blk, d) in enumerate([
    ("PUSH", "rs", "", True, "outbox <- rs[7:0]"),
    ("POP", "rd", "", True, "rd = inbox byte"),
    ("RDS", "rd", "", False, "rd = status"),
    ("RDCYC", "rd", "", False, "rd = cycle counter"),
    ("SETT", "rs", "", False, "timer period = rs; restart"),
    ("RDT", "rd", "", False, "rd = timer count"),
    ("PUSHNB", "rs", "C", False, "non-blocking push, C = success"),
    ("POPNB", "rd", "C", False, "non-blocking pop, C = success"),
    ("OUTB", "rs", "", False, "pinwrite(i, rs[i]) for uio"),
    ("INB", "rd", "Z", False, "rd = level[7:0]"),
    ("INW", "rd", "Z", False, "rd = level[15:0]"),
    ("OUTOE", "rs", "", False, "PP pins: uio_oe = rs[7:0]"),
    ("RDLR", "rd", "", False, "rd = LR"),
    ("JMPR", "rs", "", False, "PC = rs"),
]):
    _add(n, 0xE, f, ((op, "r"),), fl, blk, d)

# --- misc ------------------------------------------------------------------
_add("NOP", 0xF, 0, (), desc="nothing")
_add("HALT", 0xF, 1, (), desc="stop this thread")
_add("RET", 0xF, 2, (), desc="PC = LR")
_add("WAITT", 0xF, 3, (), blocking=True, desc="wait timer tick, clear it")
_add("DELAY", 0xF, 4, (("n", "n8"),), blocking=True, desc="occupy 1+n slots")
_add("SETC", 0xF, 5, (), "C", desc="C = 1")
_add("CLC", 0xF, 6, (), "C", desc="C = 0")
_add("START", 0xF, 7, (), desc="start other thread")
_add("STOP", 0xF, 8, (), desc="stop other thread")
_add("CLRT", 0xF, 9, (), desc="clear timer tick")

INSTRUCTIONS = tuple(_I)
BY_NAME = {i.name: i for i in INSTRUCTIONS}

ALIASES = {"BZ": "BEQ", "BNZ": "BNE", "BLO": "BCS", "BHS": "BCC", "JR": "RET"}

# Sub-opcode field name per major (None for pure-immediate majors).
SUB_FIELD = {0x0: "f", 0x1: "f", 0x9: "c", 0xA: "l", 0xB: "c", 0xC: "f",
             0xD: "f", 0xE: "f", 0xF: "f"}

PIN_NAMES = {}
for _i in range(8):
    PIN_NAMES["uio%d" % _i] = _i
    PIN_NAMES["ui%d" % _i] = 8 + _i
    PIN_NAMES["uo%d" % _i] = 16 + _i


def _mask(width):
    return (1 << width) - 1


def _fit(value, width, signed, what):
    if signed:
        lo, hi = -(1 << (width - 1)), (1 << (width - 1)) - 1
    else:
        lo, hi = 0, _mask(width)
    if not (lo <= value <= hi):
        raise ValueError("%s value %d out of range %d..%d" % (what, value, lo, hi))
    return value & _mask(width)


def encode(name, **ops):
    """Encode an instruction. ops are keyed by assembly operand name."""
    name = ALIASES.get(name, name)
    ins = BY_NAME[name]
    fields = FIELDS[ins.major]
    word = ins.major << 12
    if ins.sub is not None:
        lsb, width, _ = fields[SUB_FIELD[ins.major]]
        word |= (ins.sub & _mask(width)) << lsb
    for opname, fname in ins.operands:
        if opname not in ops:
            raise ValueError("%s: missing operand %s" % (name, opname))
        lsb, width, signed = fields[fname]
        word |= _fit(ops[opname], width, signed, "%s %s" % (name, opname)) << lsb
    return word


def decode(word):
    """Decode a 16-bit word to (Instr, {operand: value}) or (None, {}) if illegal."""
    major = (word >> 12) & 0xF
    fields = FIELDS[major]
    sub = None
    if major in SUB_FIELD:
        lsb, width, _ = fields[SUB_FIELD[major]]
        sub = (word >> lsb) & _mask(width)
    for ins in INSTRUCTIONS:
        if ins.major == major and ins.sub == sub:
            ops = {}
            for opname, fname in ins.operands:
                lsb, width, signed = fields[fname]
                v = (word >> lsb) & _mask(width)
                if signed and v & (1 << (width - 1)):
                    v -= 1 << width
                ops[opname] = v
            return ins, ops
    return None, {}


def disasm(word):
    ins, ops = decode(word)
    if ins is None:
        return ".word 0x%04X" % word
    parts = []
    for opname, fname in ins.operands:
        v = ops[opname]
        if fname in ("rd", "rs", "r"):
            parts.append("r%d" % v)
        elif fname == "pin":
            parts.append(str(v))
        elif fname in ("off8", "off6", "off5"):
            parts.append("%+d" % v)
        elif fname == "abs11":
            parts.append("0x%X" % v)
        else:
            parts.append(str(v))
    return (ins.name + " " + ", ".join(parts)).strip()


def gen_verilog_header():
    """Verilog `define block for the RTL decoder."""
    out = ["// Generated by tools/keyer_isa.py. Do not edit.",
           "`ifndef KEYER_ISA_VH", "`define KEYER_ISA_VH",
           "`define KEYER_ISA_VERSION 8'd%d" % ISA_VERSION, ""]
    majors = {0x0: "ALU2", 0x1: "ALU1", 0x2: "ADDI", 0x3: "ANDI", 0x4: "ORI",
              0x5: "XORI", 0x6: "LDI", 0x7: "LDIH", 0x8: "CMPI", 0x9: "BCC",
              0xA: "BPIN", 0xB: "JMP", 0xC: "PIN", 0xD: "PINR", 0xE: "XFER",
              0xF: "MISC"}
    for m, n in majors.items():
        out.append("`define KEYER_MAJ_%-5s 4'h%X" % (n, m))
    out.append("")
    for ins in INSTRUCTIONS:
        if ins.sub is None:
            continue
        lsb, width, _ = FIELDS[ins.major][SUB_FIELD[ins.major]]
        out.append("`define KEYER_%-7s %d'd%d  // major %X: %s" % (ins.name, width, ins.sub, ins.major, ins.desc))
    out.append("")
    for cn, cv in COND.items():
        out.append("`define KEYER_COND_%-4s 3'd%d" % (cn, cv))
    out += ["", "`endif", ""]
    return "\n".join(out)


def gen_markdown_table():
    rows = ["| Mnemonic | Major | Sub | Operands | Flags | Blocking | Operation |",
            "|---|---|---|---|---|---|---|"]
    for ins in INSTRUCTIONS:
        ops = ", ".join(o for o, _ in ins.operands)
        rows.append("| `%s` | %X | %s | %s | %s | %s | %s |" % (
            ins.name, ins.major, "-" if ins.sub is None else ins.sub, ops,
            ins.flags or "-", "yes" if ins.blocking else "", ins.desc))
    return "\n".join(rows)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--vh":
        sys.stdout.write(gen_verilog_header())
    elif len(sys.argv) > 1 and sys.argv[1] == "--md":
        print(gen_markdown_table())
    else:
        # self-check: every instruction round-trips through encode/decode
        n = 0
        for ins in INSTRUCTIONS:
            ops = {}
            for opname, fname in ins.operands:
                lsb, width, signed = FIELDS[ins.major][fname]
                ops[opname] = -1 if signed else _mask(width) if fname != "pin" else 23
            w = encode(ins.name, **ops)
            d, dops = decode(w)
            assert d is ins and dops == ops, (ins.name, w, d, dops, ops)
            n += 1
        print("%d instructions round-trip OK" % n)
