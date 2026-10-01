#!/usr/bin/env python3
"""Loom instruction-set simulator (cycle-exact golden model).

The simulator advances one core clock cycle per step(). Thread 0 executes on
even cycles, thread 1 on odd cycles. Everything observable (register
writes, pin drive registers, timers, FIFOs) updates at the end of the cycle,
matching the RTL's registered outputs.

Pin timing, matching the RTL's two-flop input synchroniser:
  pad(c)   = pad level during cycle c (our drive if enabled, else external)
  level(c) = pad(c-2) for pins 0-15; the driven register for pins 16-23
  an edge at cycle c compares level(c) with level(c-2)

Usage as a library:
  m = Machine(); m.load(words); m.host_run(0, True)
  for _ in range(1000): m.step()
"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loom_isa as isa  # noqa: E402

M16 = 0xFFFF
FIFO_DEPTH = 16
IMEM_WORDS = 256
PC_MASK = IMEM_WORDS - 1
RESERVED_UO = 0x03          # uo[0] = MISO, uo[1] = IRQ: owned by the host interface


class Thread:
    def __init__(self, tid):
        self.tid = tid
        self.reset()

    def reset(self):
        self.regs = [0] * 8
        self.pc = 0
        self.lr = 0
        self.z = 0
        self.c = 0
        self.period = 0
        self.count = 0
        self.tick = 0
        self.inbox = deque()
        self.outbox = deque()
        self.running = False
        self.halted = False
        self.delay_left = 0
        self.blocked = False

    def soft_reset(self):
        """Host RSTn: timer, flags, LR, FIFOs; PC and registers kept."""
        self.lr = 0
        self.z = self.c = 0
        self.period = self.count = self.tick = 0
        self.inbox.clear()
        self.outbox.clear()
        self.delay_left = 0


class Retire:
    __slots__ = ("cycle", "tid", "pc", "word", "done")

    def __init__(self, cycle, tid, pc, word, done):
        self.cycle, self.tid, self.pc, self.word, self.done = cycle, tid, pc, word, done

    def __repr__(self):
        return "<%d T%d pc=%02X %s %s>" % (self.cycle, self.tid, self.pc, isa.disasm(self.word),
                                          "" if self.done else "(blocked)")


class Machine:
    def __init__(self, trace=False):
        self.imem = [0] * IMEM_WORDS
        self.threads = [Thread(0), Thread(1)]
        self.cycle = 0
        self.cyc = 0
        self.uio_out = 0
        self.uio_oe = 0
        self.od_mask = 0
        self.uo_out = 0
        self.ext_uio = 0xFF          # external level when we are not driving (pull-ups)
        self.ext_ui = 0
        self.pad_hist = [0, 0]       # [pad(c-2), pad(c-1)]: the two synchroniser stages
        self.lvl_hist = [0, 0]       # [level(c-2), level(c-1)]: for edge detection
        self.trace_enabled = trace
        self.trace = []
        self.pin_events = []         # (cycle, uio_out, uio_oe, uo_out) whenever drive changes
        self.irq_en = 0

    # ---- host side -------------------------------------------------------
    def load(self, words, base=0):
        for i, w in enumerate(words):
            self.imem[(base + i) & PC_MASK] = w & M16

    def host_run(self, tid, run):
        t = self.threads[tid]
        if run:
            t.running = True
            t.halted = False
        else:
            t.running = False
            t.delay_left = 0

    def host_set_pc(self, tid, pc):
        t = self.threads[tid]
        if not t.running:
            t.pc = pc & PC_MASK

    def host_inbox_push(self, tid, byte):
        t = self.threads[tid]
        if len(t.inbox) < FIFO_DEPTH:
            t.inbox.append(byte & 0xFF)
            return True
        return False

    def host_outbox_pop(self, tid):
        t = self.threads[tid]
        return t.outbox.popleft() if t.outbox else None

    def host_pinmode(self, mask):
        self.od_mask = mask & 0xFF
        self.uio_oe &= ~self.od_mask & 0xFF
        self.uio_out &= ~self.od_mask & 0xFF

    def host_fifo_clear(self, mask):
        for tid in (0, 1):
            if mask & (1 << (2 * tid)):
                self.threads[tid].inbox.clear()
            if mask & (2 << (2 * tid)):
                self.threads[tid].outbox.clear()

    def status(self):
        s = 0
        for i, t in enumerate(self.threads):
            s |= (t.running << i) | (t.halted << (2 + i)) | (t.blocked << (4 + i))
        return s

    def irq(self):
        c = 0
        for i, t in enumerate(self.threads):
            c |= (bool(t.outbox) << i) | (t.halted << (2 + i)) | ((not t.inbox) << (4 + i))
        return 1 if (c & self.irq_en) else 0

    # ---- pins --------------------------------------------------------------
    def pad(self):
        """24-bit pad vector during the current cycle."""
        uio = (self.uio_oe & self.uio_out) | (~self.uio_oe & self.ext_uio & 0xFF)
        return uio | (self.ext_ui << 8) | (self.uo_out << 16)

    def _level_now(self):
        synced = self.pad_hist[0] & 0xFFFF        # pad(c-2), what the second sync flop holds
        return synced | (self.uo_out << 16)

    def _pinwrite(self, p, v, st):
        v &= 1
        if p < 8:
            bit = 1 << p
            if self.od_mask & bit:
                st["uio_oe"] = (st["uio_oe"] & ~bit) | (0 if v else bit)
                st["uio_out"] &= ~bit
            else:
                st["uio_out"] = (st["uio_out"] & ~bit) | (bit if v else 0)
        elif p >= 16:
            bit = 1 << (p - 16)
            if not (RESERVED_UO & bit):
                st["uo_out"] = (st["uo_out"] & ~bit) | (bit if v else 0)

    # ---- execution ---------------------------------------------------------
    def step(self):
        c = self.cycle
        # pad level during this cycle, then the synchronised level vector
        pad = self.pad()
        level = self._level_now()                  # pad(c-2) for pins 0-15
        level2 = self.lvl_hist[0]                  # level(c-2)
        self.pad_hist = [self.pad_hist[1], pad]
        self.lvl_hist = [self.lvl_hist[1], level]

        tid = c & 1
        t = self.threads[tid]
        st = {"uio_out": self.uio_out, "uio_oe": self.uio_oe, "uo_out": self.uo_out,
              "od_mask": self.od_mask}
        sett = None       # (thread, period) if SETT executed
        clr_tick = False
        done = False
        t.blocked = False
        if t.running:
            word = self.imem[t.pc & PC_MASK]
            ins, ops = isa.decode(word)
            done = self._exec(t, ins, ops, word, level, level2, st)
            if done is None:
                done = True
            # bookkeeping for the timer-affecting instructions
            if ins is not None:
                if ins.name == "SETT":
                    sett = t.regs[ops["rs"]]
                elif ins.name in ("WAITT", "CLRT") and done:
                    clr_tick = True
            t.blocked = not done
            if self.trace_enabled:
                self.trace.append(Retire(c, tid, t.pc, word, done))
            if done:
                t.pc = self._next_pc if self._next_pc is not None else (t.pc + 1) & PC_MASK
                t.delay_left = 0
        # timers (both threads, every cycle)
        for th in self.threads:
            hit = 0
            if th is t and sett is not None:
                th.period = sett
                th.count = (sett - 1) & M16 if sett else 0
                th.tick = 0
                continue
            if th.period:
                if th.count == 0:
                    hit = 1
                    th.count = (th.period - 1) & M16
                else:
                    th.count -= 1
            clear = clr_tick if th is t else False
            th.tick = 1 if hit else (0 if clear else th.tick)
        # commit pin drive registers
        changed = (st["uio_out"], st["uio_oe"], st["uo_out"]) != (self.uio_out, self.uio_oe, self.uo_out)
        self.uio_out, self.uio_oe, self.uo_out, self.od_mask = st["uio_out"], st["uio_oe"], st["uo_out"], st["od_mask"]
        if changed:
            self.pin_events.append((c + 1, self.uio_out, self.uio_oe, self.uo_out))
        self.cyc = (self.cyc + 1) & M16
        self.cycle += 1
        return done

    def run(self, cycles, on_cycle=None):
        for _ in range(cycles):
            if on_cycle:
                on_cycle(self)
            self.step()

    def _flags_add(self, t, a, b, cin=0):
        r = a + b + cin
        t.c = 1 if r > M16 else 0
        r &= M16
        t.z = 1 if r == 0 else 0
        return r

    def _flags_sub(self, t, a, b, bin_=0):
        r = a - b - bin_
        t.c = 1 if r < 0 else 0
        r &= M16
        t.z = 1 if r == 0 else 0
        return r

    def _setz(self, t, r):
        t.z = 1 if (r & M16) == 0 else 0
        return r & M16

    def _exec(self, t, ins, ops, word, level, level2, st):
        """Execute one slot. Returns True if the instruction completed."""
        self._next_pc = None
        if ins is None:
            return True                       # illegal: treated as NOP
        n = ins.name
        R = t.regs
        other = self.threads[1 - t.tid]
        lv = lambda p: (level >> p) & 1       # noqa: E731

        if ins.major == 0x0:
            d, s = ops["rd"], ops["rs"]
            a, b = R[d], R[s]
            if n == "ADD":
                R[d] = self._flags_add(t, a, b)
            elif n == "SUB":
                R[d] = self._flags_sub(t, a, b)
            elif n == "AND":
                R[d] = self._setz(t, a & b)
            elif n == "OR":
                R[d] = self._setz(t, a | b)
            elif n == "XOR":
                R[d] = self._setz(t, a ^ b)
            elif n == "MOV":
                R[d] = self._setz(t, b)
            elif n == "CMP":
                self._flags_sub(t, a, b)
            elif n == "TST":
                self._setz(t, a & b)
            elif n == "ADC":
                R[d] = self._flags_add(t, a, b, t.c)
            elif n == "SBC":
                R[d] = self._flags_sub(t, a, b, t.c)
            return True
        if ins.major == 0x1:
            d = ops["rd"]
            a = R[d]
            if n == "DJNZ":
                R[d] = (a - 1) & M16
                if R[d] != 0:
                    self._next_pc = (t.pc + 1 + ops["off"]) & PC_MASK
                return True
            if n == "SHL":
                t.c = (a >> 15) & 1
                R[d] = self._setz(t, a << 1)
            elif n == "SHR":
                t.c = a & 1
                R[d] = self._setz(t, a >> 1)
            elif n == "RCL":
                cin = t.c
                t.c = (a >> 15) & 1
                R[d] = self._setz(t, (a << 1) | cin)
            elif n == "RCR":
                cin = t.c
                t.c = a & 1
                R[d] = self._setz(t, (a >> 1) | (cin << 15))
            elif n == "NOT":
                R[d] = self._setz(t, ~a)
            elif n == "NEG":
                R[d] = self._flags_sub(t, 0, a)
            elif n == "INC":
                R[d] = self._flags_add(t, a, 1)
            elif n == "DEC":
                R[d] = self._flags_sub(t, a, 1)
            elif n == "SWAP":
                R[d] = self._setz(t, ((a & 0xFF) << 8) | (a >> 8))
            elif n == "REV8":
                lo = int("{:08b}".format(a & 0xFF)[::-1], 2)
                R[d] = self._setz(t, (a & 0xFF00) | lo)
            return True
        if n == "ADDI":
            R[ops["rd"]] = self._flags_add(t, R[ops["rd"]], ops["imm"] & M16)
            return True
        if n == "ANDI":
            R[ops["rd"]] = self._setz(t, R[ops["rd"]] & ops["imm"])
            return True
        if n == "ORI":
            R[ops["rd"]] = self._setz(t, R[ops["rd"]] | ops["imm"])
            return True
        if n == "XORI":
            R[ops["rd"]] = self._setz(t, R[ops["rd"]] ^ ops["imm"])
            return True
        if n == "LDI":
            R[ops["rd"]] = ops["imm"]
            return True
        if n == "LDIH":
            R[ops["rd"]] = (R[ops["rd"]] & 0xFF) | (ops["imm"] << 8)
            return True
        if n == "CMPI":
            self._flags_sub(t, R[ops["rs"]], ops["imm"])
            return True
        if ins.major == 0x9:
            cond = n[1:]
            take = {"RA": True, "EQ": t.z == 1, "NE": t.z == 0, "CS": t.c == 1, "CC": t.c == 0,
                    "FE": not t.inbox, "FNE": bool(t.inbox), "TP": t.tick == 1}[cond]
            if take:
                self._next_pc = (t.pc + 1 + ops["off"]) & PC_MASK
            return True
        if ins.major == 0xA:
            if lv(ops["pin"]) == ins.sub:
                self._next_pc = (t.pc + 1 + ops["off"]) & PC_MASK
            return True
        if n == "JMP":
            self._next_pc = ops["addr"] & PC_MASK
            return True
        if n == "CALL":
            t.lr = (t.pc + 1) & PC_MASK
            self._next_pc = ops["addr"] & PC_MASK
            return True
        if ins.major == 0xC:
            p = ops["pin"]
            bit = 1 << p
            if n == "SET":
                self._pinwrite(p, 1, st)
            elif n == "CLR":
                self._pinwrite(p, 0, st)
            elif n == "OEN":
                if p < 8 and not (st["od_mask"] & bit):
                    st["uio_oe"] |= bit
            elif n == "OEF":
                if p < 8 and not (st["od_mask"] & bit):
                    st["uio_oe"] &= ~bit
            elif n == "OD":
                if p < 8:
                    st["od_mask"] |= bit
                    st["uio_oe"] &= ~bit
                    st["uio_out"] &= ~bit
            elif n == "PP":
                if p < 8:
                    st["od_mask"] &= ~bit
            elif n == "WT0":
                return lv(p) == 0
            elif n == "WT1":
                return lv(p) == 1
            elif n == "WTR":
                return lv(p) == 1 and ((level2 >> p) & 1) == 0
            elif n == "WTF":
                return lv(p) == 0 and ((level2 >> p) & 1) == 1
            elif n == "WRC":
                self._pinwrite(p, t.c, st)
            elif n == "RDC":
                t.c = lv(p)
            elif n == "TSTP":
                t.z = 1 if lv(p) == 0 else 0
            return True
        if n == "OUTR":
            self._pinwrite(ops["pin"], R[ops["rs"]] & 1, st)
            return True
        if n == "INR":
            R[ops["rd"]] = self._setz(t, lv(ops["pin"]))
            return True
        if ins.major == 0xE:
            r = ops.get("rs", ops.get("rd"))
            if n == "PUSH":
                if len(t.outbox) >= FIFO_DEPTH:
                    return False
                t.outbox.append(R[r] & 0xFF)
            elif n == "POP":
                if not t.inbox:
                    return False
                R[r] = t.inbox.popleft()
            elif n == "RDS":
                s = (int(not t.inbox) | (int(len(t.inbox) >= FIFO_DEPTH) << 1)
                     | (int(not t.outbox) << 2) | (int(len(t.outbox) >= FIFO_DEPTH) << 3)
                     | (t.tick << 4) | (int(other.running) << 5) | (t.tid << 6))
                R[r] = s
            elif n == "RDCYC":
                R[r] = self.cyc
            elif n == "SETT":
                pass  # handled by step()
            elif n == "RDT":
                R[r] = t.count
            elif n == "PUSHNB":
                if len(t.outbox) < FIFO_DEPTH:
                    t.outbox.append(R[r] & 0xFF)
                    t.c = 1
                else:
                    t.c = 0
            elif n == "POPNB":
                if t.inbox:
                    R[r] = t.inbox.popleft()
                    t.c = 1
                else:
                    t.c = 0
            elif n == "OUTB":
                for i in range(8):
                    self._pinwrite(i, (R[r] >> i) & 1, st)
            elif n == "INB":
                R[r] = self._setz(t, level & 0xFF)
            elif n == "INW":
                R[r] = self._setz(t, level & 0xFFFF)
            elif n == "OUTOE":
                st["uio_oe"] = (st["uio_oe"] & st["od_mask"]) | (R[r] & 0xFF & ~st["od_mask"])
            elif n == "RDLR":
                R[r] = t.lr
            elif n == "JMPR":
                self._next_pc = R[r] & PC_MASK
            return True
        if ins.major == 0xF:
            if n == "NOP":
                pass
            elif n == "HALT":
                t.running = False
                t.halted = True
            elif n == "RET":
                self._next_pc = t.lr & PC_MASK
            elif n == "WAITT":
                return t.tick == 1
            elif n == "DELAY":
                if t.delay_left == 0:
                    if ops["n"] == 0:
                        return True
                    t.delay_left = ops["n"]
                    return False
                t.delay_left -= 1
                return t.delay_left == 0
            elif n == "SETC":
                t.c = 1
            elif n == "CLC":
                t.c = 0
            elif n == "START":
                other.running = True
                other.halted = False
            elif n == "STOP":
                other.running = False
                other.delay_left = 0
            elif n == "CLRT":
                pass  # handled by step()
            return True
        return True


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Loom ISS")
    ap.add_argument("hexfile", help="$readmemh image from loomasm.py")
    ap.add_argument("-c", "--cycles", type=int, default=10000)
    ap.add_argument("--t1", action="store_true", help="also start thread 1 (PC=--pc1)")
    ap.add_argument("--pc1", type=lambda s: int(s, 0), default=0)
    ap.add_argument("--inbox", default="", help="comma-separated bytes preloaded into T0 inbox")
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args(argv)
    words = [int(x, 16) for x in open(a.hexfile).read().split()]
    m = Machine(trace=a.trace)
    m.load(words)
    for b in filter(None, a.inbox.split(",")):
        m.host_inbox_push(0, int(b, 0))
    m.host_run(0, True)
    if a.t1:
        m.host_set_pc(1, a.pc1)
        m.host_run(1, True)
    m.run(a.cycles)
    if a.trace:
        for r in m.trace:
            print(r)
    print("pin events (cycle, uio_out, uio_oe, uo_out):")
    for e in m.pin_events[:200]:
        print("  %6d  %02X %02X %02X" % e)
    for t in m.threads:
        print("T%d pc=%02X regs=%s Z=%d C=%d outbox=%s %s" % (
            t.tid, t.pc, ["%04X" % r for r in t.regs], t.z, t.c, list(t.outbox),
            "halted" if t.halted else ("running" if t.running else "stopped")))


if __name__ == "__main__":
    main()
