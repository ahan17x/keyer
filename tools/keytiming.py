"""Static timing check of Keyer firmware: `keyerasm.py --check-timing`.

For every `WAITD k` the check finds the worst-case number of thread slots
the program can spend between the instruction that last set or advanced the
deadline (the **anchor**: a `SETT`, a `SETD` or a `WAITD`) and the first
evaluation of that `WAITD`, along any path, and compares it with the
deadline distance when that is a constant.

Slots. A thread executes one instruction slot every two core cycles
(SEMANTICS 2.1). Every instruction costs one slot, `DELAY n` costs 1 + n,
a taken and a not-taken branch both cost one. The count n for a path is
the slots of the instructions after the anchor, up to and including the
first evaluation of the `WAITD` (n = 1 when it follows the anchor directly).

Budget. With the timer period P cycles per tick (the constant a `SETT`
loaded, when the check can see it):

- anchor `WAITD` or `SETT`: the new target is k ticks after the anchor's
  own tick, so the `WAITD` is on time iff n <= floor(k P / 2);
- anchor `SETD j`: the target is j + k ticks after a `NOW` whose phase is
  not known, so between j + k - 1 and j + k periods away. n above
  floor((j + k) P / 2) is LATE; n above floor((j + k - 1) P / 2) is
  reported as MARGIN (on time or not depending on the tick phase), except
  for j + k = 1 (`SETD 0` then `WAITD 1`: a wait of 0 to 1 tick by design).

A late `WAITD` completes at once and the deadline still advances by k
(SEMANTICS 6.2), so LATE means the edge the wait was to time comes late,
not that the program stops.

Paths. The check interprets the assembled words: branches on Z and C are
followed in both directions unless the flag is a known constant, register
constants are tracked through `LDI LDIH MOV` and arithmetic on known
values (so a `DJNZ` on a register loaded with a constant runs exactly that
many times), `CALL`/`RET` are followed through the link register (a `RET`
with an unknown link register returns to every call site of the routines
it belongs to), `JMPR rs` on a known register is followed and otherwise
goes to every label the program loads into `rs` with `LDI` or `LDW` (into
any register, if it loads none into `rs`). A loop that the check cannot bound makes every `WAITD` behind it
UNBOUNDED, unless the branch that closes it carries a bound:

    djnz  r3, again         ; bound 8
    bne   retry             ; bound 3

`; bound N` on a branch line: the branch is taken at most N times between
two deadline instructions (for a conditional branch: N times in a row).

Waits. A blocking instruction on the path (a pin wait, `PUSH`, `POP`, a
serializer wait, with or without timeout) is counted as one slot, as if it
completed at once, and the report names it: the time from the anchor then
depends on an outside event. Such a `WAITD` is reported as WAITS and is a
fault until the source says why it is intended:

    waitd 1                 ; timing: the slave may stretch SCL here

`; timing: reason` on a `WAITD` line waives WAITS, LATE, MARGIN and
UNBOUNDED for that line; the report still prints the numbers, marked
`waived`, with the reason.

Not modelled: the other thread (it has its own slots), a slot lost when a
thread is started in a cycle the capture or replay engine uses its fetch
(SEMANTICS 2.1), and the values of registers the check cannot see.

Exit status of `keyerasm.py --check-timing`: 1 if any `WAITD` is LATE,
UNBOUNDED or WAITS without a waiver, else 0.

SPDX-License-Identifier: Apache-2.0
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyer_isa as isa  # noqa: E402

M16 = 0xFFFF
NODE_LIMIT = 400000

_BOUND = re.compile(r"[;#]\s*.*?\bbound\s+(\d+)\b", re.I)
_WAIVE = re.compile(r"[;#]\s*.*?\btiming:\s*(\S.*)$", re.I)

ANCHORS = ("SETT", "SETD", "WAITD")


class Program:
    """The assembled program with what the check needs per address."""

    def __init__(self, words, lines):
        self.words = words                      # addr -> word
        self.line = {}                          # addr -> source line number
        self.text = {}                          # addr -> source text
        self.code = set()                       # addresses assembled from instructions (not .word)
        self.bound = {}                         # addr -> N
        self.waive = {}                         # addr -> reason
        self.labels = {}
        names = {ln.label: ln.addr for ln in lines if ln.label}
        self.taken = set()                      # labels loaded into a register: the targets of JMPR
        self.taken_reg = {}                     # ... per register number
        self.referenced = set()                 # labels any instruction names
        for ln in lines:
            if ln.label:
                self.labels.setdefault(ln.addr, ln.label)
            if ln.op and not ln.op.startswith("."):
                for tok in re.findall(r"[A-Za-z_.$][\w.$]*", " ".join(ln.args)):
                    if tok in names:
                        self.referenced.add(names[tok] & 0xFF)
                        if ln.op in ("LDI", "LDW"):
                            self.taken.add(names[tok] & 0xFF)
                            m = re.match(r"\s*[rR]([0-7])\s*$", ln.args[0]) if ln.args else None
                            if m:
                                self.taken_reg.setdefault(int(m.group(1)), set()).add(names[tok] & 0xFF)
            for i in range(len(ln.words)):
                a = ln.addr + i
                self.line[a], self.text[a] = ln.no, ln.text.strip()
                if ln.op != ".word":
                    self.code.add(a)
            if ln.words and ln.op != ".word":
                last = ln.addr + len(ln.words) - 1
                m = _BOUND.search(ln.text)
                if m:
                    self.bound[last] = int(m.group(1))
                m = _WAIVE.search(ln.text)
                if m:
                    self.waive[last] = m.group(1).strip()
        self.dec = {a: isa.decode(w) for a, w in words.items()}

    def ins(self, pc):
        return self.dec.get(pc, (None, {}))


# ---------------------------------------------------------------- one instruction

class St:
    """Abstract machine state of one thread: None is 'not a known constant'."""
    __slots__ = ("pc", "regs", "z", "c", "lr", "period", "cnt")

    def __init__(self, pc, regs=(None,) * 8, z=None, c=None, lr=None, period=None, cnt=()):
        self.pc, self.regs, self.z, self.c, self.lr, self.period, self.cnt = pc, regs, z, c, lr, period, cnt

    def key(self):
        return (self.pc, self.regs, self.z, self.c, self.lr, self.period, self.cnt)

    def with_(self, **kw):
        s = St(self.pc, self.regs, self.z, self.c, self.lr, self.period, self.cnt)
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def setreg(self, r, v):
        regs = list(self.regs)
        regs[r] = None if v is None else v & M16
        return regs


def _alu2(name, a, b, c):
    """(result or None for 'no write', z, c) on known operands; None if not modelled."""
    if name in ("ADD", "ADC"):
        if name == "ADC" and c is None:
            return None
        r = a + b + (c if name == "ADC" else 0)
        return r & M16, int(r & M16 == 0), r >> 16
    if name in ("SUB", "SBC", "CMP"):
        if name == "SBC" and c is None:
            return None
        bb = b + (c if name == "SBC" else 0)
        r = (a - bb) & M16
        return (None if name == "CMP" else r), int(r == 0), int(a < bb)
    if name in ("AND", "TST"):
        r = a & b
        return (None if name == "TST" else r), int(r == 0), "keep"
    if name == "OR":
        return a | b, int(a | b == 0), "keep"
    if name == "XOR":
        return a ^ b, int(a ^ b == 0), "keep"
    if name == "MOV":
        return b, int(b == 0), "keep"
    return None


def _alu1(name, a, c):
    if name == "SHL":
        r = (a << 1) & M16
        return r, int(r == 0), a >> 15
    if name == "SHR":
        return a >> 1, int(a >> 1 == 0), a & 1
    if name in ("RCL", "RCR"):
        if c is None:
            return None
        r = ((a << 1) | c) & M16 if name == "RCL" else (a >> 1) | (c << 15)
        return r, int(r == 0), (a >> 15) if name == "RCL" else a & 1
    if name == "NOT":
        return a ^ M16, int(a == M16), "keep"
    if name == "NEG":
        return (-a) & M16, int(a == 0), int(a != 0)
    if name == "INC":
        return (a + 1) & M16, int(a == M16), int(a == M16)
    if name == "DEC":
        return (a - 1) & M16, int(a == 1), int(a == 0)
    if name == "SWAP":
        r = ((a << 8) | (a >> 8)) & M16
        return r, int(r == 0), "keep"
    return None


def step(prog, s, ret_sites=None):
    """Successors of state s: a list of (state, taken) where `taken` marks a
    branch that was taken (for the bound counters). An empty list ends the
    path. ret_sites: pc of a RET -> the return addresses to use when LR is
    unknown."""
    ins, ops = prog.ins(s.pc)
    nxt = (s.pc + 1) & 0xFF
    if ins is None:
        return [(s.with_(pc=nxt), False)]
    n = ins.name
    regs, z, c = s.regs, s.z, s.c

    def fall(**kw):
        return [(s.with_(pc=nxt, **kw), False)]

    def unknown_result():
        """Generic effect: the destination register and the flags the table
        names become unknown."""
        kw = {}
        for opname, _ in ins.operands:
            if opname == "rd":
                kw["regs"] = tuple(s.setreg(ops["rd"], None))
        if "Z" in ins.flags:
            kw["z"] = None
        if "C" in ins.flags:
            kw["c"] = None
        return kw

    if ins.major == 0x0:
        a, b = regs[ops["rd"]], regs[ops["rs"]]
        res = _alu2(n, a, b, c) if a is not None and b is not None else None
        if n == "MOV" and b is not None:
            res = _alu2(n, 0, b, c)
        if res is None:
            return fall(**unknown_result())
        r, zz, cc = res
        kw = {"z": zz}
        if cc != "keep":
            kw["c"] = cc
        if r is not None:
            kw["regs"] = tuple(s.setreg(ops["rd"], r))
        return fall(**kw)
    if n == "DJNZ":
        a = regs[ops["rd"]]
        tgt = (s.pc + 1 + ops["off"]) & 0xFF
        if a is not None:
            r = (a - 1) & M16
            st = s.with_(regs=tuple(s.setreg(ops["rd"], r)))
            return [(st.with_(pc=tgt), True)] if r else [(st.with_(pc=nxt), False)]
        return [(s.with_(pc=tgt), True), (s.with_(pc=nxt), False)]
    if ins.major == 0x1:
        a = regs[ops["rd"]]
        res = _alu1(n, a, c) if a is not None else None
        if res is None:
            return fall(**unknown_result())
        r, zz, cc = res
        kw = {"z": zz, "regs": tuple(s.setreg(ops["rd"], r))}
        if cc != "keep":
            kw["c"] = cc
        return fall(**kw)
    if n in ("ADDI", "ANDI", "ORI", "XORI"):
        a, imm = regs[ops["rd"]], ops["imm"]
        if n == "ANDI" and imm == 0:
            a = 0
        if a is None:
            return fall(**unknown_result())
        if n == "ADDI":
            t = a + (imm & M16)
            return fall(regs=tuple(s.setreg(ops["rd"], t)), z=int(t & M16 == 0), c=t >> 16)
        r = a & imm if n == "ANDI" else a | imm if n == "ORI" else a ^ imm
        return fall(regs=tuple(s.setreg(ops["rd"], r)), z=int(r == 0))
    if n == "LDI":
        return fall(regs=tuple(s.setreg(ops["rd"], ops["imm"])))
    if n == "LDIH":
        a = regs[ops["rd"]]
        return fall(regs=tuple(s.setreg(ops["rd"], None if a is None else (ops["imm"] << 8) | (a & 0xFF))))
    if n == "CMPI":
        a = regs[ops["rs"]]
        if a is None:
            return fall(z=None, c=None)
        return fall(z=int(a == ops["imm"]), c=int(a < ops["imm"]))
    if ins.major == 0x9:
        tgt = (s.pc + 1 + ops["off"]) & 0xFF
        cond = {"BRA": 1, "BEQ": z, "BNE": None if z is None else 1 - z,
                "BCS": c, "BCC": None if c is None else 1 - c}.get(n)
        if cond == 1:
            return [(s.with_(pc=tgt), True)]
        if cond == 0:
            return fall()
        return [(s.with_(pc=tgt), True), (s.with_(pc=nxt), False)]
    if ins.major == 0xA:
        return [(s.with_(pc=(s.pc + 1 + ops["off"]) & 0xFF), True), (s.with_(pc=nxt), False)]
    if n == "JMP":
        return [(s.with_(pc=ops["addr"] & 0xFF), True)]
    if n == "CALL":
        return [(s.with_(pc=ops["addr"] & 0xFF, lr=nxt), False)]
    if n == "RET":
        if s.lr is not None:
            return [(s.with_(pc=s.lr & 0xFF), False)]
        return [(s.with_(pc=a), False) for a in sorted((ret_sites or {}).get(s.pc, ()))]
    if n == "JMPR":
        a = regs[ops["rs"]]
        if a is None:                           # the labels the program loads into this register
            return [(s.with_(pc=t), False) for t in sorted(prog.taken_reg.get(ops["rs"], prog.taken))]
        return [(s.with_(pc=a & 0xFF), False)]
    if n == "RDLR":
        return fall(regs=tuple(s.setreg(ops["rd"], s.lr)))
    if n == "SETT":
        return fall(period=regs[ops["rs"]])
    if n == "HALT":
        return []
    if n == "SETC":
        return fall(c=1)
    if n == "CLC":
        return fall(c=0)
    return fall(**unknown_result())


def cost(prog, pc):
    ins, ops = prog.ins(pc)
    return 1 + ops["n"] if ins is not None and ins.name == "DELAY" else 1


def blocks(prog, pc):
    ins, _ = prog.ins(pc)
    return ins is not None and ins.blocking and ins.name not in ("WAITD", "DELAY")


# ---------------------------------------------------------------- whole-program facts

def _meet(a, b):
    return a if a == b else None


def _meet_state(a, b):
    return St(a.pc, tuple(_meet(x, y) for x, y in zip(a.regs, b.regs)), _meet(a.z, b.z), _meet(a.c, b.c),
              _meet(a.lr, b.lr), _meet(a.period, b.period))


def return_sites(prog):
    """pc of each RET -> the addresses it can return to: the word after
    every CALL whose routine reaches that RET without passing another."""
    out = {}
    for a in sorted(prog.code):
        ins, ops = prog.ins(a)
        if ins is None or ins.name != "CALL":
            continue
        seen, todo = set(), [ops["addr"] & 0xFF]
        while todo:
            pc = todo.pop()
            if pc in seen or pc not in prog.words:
                continue
            seen.add(pc)
            i2, _ = prog.ins(pc)
            if i2 is not None and i2.name == "RET":
                out.setdefault(pc, set()).add((a + 1) & 0xFF)
                continue
            for st, _ in step(prog, St(pc)):
                if not (i2 is not None and i2.name == "CALL"):
                    todo.append(st.pc)
                else:
                    todo.append((pc + 1) & 0xFF)       # a nested call returns here
    return out


def entries(prog):
    """Address 0 and every label no instruction of the program refers to
    (the entry points a host sets with PC0 / PC1)."""
    targets = prog.referenced
    # an address the previous instruction can fall into is not an entry
    falls = set()
    for a in prog.code:
        ins, _ = prog.ins(a)
        if ins is None or ins.name not in ("BRA", "JMP", "RET", "JMPR", "HALT"):
            falls.add((a + 1) & 0xFF)
    out = {0} if 0 in prog.code else set()
    for a in prog.labels:
        if a in prog.code and a not in targets and a not in falls:
            out.add(a)
    return out


def constants(prog, rets):
    """Forward constant propagation from the entry points: the state that
    holds whenever control is at an address (None where it varies)."""
    at = {}
    todo = []
    for e in sorted(entries(prog)):
        at[e] = St(e)
        todo.append(e)
    while todo:
        pc = todo.pop()
        if pc not in prog.words:
            continue
        for st, _ in step(prog, at[pc], rets):
            old = at.get(st.pc)
            new = st if old is None else _meet_state(old, st)
            if old is None or new.key() != old.key():
                at[st.pc] = new
                todo.append(st.pc)
    return at


# ---------------------------------------------------------------- segments

class Result:
    """One WAITD seen from one anchor."""

    def __init__(self, anchor, waitd, k, slots, period, waits, loop, path):
        self.anchor, self.waitd, self.k, self.slots, self.period = anchor, waitd, k, slots, period
        self.waits, self.loop, self.path = waits, loop, path
        self.status = self.budget = self.budget_min = self.need = None


def _explore(prog, start, rets):
    """State graph from `start` (the state after an anchor) to the next
    deadline instruction. Returns (nodes, edges, terminals): edges[i] = list
    of (j, slots, wait_pc or None); terminals[i] = (waitd pc, period)."""
    index, nodes, edges, term = {start.key(): 0}, [start], [[]], {}
    todo = [0]
    while todo:
        i = todo.pop()
        s = nodes[i]
        ins, _ = prog.ins(s.pc)
        if s.pc not in prog.words:
            continue
        if ins is not None and ins.name in ANCHORS:
            if ins.name == "WAITD":
                term[i] = (s.pc, s.period)
            continue
        w = s.pc if blocks(prog, s.pc) else None
        for st, taken in step(prog, s, rets):
            if s.pc in prog.bound:
                cnt = dict(s.cnt)
                if taken:
                    if cnt.get(s.pc, 0) >= prog.bound[s.pc]:
                        continue
                    cnt[s.pc] = cnt.get(s.pc, 0) + 1
                else:
                    cnt.pop(s.pc, None)
                st = st.with_(cnt=tuple(sorted(cnt.items())))
            k = st.key()
            j = index.get(k)
            if j is None:
                if len(nodes) >= NODE_LIMIT:
                    return None
                j = index[k] = len(nodes)
                nodes.append(st)
                edges.append([])
                todo.append(j)
            edges[i].append((j, cost(prog, s.pc), w))
    return nodes, edges, term


def _sccs(n, edges):
    """Tarjan, iterative; components in reverse topological order."""
    idx, low, on, stack, out = [None] * n, [0] * n, [False] * n, [], []
    counter = 0
    for root in range(n):
        if idx[root] is not None:
            continue
        work = [(root, 0)]
        while work:
            v, ei = work.pop()
            if ei == 0:
                idx[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on[v] = True
            recurse = False
            while ei < len(edges[v]):
                w = edges[v][ei][0]
                ei += 1
                if idx[w] is None:
                    work.append((v, ei))
                    work.append((w, 0))
                    recurse = True
                    break
                if on[w]:
                    low[v] = min(low[v], idx[w])
            if recurse:
                continue
            if low[v] == idx[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on[w] = False
                    comp.append(w)
                    if w == v:
                        break
                out.append(comp)
            if work:
                u = work[-1][0]
                low[u] = min(low[u], low[v])
    return out


def _longest(prog, nodes, edges, term):
    """Per node: {terminal key: (slots or None when unbounded, waits, loop pc, next node)}."""
    best = [None] * len(nodes)
    comp_of = {}
    comps = _sccs(len(nodes), edges)
    for ci, comp in enumerate(comps):
        for v in comp:
            comp_of[v] = ci
    for ci, comp in enumerate(comps):
        cyclic = len(comp) > 1 or any(j == comp[0] for j, _, _ in edges[comp[0]])
        if not cyclic:
            v = comp[0]
            d = {}
            if v in term:
                d[term[v]] = (1, frozenset(), None, None)
            for j, c, w in edges[v]:
                for key, (sl, waits, loop, _) in best[j].items():
                    waits = waits | {w} if w is not None else waits
                    cand = (None if sl is None else sl + c, waits, loop, j)
                    old = d.get(key)
                    if old is None or (old[0] is not None and (cand[0] is None or cand[0] > old[0])):
                        d[key] = (cand[0], waits | old[1] if old else waits, cand[2], j)
                    else:
                        d[key] = (old[0], old[1] | waits, old[2], old[3])
            best[v] = d
            continue
        # a loop the check cannot bound: everything behind it is unbounded
        loop_pc = min((nodes[v].pc for v in comp
                       if any(comp_of[j] == ci and nodes[j].pc <= nodes[v].pc for j, _, _ in edges[v])),
                      default=nodes[comp[0]].pc)
        d = {}
        for v in comp:
            for j, c, w in edges[v]:
                if comp_of[j] == ci:
                    continue
                for key, (sl, waits, loop, _) in best[j].items():
                    old = d.get(key)
                    d[key] = (None, (old[1] if old else frozenset()) | waits | ({w} if w is not None else set()),
                              loop_pc, None)
        for v in comp:
            best[v] = d
    return best


def _path(prog, nodes, best, key, limit=400):
    out, i = [], 0
    while i is not None and len(out) < limit:
        out.append(nodes[i].pc)
        ent = best[i].get(key)
        i = ent[3] if ent else None
    return out


def analyse(words, lines):
    """All (anchor, WAITD) results of a program, worst first per WAITD."""
    prog = Program(words, lines)
    rets = return_sites(prog)
    at = constants(prog, rets)
    results = []
    for a in sorted(prog.code):
        ins, ops = prog.ins(a)
        if ins is None or ins.name not in ANCHORS:
            continue
        base = at.get(a) or St(a)
        for start, _ in step(prog, base.with_(cnt=()), rets):
            g = _explore(prog, start, rets)
            if g is None:
                results.append(Result(a, None, None, None, None, frozenset(), a, []))
                continue
            nodes, edges, term = g
            best = _longest(prog, nodes, edges, term)
            for key, (slots, waits, loop, _) in sorted(best[0].items(), key=lambda kv: kv[0][0]):
                w, period = key
                k = prog.ins(w)[1]["k"]
                results.append(Result(a, w, k, slots, period, waits, loop,
                                      _path(prog, nodes, best, key) if slots is not None else []))
    for r in results:
        _judge(prog, r)
    return prog, results


def _judge(prog, r):
    if r.waitd is None:
        r.status = "LIMIT"
        return
    ins, ops = prog.ins(r.anchor)
    ticks = r.k + (ops["k"] if ins.name == "SETD" else 0)
    ticks_min = ticks - 1 if ins.name == "SETD" else ticks
    if r.slots is None:
        r.status = "UNBOUNDED"
    else:
        if r.period:
            r.budget = ticks * r.period // 2
            r.budget_min = ticks_min * r.period // 2
            # `SETD 0` directly before `WAITD 1` waits 0 to 1 tick by design
            r.status = ("LATE" if r.slots > r.budget else
                        "MARGIN" if r.slots > r.budget_min and ticks_min > 0 else "ok")
        else:
            r.need = -(-2 * r.slots // ticks_min) if ticks_min > 0 else None
            r.status = "period?"
        if r.waits and r.status != "LATE":
            r.status = "WAITS"


FAULTS = ("LATE", "UNBOUNDED", "WAITS", "LIMIT")


def worst(results):
    """One result per WAITD: the most serious, then the one with most slots."""
    rank = {"LIMIT": 6, "UNBOUNDED": 5, "LATE": 4, "WAITS": 3, "MARGIN": 2, "period?": 1, "ok": 0}
    out = {}
    for r in results:
        if r.waitd is None:
            out[("limit", r.anchor)] = r
            continue
        o = out.get(r.waitd)
        margin = lambda x: (rank[x.status], -(x.budget - x.slots) if x.budget is not None and x.slots is not None
                            else (x.slots or 0))
        if o is None or margin(r) > margin(o):
            out[r.waitd] = r
    return [out[k] for k in sorted(out, key=lambda k: (isinstance(k, tuple), k if not isinstance(k, tuple) else k[1]))]


def report(prog, results, verbose=False, name=""):
    """(text, number of faults that carry no waiver)."""
    lines, faults = [], 0

    def where(pc):
        return "line %d (%s)" % (prog.line.get(pc, 0), " ".join(prog.text.get(pc, "?").split(";")[0].split()))

    shown = worst(results)
    for r in shown:
        if r.waitd is None:
            lines.append("  LIMIT      from %s: more than %d states; add `; bound N` to its loops"
                         % (where(r.anchor), NODE_LIMIT))
            faults += 1
            continue
        waiver = prog.waive.get(r.waitd)
        fault = r.status in FAULTS or r.status == "MARGIN"
        tag = r.status + (" (waived)" if waiver and fault else "")
        if r.status in FAULTS and not waiver:
            faults += 1
        if r.slots is None:
            what = "no bound: the loop at %s has none" % where(r.loop)
        else:
            what = "%d slot%s" % (r.slots, "" if r.slots == 1 else "s")
            if r.budget is not None:
                what += " of %d" % r.budget
                if r.budget_min != r.budget:
                    what += " (%d if the tick phase is against it)" % r.budget_min
                what += ", period %d" % r.period
            elif r.need is not None:
                what += ", period unknown: on time for a period of at least %d cycles" % r.need
            else:
                what += ", period unknown"
        lines.append("  %-18s %s: %s, from %s" % (tag, where(r.waitd), what, where(r.anchor)))
        for w in sorted(r.waits):
            lines.append("  %-18s   waits at %s" % ("", where(w)))
        if waiver and fault:
            lines.append("  %-18s   waiver: %s" % ("", waiver))
        if verbose and r.path:
            ls = []
            for pc in r.path:
                if not ls or ls[-1] != prog.line.get(pc):
                    ls.append(prog.line.get(pc))
            lines.append("  %-18s   path (source lines): %s" % ("", " ".join(str(x) for x in ls)))
    n = len([r for r in shown if r.waitd is not None])
    head = "%s%d WAITD checked, %d fault%s" % (name + ": " if name else "", n, faults, "" if faults == 1 else "s")
    return "\n".join([head] + lines), faults
