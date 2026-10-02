#!/usr/bin/env python3
"""Keyer golden model: a cycle-exact behavioural model of the Keyer core.

Written from docs/SEMANTICS.md (the contract) and tools/keyer_isa.py (the
encoding table) alone, independently of the RTL in src/ (DECISIONS D-012).
Section numbers in the comments refer to SEMANTICS.md.

One call to Machine.step() is one core clock cycle c (= Machine.cycle):

  1. Observe. Everything the cycle depends on is read as it is during
     cycle c: the drive registers and the external levels (pad(c)), the
     synchronised levels level(c) = pad(c - 2) and level2(c) = level(c - 2),
     FIFO occupancies, NOW and DEADLINE, the running flags.
  2. Execute. Thread c mod 2, if it is running, evaluates IMEM[PC]. The
     instruction either completes (its PC, register, flag, pin, FIFO and
     timer effects are applied) or blocks (no architectural effect; only the
     private DELAY count advances).
  3. Commit. The timers of both threads tick (6.1), the synchroniser history
     shifts and the cycle counter advances to c + 1.

Host effects (the host_* methods and Thread.soft_reset()) called between
step(c) and step(c + 1) stand for the host interface's effects asserted
during cycle c. They land at the end of c, after the core's commit of that
cycle, so where both write the same state the host wins (2.3, 3.2, 5.3, 8).
Where SEMANTICS has a host effect depend on state during c (a PC write only
if the thread was not running, a push on a full FIFO or a pop on an empty
FIFO ignored), it observes the state as it was during the cycle just
stepped, not the core's commit of that cycle. Before the first step that is
the reset state.
"""

import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyer_isa as isa  # noqa: E402

M8 = 0xFF
M16 = 0xFFFF
IMEM_WORDS = 256
FIFO_DEPTH = 16
NREGS = 8
NPINS = 24


def reached(a, b):
    """SEMANTICS 1: the signed 16-bit difference a - b is zero or positive."""
    return ((a - b) & M16) < 0x8000


def _zf(v):
    """Z flag value for a result (1 when the 16-bit result is zero)."""
    return 0 if v & M16 else 1


def _rev8(v):
    r = 0
    for i in range(8):
        r |= ((v >> i) & 1) << (7 - i)
    return r


_DECODED = {}


def decode(word):
    """keyer_isa.decode(word), cached (the table lookup is a linear search)."""
    word &= M16
    d = _DECODED.get(word)
    if d is None:
        d = _DECODED[word] = isa.decode(word)
    return d


class Retire:
    """One executed slot: the cycle, the thread, its PC, the instruction word
    and whether the instruction completed (False: it blocked)."""

    __slots__ = ("cycle", "tid", "pc", "word", "done")

    def __init__(self, cycle, tid, pc, word, done):
        self.cycle, self.tid, self.pc, self.word, self.done = cycle, tid, pc, word, done

    def __repr__(self):
        return "Retire(cycle=%d, tid=%d, pc=0x%02X, word=0x%04X [%s], %s)" % (
            self.cycle, self.tid, self.pc, self.word, isa.disasm(self.word),
            "done" if self.done else "blocked")


class Thread:
    """Architectural state of one hardware thread (SEMANTICS 3, 4, 6, 7.6, 8)."""

    def __init__(self, tid):
        self.tid = tid
        self.inbox = collections.deque()      # host -> thread, head first
        self.outbox = collections.deque()     # thread -> host, head first
        self.reset()

    def reset(self):
        """Hard reset (3.1)."""
        self.regs = [0] * NREGS
        self.pc = 0
        self.lr = 0
        self.z = 0
        self.c = 0
        self.period = 0
        self.prescale = 0
        self.now = 0
        self.deadline = 0
        self.inbox.clear()
        self.outbox.clear()
        self.running = False
        self.halted = False
        self.blocked = False
        self.delay_left = 0                   # DELAY slots still to occupy (7.6); 0 = none in progress
        self._open_window()

    def soft_reset(self):
        """Host soft reset, CTRL bit RST0/RST1 (3.2). Called between two
        steps, it lands at the end of the cycle just stepped and wins over
        that cycle's commit for everything it clears; PC, registers, running
        and halted are untouched. A host push into a FIFO later in the same
        window is lost (8)."""
        self.lr = 0
        self.z = 0
        self.c = 0
        self.period = 0
        self.prescale = 0
        self.now = 0
        self.deadline = 0
        self.delay_left = 0
        self.inbox.clear()
        self.outbox.clear()
        self._in_cleared = True
        self._out_cleared = True

    def _open_window(self):
        # The host's view of the FIFOs for host calls made after this step:
        # occupancy during the cycle being stepped, then the host's own
        # operations in call order (section 8, "observe, then commit").
        self._in_seen = len(self.inbox)
        self._in_pushed = 0
        self._in_cleared = False
        self._out_seen = len(self.outbox)
        self._out_popped = 0
        self._out_cleared = False

    def __repr__(self):
        return ("T%d pc=%02X lr=%02X z=%d c=%d run=%d halt=%d blk=%d regs=[%s] "
                "timer(period=%d prescale=%d now=%d deadline=%d) delay=%d in=%d out=%d") % (
            self.tid, self.pc, self.lr, self.z, self.c, self.running, self.halted,
            self.blocked, " ".join("%04X" % r for r in self.regs), self.period,
            self.prescale, self.now, self.deadline, self.delay_left,
            len(self.inbox), len(self.outbox))


class Machine:
    """The whole chip as seen by firmware and by the host interface."""

    def __init__(self, trace=False):
        self.imem = [0] * IMEM_WORDS
        self.threads = [Thread(0), Thread(1)]
        self.trace_enabled = bool(trace)
        self.trace = []
        self.pin_events = []
        self.ext_uio = 0xFF          # external level on released uio pins (pull-ups)
        self.ext_ui = 0              # external level on the ui pins
        self.reset()

    def reset(self):
        """Hard reset (3.1). Program memory is preserved."""
        self.cycle = 0
        self.uio_out = 0
        self.uio_oe = 0
        self.od_mask = 0
        self.uo_out = 0              # firmware outputs uo[7:2]; bits 1:0 always 0
        self.irq_en = 0
        for t in self.threads:
            t.reset()
        self._pad1 = 0               # pad(c - 1), pins 0-15
        self._pad2 = 0               # pad(c - 2)
        self._lvl1 = 0               # level(c - 1), all 24 pins
        self._lvl2 = 0               # level(c - 2)
        self._run_seen = (False, False)   # running[t] during the cycle just stepped
        self._pins_at_reset = (0, 0, 0)
        self.trace = []
        self.pin_events = []
        # per-instruction scratch, valid during step()
        self._pc = 0
        self._level = 0
        self._level2 = 0
        self._jump = None
        self._sett = False

    @property
    def cyc(self):
        """The free-running counter CYC during the current cycle (1)."""
        return self.cycle & M16

    # ------------------------------------------------------------ program

    def load(self, words, base=0):
        for i, w in enumerate(words):
            self.imem[(base + i) % IMEM_WORDS] = w & M16

    # ------------------------------------------------------------ pins (5)

    def pad(self):
        """pad(c) for the current cycle as a 24-bit vector (5.1): uio is the
        driven value where uio_oe is set and ext_uio elsewhere, ui is ext_ui,
        uo is uo_out (the firmware outputs; bits 16 and 17 read 0)."""
        oe = self.uio_oe
        uio = ((self.uio_out & oe) | (self.ext_uio & ~oe)) & M8
        return uio | ((self.ext_ui & M8) << 8) | ((self.uo_out & 0xFC) << 16)

    def _pinwrite(self, p, v):
        """pinwrite(p, v), section 5.3."""
        if p < 8:
            bit = 1 << p
            if self.od_mask & bit:
                self.uio_out &= ~bit & M8
                if v:
                    self.uio_oe &= ~bit & M8
                else:
                    self.uio_oe |= bit
            elif v:
                self.uio_out |= bit
            else:
                self.uio_out &= ~bit & M8
        elif 18 <= p < NPINS:
            bit = 1 << (p - 16)
            if v:
                self.uo_out |= bit
            else:
                self.uo_out &= ~bit & M8
        # pins 8-17 (and the non-existent 24-31): no effect

    def _note_pins(self):
        """Record the drive state that is on the pads from cycle self.cycle on."""
        when = self.cycle
        state = (self.uio_out, self.uio_oe, self.uo_out)
        ev = self.pin_events
        if ev and ev[-1][0] == when:          # a later change landing at the same edge
            ev.pop()
        prev = ev[-1][1:] if ev else self._pins_at_reset
        if state != prev:
            ev.append((when,) + state)

    # ------------------------------------------------------------ host interface (10)

    def host_run(self, tid, run):
        """CTRL RUN bit for one thread (10.3, 2.3). Lands after the core's
        commit of the cycle just stepped, so the host's value wins."""
        t = self.threads[tid]
        if run:
            t.running = True
            t.halted = False
        else:
            t.running = False
            t.delay_left = 0

    def host_set_pc(self, tid, pc):
        """PC0/PC1 write: taken only if the thread was not running during the
        cycle just stepped (10.3). Returns whether it was taken."""
        if self._run_seen[tid]:
            return False
        self.threads[tid].pc = pc & M8
        return True

    def host_inbox_push(self, tid, byte):
        """Push one byte into a thread's inbox (10.3, 8). Dropped (returns
        False) if the inbox was full during the cycle just stepped (counting
        the host's earlier pushes in this window), or if it was emptied by a
        FIFOCLR or soft reset in this window."""
        t = self.threads[tid]
        if t._in_cleared or t._in_seen + t._in_pushed >= FIFO_DEPTH or len(t.inbox) >= FIFO_DEPTH:
            return False
        t.inbox.append(byte & M8)
        t._in_pushed += 1
        return True

    def host_outbox_pop(self, tid):
        """Pop the head of a thread's outbox (10.4, 8). Returns None and pops
        nothing if the outbox was empty during the cycle just stepped
        (counting the host's earlier pops in this window): a byte the thread
        pushes in cycle c can be popped after the next step."""
        t = self.threads[tid]
        if t._out_cleared or t._out_seen - t._out_popped <= 0 or not t.outbox:
            return None
        t._out_popped += 1
        return t.outbox.popleft()

    def host_pinmode(self, mask):
        """PINMODE write (10.3, 5.3), applied after the core's pin command of
        the cycle just stepped."""
        v = mask & M8
        self.od_mask = v
        self.uio_oe &= ~v & M8
        self.uio_out &= ~v & M8
        self._note_pins()

    def host_fifo_clear(self, mask):
        """FIFOCLR (10.3): bit 0 inbox 0, bit 1 outbox 0, bit 2 inbox 1, bit 3 outbox 1."""
        for t in self.threads:
            if (mask >> (2 * t.tid)) & 1:
                t.inbox.clear()
                t._in_cleared = True
            if (mask >> (2 * t.tid + 1)) & 1:
                t.outbox.clear()
                t._out_cleared = True

    def status(self):
        """Host STAT register (10.4)."""
        s = 0
        for t in self.threads:
            s |= (int(bool(t.running)) << t.tid) | (int(bool(t.halted)) << (2 + t.tid)) \
                | (int(bool(t.blocked)) << (4 + t.tid))
        return s

    def irq(self):
        """The IRQ output uo[1] during the current cycle (11)."""
        t0, t1 = self.threads
        cond = ((1 if t0.outbox else 0)
                | (1 if t1.outbox else 0) << 1
                | int(bool(t0.halted)) << 2
                | int(bool(t1.halted)) << 3
                | (0 if t0.inbox else 1) << 4
                | (0 if t1.inbox else 1) << 5)
        return 1 if cond & self.irq_en else 0

    # ------------------------------------------------------------ the clock

    def step(self):
        """Advance one core clock cycle. Returns True if the slot owner's
        instruction completed, False if it blocked, True if no thread
        executes in this cycle."""
        c = self.cycle
        th = self.threads
        t = th[c & 1]

        # 1. observe
        th[0]._open_window()
        th[1]._open_window()
        self._run_seen = (bool(th[0].running), bool(th[1].running))
        pad = self.pad()
        level = (self._pad2 & 0xFFFF) | ((self.uo_out & 0xFC) << 16)     # 5.2
        drive = (self.uio_out, self.uio_oe, self.uo_out)

        # 2. execute (2.1: the model equates "executes" with "running")
        done = True
        sett_thread = None
        if t.running:
            pc = t.pc & M8
            word = self.imem[pc] & M16
            ins, ops = decode(word)
            self._pc = pc
            self._level = level
            self._level2 = self._lvl2
            self._jump = None
            self._sett = False
            if ins is not None:
                done = _EXEC[ins.name](self, t, ops)
            # an undefined encoding completes with no effect but PC + 1 (2.2)
            if done:
                t.delay_left = 0                                       # 7.6
                t.pc = ((pc + 1) & M8) if self._jump is None else (self._jump & M8)
                if self._sett:
                    sett_thread = t
            t.blocked = not done                                       # 2.2
            if self.trace_enabled:
                self.trace.append(Retire(c, t.tid, pc, word, done))
        else:
            t.blocked = False

        # 3. commit: timer tick for both threads, running or not (6.1)
        for x in th:
            if x is sett_thread:
                continue
            per = x.period
            if per:
                if x.prescale:
                    x.prescale -= 1
                else:
                    x.prescale = per - 1
                    x.now = (x.now + 1) & M16
        self._pad2, self._pad1 = self._pad1, pad
        self._lvl2, self._lvl1 = self._lvl1, level
        self.cycle = c + 1
        if (self.uio_out, self.uio_oe, self.uo_out) != drive:
            self._note_pins()
        return done

    def run(self, cycles, on_cycle=None):
        """Run `cycles` cycles; on_cycle(machine) is called before each step."""
        for _ in range(cycles):
            if on_cycle is not None:
                on_cycle(self)
            self.step()

    # ------------------------------------------------------------ helpers for instructions

    def _bit(self, p):
        return (self._level >> p) & 1

    def _branch(self, cond, off):
        if cond:
            self._jump = (self._pc + 1 + off) & M8
        return True

    def _other(self, t):
        return self.threads[1 - t.tid]

    @staticmethod
    def _timed(t, ok, timed):
        """Completion rule of a wait and its timeout form (7.2-7.4). Returns
        (done, do_base_effect)."""
        if ok:
            if timed:
                t.c = 0
            return True, True
        if timed and reached(t.now, t.deadline):
            t.c = 1
            return True, False
        return False, False

    # ---- ALU2 (12): rd op= rs --------------------------------------------

    def _x_ADD(self, t, o):
        r = t.regs[o["rd"]] + t.regs[o["rs"]]
        t.regs[o["rd"]] = r & M16
        t.z, t.c = _zf(r), r >> 16
        return True

    def _x_SUB(self, t, o):
        a, b = t.regs[o["rd"]], t.regs[o["rs"]]
        r = (a - b) & M16
        t.regs[o["rd"]] = r
        t.z, t.c = _zf(r), int(a < b)
        return True

    def _x_AND(self, t, o):
        r = t.regs[o["rd"]] & t.regs[o["rs"]]
        t.regs[o["rd"]] = r
        t.z = _zf(r)
        return True

    def _x_OR(self, t, o):
        r = t.regs[o["rd"]] | t.regs[o["rs"]]
        t.regs[o["rd"]] = r
        t.z = _zf(r)
        return True

    def _x_XOR(self, t, o):
        r = t.regs[o["rd"]] ^ t.regs[o["rs"]]
        t.regs[o["rd"]] = r
        t.z = _zf(r)
        return True

    def _x_MOV(self, t, o):
        r = t.regs[o["rs"]]
        t.regs[o["rd"]] = r
        t.z = _zf(r)
        return True

    def _x_CMP(self, t, o):
        a, b = t.regs[o["rd"]], t.regs[o["rs"]]
        t.z, t.c = _zf(a - b), int(a < b)
        return True

    def _x_TST(self, t, o):
        t.z = _zf(t.regs[o["rd"]] & t.regs[o["rs"]])
        return True

    def _x_ADC(self, t, o):
        r = t.regs[o["rd"]] + t.regs[o["rs"]] + t.c
        t.regs[o["rd"]] = r & M16
        t.z, t.c = _zf(r), r >> 16
        return True

    def _x_SBC(self, t, o):
        a, b, cin = t.regs[o["rd"]], t.regs[o["rs"]], t.c
        r = (a - b - cin) & M16
        t.regs[o["rd"]] = r
        t.z, t.c = _zf(r), int(a < b + cin)
        return True

    # ---- ALU1 (12): rd = op rd -------------------------------------------

    def _x_SHL(self, t, o):
        a = t.regs[o["rd"]]
        r = (a << 1) & M16
        t.regs[o["rd"]] = r
        t.z, t.c = _zf(r), a >> 15
        return True

    def _x_SHR(self, t, o):
        a = t.regs[o["rd"]]
        r = a >> 1
        t.regs[o["rd"]] = r
        t.z, t.c = _zf(r), a & 1
        return True

    def _x_RCL(self, t, o):
        a = t.regs[o["rd"]]
        r = ((a << 1) | t.c) & M16
        t.regs[o["rd"]] = r
        t.z, t.c = _zf(r), a >> 15
        return True

    def _x_RCR(self, t, o):
        a = t.regs[o["rd"]]
        r = (a >> 1) | (t.c << 15)
        t.regs[o["rd"]] = r
        t.z, t.c = _zf(r), a & 1
        return True

    def _x_NOT(self, t, o):
        r = ~t.regs[o["rd"]] & M16
        t.regs[o["rd"]] = r
        t.z = _zf(r)
        return True

    def _x_NEG(self, t, o):
        a = t.regs[o["rd"]]
        r = -a & M16
        t.regs[o["rd"]] = r
        t.z, t.c = _zf(r), int(a != 0)
        return True

    def _x_INC(self, t, o):
        r = t.regs[o["rd"]] + 1
        t.regs[o["rd"]] = r & M16
        t.z, t.c = _zf(r), r >> 16
        return True

    def _x_DEC(self, t, o):
        a = t.regs[o["rd"]]
        r = (a - 1) & M16
        t.regs[o["rd"]] = r
        t.z, t.c = _zf(r), int(a == 0)
        return True

    def _x_SWAP(self, t, o):
        a = t.regs[o["rd"]]
        r = ((a & M8) << 8) | (a >> 8)
        t.regs[o["rd"]] = r
        t.z = _zf(r)
        return True

    def _x_REV8(self, t, o):
        a = t.regs[o["rd"]]
        r = (a & 0xFF00) | _rev8(a & M8)
        t.regs[o["rd"]] = r
        t.z = _zf(r)
        return True

    def _x_DJNZ(self, t, o):
        r = (t.regs[o["rd"]] - 1) & M16
        t.regs[o["rd"]] = r
        return self._branch(r != 0, o["off"])

    # ---- immediates (12) ---------------------------------------------------

    def _x_ADDI(self, t, o):
        r = t.regs[o["rd"]] + (o["imm"] & M16)          # sign-extended immediate
        t.regs[o["rd"]] = r & M16
        t.z, t.c = _zf(r), r >> 16
        return True

    def _x_ANDI(self, t, o):
        r = t.regs[o["rd"]] & o["imm"]
        t.regs[o["rd"]] = r
        t.z = _zf(r)
        return True

    def _x_ORI(self, t, o):
        r = t.regs[o["rd"]] | o["imm"]
        t.regs[o["rd"]] = r
        t.z = _zf(r)
        return True

    def _x_XORI(self, t, o):
        r = t.regs[o["rd"]] ^ o["imm"]
        t.regs[o["rd"]] = r
        t.z = _zf(r)
        return True

    def _x_LDI(self, t, o):
        t.regs[o["rd"]] = o["imm"]
        return True

    def _x_LDIH(self, t, o):
        t.regs[o["rd"]] = (t.regs[o["rd"]] & M8) | (o["imm"] << 8)
        return True

    def _x_CMPI(self, t, o):
        a, b = t.regs[o["rs"]], o["imm"]
        t.z, t.c = _zf(a - b), int(a < b)
        return True

    # ---- branches and jumps (2.4, 12) -------------------------------------

    def _x_BRA(self, t, o):
        return self._branch(True, o["off"])

    def _x_BEQ(self, t, o):
        return self._branch(t.z == 1, o["off"])

    def _x_BNE(self, t, o):
        return self._branch(t.z == 0, o["off"])

    def _x_BCS(self, t, o):
        return self._branch(t.c == 1, o["off"])

    def _x_BCC(self, t, o):
        return self._branch(t.c == 0, o["off"])

    def _x_BFE(self, t, o):
        return self._branch(not t.inbox, o["off"])

    def _x_BFNE(self, t, o):
        return self._branch(bool(t.inbox), o["off"])

    def _x_BDR(self, t, o):
        return self._branch(reached(t.now, t.deadline), o["off"])

    def _x_BP0(self, t, o):
        return self._branch(self._bit(o["pin"]) == 0, o["off"])

    def _x_BP1(self, t, o):
        return self._branch(self._bit(o["pin"]) == 1, o["off"])

    def _x_JMP(self, t, o):
        self._jump = o["addr"] & M8
        return True

    def _x_CALL(self, t, o):
        t.lr = (self._pc + 1) & M8
        self._jump = o["addr"] & M8
        return True

    # ---- pins (5.3, 7.2, 12) ----------------------------------------------

    def _x_SET(self, t, o):
        self._pinwrite(o["pin"], 1)
        return True

    def _x_CLR(self, t, o):
        self._pinwrite(o["pin"], 0)
        return True

    def _x_WRC(self, t, o):
        self._pinwrite(o["pin"], t.c)
        return True

    def _x_OEN(self, t, o):
        p = o["pin"]
        if p < 8 and not (self.od_mask >> p) & 1:
            self.uio_oe |= 1 << p
        return True

    def _x_OEF(self, t, o):
        p = o["pin"]
        if p < 8 and not (self.od_mask >> p) & 1:
            self.uio_oe &= ~(1 << p) & M8
        return True

    def _x_OD(self, t, o):
        p = o["pin"]
        if p < 8:
            bit = 1 << p
            self.od_mask |= bit
            self.uio_oe &= ~bit & M8
            self.uio_out &= ~bit & M8
        return True

    def _x_PP(self, t, o):
        p = o["pin"]
        if p < 8:
            self.od_mask &= ~(1 << p) & M8
        return True

    def _wait_pin(self, t, o, kind, timed):
        p = o["pin"]
        lv = self._bit(p)
        if kind == 0:
            ok = lv == 0
        elif kind == 1:
            ok = lv == 1
        else:
            l2 = (self._level2 >> p) & 1
            ok = (lv == 1 and l2 == 0) if kind == 2 else (lv == 0 and l2 == 1)
        return self._timed(t, ok, timed)[0]

    def _x_WT0(self, t, o):
        return self._wait_pin(t, o, 0, False)

    def _x_WT1(self, t, o):
        return self._wait_pin(t, o, 1, False)

    def _x_WTR(self, t, o):
        return self._wait_pin(t, o, 2, False)

    def _x_WTF(self, t, o):
        return self._wait_pin(t, o, 3, False)

    def _x_WT0T(self, t, o):
        return self._wait_pin(t, o, 0, True)

    def _x_WT1T(self, t, o):
        return self._wait_pin(t, o, 1, True)

    def _x_WTRT(self, t, o):
        return self._wait_pin(t, o, 2, True)

    def _x_WTFT(self, t, o):
        return self._wait_pin(t, o, 3, True)

    def _x_RDC(self, t, o):
        t.c = self._bit(o["pin"])
        return True

    def _x_TSTP(self, t, o):
        t.z = 1 - self._bit(o["pin"])
        return True

    def _x_OUTR(self, t, o):
        self._pinwrite(o["pin"], t.regs[o["rs"]] & 1)
        return True

    def _x_INR(self, t, o):
        v = self._bit(o["pin"])
        t.regs[o["rd"]] = v
        t.z = 1 - v
        return True

    # ---- transfer (7.3, 7.4, 8, 12) ---------------------------------------

    def _push(self, t, o, timed):
        done, base = self._timed(t, len(t.outbox) < FIFO_DEPTH, timed)
        if base:
            t.outbox.append(t.regs[o["rs"]] & M8)
        return done

    def _pop(self, t, o, timed):
        done, base = self._timed(t, len(t.inbox) > 0, timed)
        if base:
            t.regs[o["rd"]] = t.inbox.popleft()
        return done

    def _x_PUSH(self, t, o):
        return self._push(t, o, False)

    def _x_POP(self, t, o):
        return self._pop(t, o, False)

    def _x_PUSHT(self, t, o):
        return self._push(t, o, True)

    def _x_POPT(self, t, o):
        return self._pop(t, o, True)

    def _x_PUSHNB(self, t, o):
        if len(t.outbox) < FIFO_DEPTH:
            t.outbox.append(t.regs[o["rs"]] & M8)
            t.c = 1
        else:
            t.c = 0
        return True

    def _x_POPNB(self, t, o):
        if t.inbox:
            t.regs[o["rd"]] = t.inbox.popleft()
            t.c = 1
        else:
            t.c = 0
        return True

    def _x_RDS(self, t, o):
        ni, no = len(t.inbox), len(t.outbox)
        t.regs[o["rd"]] = (int(ni == 0)
                           | int(ni == FIFO_DEPTH) << 1
                           | int(no == 0) << 2
                           | int(no == FIFO_DEPTH) << 3
                           | int(reached(t.now, t.deadline)) << 4
                           | int(bool(self._other(t).running)) << 5
                           | t.tid << 6)
        return True

    def _x_RDCYC(self, t, o):
        t.regs[o["rd"]] = self.cycle & M16
        return True

    def _x_SETT(self, t, o):
        v = t.regs[o["rs"]]
        t.period = v
        t.prescale = (v - 1) if v else 0
        t.now = 0
        t.deadline = 0
        self._sett = True             # no tick for this thread in this cycle (6.1)
        return True

    def _x_RDT(self, t, o):
        t.regs[o["rd"]] = (t.now - t.deadline) & M16
        return True

    def _x_OUTB(self, t, o):
        v = t.regs[o["rs"]]
        for i in range(8):            # od_mask is not changed by pinwrite: order is irrelevant
            self._pinwrite(i, (v >> i) & 1)
        return True

    def _x_INB(self, t, o):
        v = self._level & M8
        t.regs[o["rd"]] = v
        t.z = _zf(v)
        return True

    def _x_INW(self, t, o):
        v = self._level & M16
        t.regs[o["rd"]] = v
        t.z = _zf(v)
        return True

    def _x_OUTOE(self, t, o):
        v = t.regs[o["rs"]] & M8
        od = self.od_mask
        self.uio_oe = (self.uio_oe & od) | (v & ~od & M8)
        return True

    def _x_RDLR(self, t, o):
        t.regs[o["rd"]] = t.lr
        return True

    def _x_JMPR(self, t, o):
        self._jump = t.regs[o["rs"]] & M8
        return True

    # ---- misc (6.2, 7.5, 7.6, 9, 12) --------------------------------------

    def _x_NOP(self, t, o):
        return True

    def _x_HALT(self, t, o):
        t.running = False
        t.halted = True
        return True

    def _x_RET(self, t, o):
        self._jump = t.lr & M8
        return True

    def _x_WAITD(self, t, o):
        target = (t.deadline + o["k"]) & M16
        if reached(t.now, target):
            t.deadline = target
            return True
        return False

    def _x_DELAY(self, t, o):
        # occupies n + 1 slots: the first evaluation loads the count
        n = o["n"]
        if t.delay_left == 0:
            if n == 0:
                return True
            t.delay_left = n
            return False
        t.delay_left -= 1
        return t.delay_left == 0

    def _x_SETC(self, t, o):
        t.c = 1
        return True

    def _x_CLC(self, t, o):
        t.c = 0
        return True

    def _x_START(self, t, o):
        u = self._other(t)
        u.running = True
        u.halted = False
        return True

    def _x_STOP(self, t, o):
        u = self._other(t)
        u.running = False
        u.delay_left = 0
        return True

    def _x_SETD(self, t, o):
        t.deadline = (t.now + o["k"]) & M16
        return True


# Dispatch table: one handler per instruction of the encoding table. A name
# without a handler is an error at import time, so a new instruction in
# keyer_isa.py cannot be silently executed as a NOP.
_EXEC = {ins.name: getattr(Machine, "_x_" + ins.name) for ins in isa.INSTRUCTIONS}


# ---------------------------------------------------------------- CLI

def _read_image(path):
    """A hex image (whitespace-separated 16-bit words, `@addr` to move,
    `//` and `#` comments) or, for a .s file, assembler source."""
    if path.endswith(".s"):
        import keyerasm
        with open(path) as f:
            words, syms, _ = keyerasm.assemble(f.read())
        return keyerasm.to_list(words), syms
    image = [0] * IMEM_WORDS
    addr = 0
    with open(path) as f:
        for line in f:
            line = line.split("//")[0].split("#")[0]
            for tok in line.split():
                if tok.startswith("@"):
                    addr = int(tok[1:], 16)
                else:
                    image[addr % IMEM_WORDS] = int(tok, 16) & M16
                    addr += 1
    return image, {}


def _bytes(text):
    return [int(x, 0) & M8 for x in text.split(",") if x.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Keyer golden model: run a program for N cycles "
                                             "and print the pin events and the thread state.")
    ap.add_argument("image", help="hex image (one word per token, @addr allowed) or a .s source file")
    ap.add_argument("-n", "--cycles", type=int, default=1000, help="cycles to run (default 1000)")
    ap.add_argument("--run", type=lambda s: int(s, 0), default=1, help="RUN mask: bit 0 T0, bit 1 T1 (default 1)")
    ap.add_argument("--pc0", default="0", help="start PC of T0 (number or symbol of a .s file)")
    ap.add_argument("--pc1", default="0", help="start PC of T1 (number or symbol of a .s file)")
    ap.add_argument("--inbox0", default="", help="comma-separated bytes for T0's inbox")
    ap.add_argument("--inbox1", default="", help="comma-separated bytes for T1's inbox")
    ap.add_argument("--ext-ui", type=lambda s: int(s, 0), default=0, help="external ui level")
    ap.add_argument("--ext-uio", type=lambda s: int(s, 0), default=0xFF, help="external uio level")
    ap.add_argument("--trace", action="store_true", help="print every executed slot")
    args = ap.parse_args(argv)

    words, syms = _read_image(args.image)
    m = Machine(trace=args.trace)
    m.load(words)
    m.ext_ui, m.ext_uio = args.ext_ui & M8, args.ext_uio & M8
    for tid, spec in ((0, args.pc0), (1, args.pc1)):
        pc = syms[spec] if spec in syms else int(spec, 0)
        m.host_set_pc(tid, pc)
    for tid, text in ((0, args.inbox0), (1, args.inbox1)):
        for b in _bytes(text):
            if not m.host_inbox_push(tid, b):
                print("inbox %d full, byte 0x%02X dropped" % (tid, b))
    for tid in (0, 1):
        if (args.run >> tid) & 1:
            m.host_run(tid, True)
    m.run(args.cycles)

    if args.trace:
        for r in m.trace:
            print(r)
    print("pin events (cycle: uio_out uio_oe uo_out):")
    for cyc, out, oe, uo in m.pin_events:
        print("  %8d: %02X %02X %02X" % (cyc, out, oe, uo))
    print("after %d cycles: STAT=%02X IRQ=%d od_mask=%02X" % (m.cycle, m.status(), m.irq(), m.od_mask))
    for t in m.threads:
        print(" ", t)
        if t.outbox:
            print("    outbox:", " ".join("%02X" % b for b in t.outbox))
    return 0


if __name__ == "__main__":
    sys.exit(main())
