#!/usr/bin/env python3
"""Keyer golden model: a cycle-exact behavioural model of the Keyer core.

Written from docs/SEMANTICS.md (the contract, version 0.4) and
tools/keyer_isa.py (the encoding table) alone, independently of the RTL in
src/ (DECISIONS D-012). Section numbers in the comments refer to
SEMANTICS.md.

One call to Machine.step() is one core clock cycle c (= Machine.cycle):

  1. Observe. Everything the cycle depends on is read as it is during
     cycle c: the drive registers and the external levels (pad(c)), the
     synchronised levels level(c) = pad(c - 2) and level2(c) = level(c - 2),
     FIFO occupancies, NOW and DEADLINE, the running flags, the state of the
     capture and replay engines, and whether the memory port was used in
     cycle c - 1 (by the host, which the caller flags by setting
     host_port_busy before the step, or by an engine).
  2. Execute (2.1). Thread c mod 2 executes if it is running and its fetch is
     valid, i.e. the port was not used in cycle c - 1. It evaluates IMEM[PC];
     the instruction either completes (its PC, register, flag, pin, FIFO and
     timer effects are applied) or blocks (no architectural effect; only the
     private DELAY count advances). Machine.executed records whether it
     executed.
  3. Engines (14). The port is free in c if the host does not use it and
     thread (c + 1) mod 2 is not running during c (14.1). The capture engine
     triggers or records on the group nibble of level(c) and, in a free
     cycle, writes its oldest queued entry to IMEM; otherwise the replay
     engine may fetch. The replay engine applies its head entry with
     pinwrite after the core's pin command (14.7). The serializer (15) then
     runs its receiver and, in a symbol tick of the owner's timer (period and
     prescale sampled in step 1), its transmitter, on its state as it was
     during c; its pin writes come after the replay engine's (5.3). The
     serializer instructions only record their effect during step 2, and it
     is merged here (a SERCFG overrides the engine, 15.2). The CR_CTRL
     actions of the cycle (CAPC) are applied last.
  4. Commit. The timers of both threads tick (6.1), the synchroniser history
     shifts and the cycle counter advances to c + 1.

Host effects (the host_* methods and Thread.soft_reset()) called between
step(c) and step(c + 1) stand for the host interface's effects asserted
during cycle c. They land at the end of c, after the core's commit of that
cycle, so where both write the same state the host wins (2.3, 3.2, 5.3, 8).
Where SEMANTICS has a host effect depend on state during c (a PC write only
if the thread was not running, a push on a full FIFO or a pop on an empty
FIFO ignored, the capture count sampled by a replay START), it observes the
state as it was during the cycle just stepped, not the core's commit of
that cycle. Before the first step that is the reset state. host_cr_ctrl()
is merged with a CAPC committed in the same cycle (ARM over DISARM, START
over STOP). The configuration writes (host_cap_cfg, host_cap_buf,
host_rep_cfg, host_rep_buf) take effect at once, so a test may write the
configuration and the control byte between the same two steps (in silicon
they are separate SPI transactions). The one visible consequence: cap_prev
during the first armed cycle is then still the nibble of the previous
group, so with a non-zero trigger mask a group change should be written at
least one step before ARM.

Cycle 0. SEMANTICS 2.1 makes the fetch for cycle 0 invalid. In silicon no
thread runs during cycle 0, so that rule cannot be observed. The model
treats host calls made before the first step as landing at the end of a
cycle -1 with a valid fetch, so a thread started that way executes at
cycle 0 (docs/spec-questions.md Q10).
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

# Capture and replay (14)
ENTRY_DT_MAX = 0xFFF         # the 12-bit delta field of an entry {delta[11:0], pins[3:0]}
QUEUE_DEPTH = 2              # capture write queue (14.4)
PREFETCH_DEPTH = 2           # replay prefetch buffer (14.6)
CR_ARM, CR_DISARM, CR_START, CR_STOP = 1, 2, 4, 8     # CR_CTRL / CAPC action bits (14.2, 14.5)

# Serializer (15)
SER_TX_IDLE, SER_TX_DATA, SER_TX_CRC, SER_TX_TAIL = 0, 1, 2, 3    # tx_state
SER_RX_HUNT, SER_RX_DATA = 0, 1                                   # rx_state
SER_SYNC_NRZI, SER_SYNC_MAN = 0x80, 0xD5     # receive sync byte, mode 1 / mode 2 (15.5)
SER_CRC5_POLY, SER_CRC5_INIT, SER_CRC5_RES = 0x14, 0x1F, 0x06
SER_POLY_M = (0x0000A001, 0xEDB88320)        # indexed by crc32 = cfg[3] (15.1)
SER_INIT_M = (0x0000FFFF, 0xFFFFFFFF)
SER_RES_M = (0x0000B001, 0xDEBB20E3)
SER_W = (16, 32)
_SER_CFG, _SER_TX, _SER_RXB, _SER_RXE = 1, 2, 3, 4  # the instruction's serializer effect of a cycle


def reached(a, b):
    """SEMANTICS 1: the signed 16-bit difference a - b is zero or positive."""
    return ((a - b) & M16) < 0x8000


def _zf(v):
    """Z flag value for a result (1 when the 16-bit result is zero)."""
    return 0 if v & M16 else 1


def _crc_step(r, b, poly):
    """step(r, b, POLY) of 15.1: reflected CRC, one bit."""
    fb = (r ^ b) & 1
    r >>= 1
    return r ^ poly if fb else r


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


class CaptureReplay:
    """The capture and replay engine pair (SEMANTICS 14). Attribute names
    match the RTL's u_cr signals (13); every value is an int. All of it is
    reset by rst_n (Machine.reset()) and untouched by a thread's soft reset.
    The engine's behaviour per cycle is in Machine._cr_cycle() and
    Machine._cr_control(), because it uses the pins and the program memory."""

    CONFIG = ("cap_group", "cap_mask", "cap_tpat", "cap_tmask", "cap_base", "cap_len",
              "rep_group", "rep_mask", "rep_base", "rep_len")

    def __init__(self):
        self.queue = collections.deque()      # capture entries not yet written, oldest first (14.4)
        self.pf = collections.deque()         # replay prefetch, the head (oldest) first (14.6)
        self.reset()

    def reset(self):
        """rst_n (14): everything to 0."""
        for name in self.CONFIG:
            setattr(self, name, 0)
        # capture (14.2-14.4)
        self.cap_armed = 0
        self.cap_trig = 0
        self.cap_done = 0
        self.cap_ovf = 0
        self.cap_was_armed = 0
        self.cap_last = 0            # s() of the last entry produced
        self.cap_prev = 0            # nib() of the previous cycle, at all times
        self.cap_dt = 0              # cycles since the cycle of the last entry
        self.cap_n = 0               # entries produced since ARM (lost ones included)
        self.cap_w = 0               # entries written to IMEM since ARM
        self.queue.clear()
        # replay (14.5-14.7)
        self.rep_active = 0
        self.rep_done = 0
        self.rep_under = 0
        self.rep_k = 0               # entries applied since START
        self.rep_f = 0               # entries fetched since START
        self.rep_dt = 0              # cycles since the cycle of the last apply
        self.rep_n = 0               # n_rep, latched at START (14.5)
        self.pf.clear()
        self.pf_inflight = 0         # 1 during the cycle after a fetch (14.6)
        self.pf_word = 0             # the word fetched, valid while pf_inflight

    @property
    def q_count(self):
        """Entries in the capture queue (0..2)."""
        return len(self.queue)

    @property
    def pf_count(self):
        """Entries held in the replay prefetch (0..2); a word in flight is not counted."""
        return len(self.pf)

    def cap_active(self):
        """Capture active (14.4): armed, or entries still queued."""
        return 1 if (self.cap_armed or self.queue) else 0

    def status(self):
        """The CR_CTRL read byte (14.8)."""
        return (self.cap_active()
                | self.cap_trig << 1
                | self.cap_done << 2
                | self.cap_ovf << 3
                | self.rep_active << 4
                | self.rep_done << 5
                | self.rep_under << 6)

    def counts(self):
        """The CR_COUNT read bytes (14.8): entries recorded, entries applied."""
        return self.cap_w, self.rep_k

    def __repr__(self):
        return ("CR cap(armed=%d trig=%d done=%d ovf=%d last=%X prev=%X dt=%d n=%d w=%d q=%s) "
                "rep(active=%d done=%d under=%d k=%d f=%d dt=%d n=%d pf=%s inflight=%d)") % (
            self.cap_armed, self.cap_trig, self.cap_done, self.cap_ovf, self.cap_last,
            self.cap_prev, self.cap_dt, self.cap_n, self.cap_w,
            [("%04X" % e) for e in self.queue], self.rep_active, self.rep_done,
            self.rep_under, self.rep_k, self.rep_f, self.rep_dt, self.rep_n,
            [("%04X" % e) for e in self.pf], self.pf_inflight)


class Serializer:
    """The serializer engine (SEMANTICS 15). Attribute names match the RTL's
    u_ser signals (13); every value is an int. All of it is reset to 0 by
    rst_n (Machine.reset()); a thread's soft reset, RUN, HALT and STOP do not
    touch it. The per-cycle behaviour is in Machine._ser_cycle(), because it
    uses the pins, the levels and the owner's timer."""

    FIELDS = ("cfg", "owner",
              "tx_hold", "tx_hold_c", "tx_full", "tx_state", "tx_sh", "tx_c", "tx_app",
              "tx_n", "tx_half", "tx_bit", "tx_ones", "tx_line", "crc_m", "crc5",
              "rx_state", "rx_sh", "rx_n", "rx_ones", "rx_psym", "rx_last", "rx_cnt",
              "rx_w", "rx_first", "rx_hold", "rx_valid", "rx_end", "rx_ovr", "rx_serr",
              "rx_ferr", "rx_c5ok", "rx_cok")

    def __init__(self):
        self.reset()

    def reset(self):
        """rst_n (3.1, 15): every register 0."""
        for name in self.FIELDS:
            setattr(self, name, 0)

    def configure(self, cfg, owner):
        """A committing SERCFG (15.2): cfg and owner written, every other
        register 0 except rx_sh = 0xFF and rx_w = 1; rx_last is not touched
        here (it follows 15.1 in every cycle)."""
        rx_last = self.rx_last
        self.reset()
        self.cfg = cfg & M8
        self.owner = owner & 1
        self.rx_sh = M8
        self.rx_w = 1
        self.rx_last = rx_last

    def snapshot(self):
        """A copy of the state, read as the state during the cycle."""
        o = Serializer.__new__(Serializer)
        o.__dict__.update(self.__dict__)
        return o

    # configuration fields (15.1)
    @property
    def mode(self):
        return self.cfg & 3

    def status(self):
        """status(c), section 15.7."""
        return (self.tx_full
                | int(self.tx_state != SER_TX_IDLE) << 1
                | self.rx_valid << 2
                | int(self.rx_state == SER_RX_DATA) << 3
                | self.rx_end << 4
                | self.rx_c5ok << 5
                | self.rx_cok << 6
                | self.rx_ovr << 7
                | self.rx_serr << 8
                | self.rx_ferr << 9)

    def __repr__(self):
        return "SER " + " ".join("%s=%X" % (n, getattr(self, n)) for n in self.FIELDS)


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
        self.cr = CaptureReplay()    # capture and replay engines (14)
        self.ser = Serializer()      # serializer engine (15)
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
        self.cr.reset()
        self.ser.reset()
        self._ser_cmd = None         # the serializer effect of the instruction of this cycle (15.2)
        self.host_port_busy = False  # set by the caller before a step in whose cycle the host uses the port
        self.executed = False        # whether the slot's thread executed in the last step (2.1)
        self._port_used_prev = False # port used in the previous cycle: the fetch for this one is invalid
        self._cr_ctl = 0             # CR_CTRL action bits of the cycle just stepped (CAPC and host)
        self._cap_w_seen = 0         # cap_w during the cycle just stepped (sampled by START, 14.5)
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

    def host_cr_ctrl(self, byte):
        """CR_CTRL write (10.3, 14.2, 14.5): bit 0 ARM, bit 1 DISARM, bit 2
        START, bit 3 STOP. Lands at the end of the cycle just stepped,
        together with a CAPC committed in it (ARM over DISARM, START over
        STOP); START samples cap_w as it was during that cycle."""
        self._cr_ctl |= byte & 0xF
        self._cr_control()

    def host_cap_cfg(self, word):
        """CAP_CFG write (14): byte 0 = bits 7:0 (group 2:0, watch mask 7:4),
        byte 1 = bits 15:8 (trigger pattern 11:8, trigger mask 15:12)."""
        cr = self.cr
        cr.cap_group = word & 0x7
        cr.cap_mask = (word >> 4) & 0xF
        cr.cap_tpat = (word >> 8) & 0xF
        cr.cap_tmask = (word >> 12) & 0xF

    def host_cap_buf(self, word):
        """CAP_BUF write (14): byte 0 base word, byte 1 length."""
        self.cr.cap_base = word & M8
        self.cr.cap_len = (word >> 8) & M8

    def host_rep_cfg(self, byte):
        """REP_CFG write (14): bits 2:0 group, bits 7:4 drive mask."""
        self.cr.rep_group = byte & 0x7
        self.cr.rep_mask = (byte >> 4) & 0xF

    def host_rep_buf(self, word):
        """REP_BUF write (14): byte 0 base word, byte 1 length (0: the last
        capture's cap_w, sampled at START)."""
        self.cr.rep_base = word & M8
        self.cr.rep_len = (word >> 8) & M8

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
                | (0 if t1.inbox else 1) << 5
                | self.cr.cap_done << 6
                | self.cr.rep_done << 7)
        return 1 if cond & self.irq_en else 0

    # ------------------------------------------------------------ the clock

    def step(self):
        """Advance one core clock cycle. Returns True if the slot owner's
        instruction completed, False if it blocked, True if no thread
        executes in this cycle (see Machine.executed)."""
        c = self.cycle
        th = self.threads
        t = th[c & 1]

        # 1. observe
        th[0]._open_window()
        th[1]._open_window()
        run_seen = (bool(th[0].running), bool(th[1].running))
        self._run_seen = run_seen
        pad = self.pad()
        level = (self._pad2 & 0xFFFF) | ((self.uo_out & 0xFC) << 16)     # 5.2
        drive = (self.uio_out, self.uio_oe, self.uo_out)
        host_busy = bool(self.host_port_busy)
        fetch_valid = not self._port_used_prev                          # 2.1
        self._cr_ctl = 0
        self._cap_w_seen = self.cr.cap_w
        self._ser_cmd = None
        ser_t = th[self.ser.owner]
        ser_per, ser_pre = ser_t.period, ser_t.prescale              # T(c), prescale[owner](c) (15.1)

        # 2. execute (2.1: running and the fetch was valid)
        done = True
        sett_thread = None
        executed = bool(t.running) and fetch_valid
        if executed:
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
        self.executed = executed

        # 3. capture and replay engines (14), after the core's pin command
        free = not host_busy and not run_seen[(c + 1) & 1]              # 14.1
        engine_used = self._cr_cycle(level, free)
        # serializer (15): its pin writes come after the core's and the replay engine's (5.3)
        self._ser_cycle(level, ser_per, ser_pre)
        self._cr_control()                                             # CAPC actions of this cycle
        self._port_used_prev = host_busy or engine_used                # 2.1, for cycle c + 1
        self.host_port_busy = False

        # 4. commit: timer tick for both threads, running or not (6.1)
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

    # ------------------------------------------------------------ capture and replay (14)

    def _cr_cycle(self, level, free):
        """One cycle of the capture and replay engines on the state during
        this cycle (14.1, 14.3, 14.4, 14.6, 14.7). Called after the core's
        instruction, so a replay apply lands after the core's pin command.
        Returns whether an engine used the memory port."""
        cr = self.cr
        used = False

        # ---- capture: trigger (14.3) and recording (14.4)
        g = cr.cap_group
        nib = ((level >> (4 * g)) & 0xF) if g < 6 else 0         # groups 6, 7: pins 24-31 read 0
        s = nib & cr.cap_mask
        q_seen = len(cr.queue)
        entry = None
        if cr.cap_armed:
            if not cr.cap_trig:
                tm = cr.cap_tmask
                want = cr.cap_tpat & tm
                if tm == 0 or ((nib & tm) == want and (cr.cap_prev & tm) != want):
                    cr.cap_trig = 1
                    entry = s                                    # {0, s(c)}
                    cr.cap_last = s
                    cr.cap_dt = 1
            else:
                d = cr.cap_dt
                if s != cr.cap_last:
                    entry = (d << 4) | s
                    cr.cap_last = s
                    cr.cap_dt = 1
                elif d == ENTRY_DT_MAX:
                    entry = (ENTRY_DT_MAX << 4) | cr.cap_last    # idle entry
                    cr.cap_dt = 1
                else:
                    cr.cap_dt = d + 1
        cr.cap_prev = nib                                        # cap_prev(c + 1) = nib(c)
        # a free cycle with a non-empty queue writes the oldest entry (14.1, 14.4)
        written = 0
        if free and q_seen:
            self.imem[(cr.cap_base + cr.cap_w) & M8] = cr.queue.popleft()
            cr.cap_w = (cr.cap_w + 1) & M8
            used = True
            written = 1
        if entry is not None:
            n = (cr.cap_n + 1) & M8
            cr.cap_n = n
            if q_seen - written >= QUEUE_DEPTH:  # lost only if two entries stay held (a write in c frees a slot)
                cr.cap_ovf = 1
                cr.cap_armed = 0
            else:
                cr.queue.append(entry)
            if n == cr.cap_len:                  # the cap_len-th entry
                cr.cap_armed = 0

        # ---- replay: apply (14.7) and fetch (14.6)
        arriving = cr.pf_inflight                # word fetched in c - 1, held from the end of c
        fetched = None
        if cr.rep_active:
            pf_seen = len(cr.pf)
            k = cr.rep_k
            dt = cr.rep_dt
            applied = 0
            if cr.pf:
                head = cr.pf[0]
                target = max(head >> 4, 1)
                if k == 0 or dt == target:
                    cr.pf.popleft()
                    self._rep_apply(head & 0xF)
                    applied = 1
                    cr.rep_k = (k + 1) & M8
                    if ((k + 1) & M8) == cr.rep_n:
                        cr.rep_active = 0
                        cr.rep_done = 1
                elif dt > target:                # underrun: nothing applied
                    cr.rep_under = 1
                    cr.rep_active = 0
                    cr.rep_done = 1
            elif k >= 1 and dt == ENTRY_DT_MAX:
                # no head and rep_dt saturated: no entry can be applied on time
                # any more, whatever its delta, so this is an underrun too (14.7)
                cr.rep_under = 1
                cr.rep_active = 0
                cr.rep_done = 1
            cr.rep_dt = 1 if applied else min(dt + 1, ENTRY_DT_MAX)
            # an entry applied in c no longer counts towards the limit in c (14.6)
            if free and not used and cr.rep_f < cr.rep_n and pf_seen - applied + arriving < PREFETCH_DEPTH:
                fetched = self.imem[(cr.rep_base + cr.rep_f) & M8] & M16
                cr.rep_f = (cr.rep_f + 1) & M8
                used = True
        if arriving:
            cr.pf.append(cr.pf_word)
        if fetched is None:
            cr.pf_inflight = 0
        else:
            cr.pf_inflight = 1
            cr.pf_word = fetched
        return used

    def _rep_apply(self, pins):
        """Apply a replay entry's pins: pinwrite(4 rep_group + i, pins[i]) for
        every i in the drive mask (14.7, 5.3). Groups 6 and 7 are pins
        24-31: ignored."""
        cr = self.cr
        g = cr.rep_group
        if g >= 6:
            return
        for i in range(4):                       # pinwrite does not change od_mask: order is irrelevant
            if (cr.rep_mask >> i) & 1:
                self._pinwrite(4 * g + i, (pins >> i) & 1)

    def _cr_control(self):
        """Apply the CR_CTRL actions of the cycle just stepped (CAPC and host
        combined, 14.2, 14.5) on top of the engines' own updates, then settle
        cap_done (14.4). Every action sets fixed values, so calling this again
        after a later host_cr_ctrl() in the same window is safe."""
        cr = self.cr
        bits = self._cr_ctl
        if bits & CR_ARM:
            cr.cap_armed = 1 if cr.cap_len else 0    # cap_len = 0: no entries, done at once
            cr.cap_trig = 0
            cr.cap_ovf = 0
            cr.cap_n = 0
            cr.cap_w = 0
            cr.queue.clear()
            cr.cap_was_armed = 1
        elif bits & CR_DISARM:
            cr.cap_armed = 0
        if bits & CR_START:
            n = cr.rep_len if cr.rep_len else self._cap_w_seen
            cr.rep_n = n
            cr.rep_active = 1 if n else 0
            cr.rep_done = 0 if n else 1
            cr.rep_under = 0
            cr.rep_k = 0
            cr.rep_f = 0
            cr.rep_dt = 0
            cr.pf.clear()
            cr.pf_inflight = 0                       # a word in flight is dropped too
        elif bits & CR_STOP:
            cr.rep_active = 0
            cr.rep_done = 1
        if not cr.rep_active:
            # the prefetch holds nothing while the replay is inactive: entries
            # held and a word in flight at STOP, underrun or the last apply
            # are discarded (14.5, 14.6)
            cr.pf.clear()
            cr.pf_inflight = 0
        # done after any cycle that leaves the engine disarmed, drained and once armed; ARM clears it
        cr.cap_done = 1 if (not cr.cap_armed and not cr.queue and cr.cap_was_armed) else 0

    # ------------------------------------------------------------ serializer (15)

    def _ser_write(self, p, v, e):
        """write(p, v, e) of 15.1: ignored when od_mask'[p] is set. Called
        after the core's pin command and the replay engine (5.3), so
        self.od_mask is od_mask'."""
        bit = 1 << p
        if self.od_mask & bit:
            return
        if v:
            self.uio_out |= bit
        else:
            self.uio_out &= ~bit & M8
        if e:
            self.uio_oe |= bit
        else:
            self.uio_oe &= ~bit & M8

    def _ser_line(self, p, sv):
        """line(s): P <= s, N <= ~s, both driven (s = 0: J, s = 1: K)."""
        self._ser_write(p, sv, 1)
        self._ser_write(p + 1, 1 - sv, 1)

    def _ser_se0(self, p):
        self._ser_write(p, 0, 1)
        self._ser_write(p + 1, 0, 1)

    def _ser_cycle(self, level, tper, tpre):
        """One cycle of the serializer on the state during this cycle (15).
        `level` is level(c); tper, tpre are period[owner](c) and
        prescale[owner](c), sampled before the core's instruction. Called
        after the core's instruction and the replay engine, so the engine's
        pin writes come third in the order of 5.3; the instruction's own
        serializer effects (self._ser_cmd) are merged last, with SERCFG
        overriding everything the engine would do (15.2)."""
        s = self.ser
        cmd = self._ser_cmd
        self._ser_cmd = None
        cfg = s.cfg
        p = 2 * ((cfg >> 6) & 3)
        lvl_p = (level >> p) & 1
        if cmd is not None and cmd[0] == _SER_CFG:
            s.configure(cmd[1], cmd[2])            # overrides every engine update, no pin writes
            s.rx_last = lvl_p                      # 15.1: in every cycle
            return
        mode = cfg & 3
        if mode == 1 or mode == 2:
            o = s.snapshot()                       # the state during c
            if (cfg >> 4) & 1 and o.tx_state == SER_TX_IDLE:
                self._ser_rx(o, s, level, tper, mode, cfg, p)       # 15.4-15.6
            else:
                self._ser_rx_hold(s)
            if tper != 0 and tpre == 0:            # symbol tick (15.1)
                self._ser_tx(o, s, mode, cfg, p)   # 15.3; its crc_m write stands over the receiver's
        else:
            self._ser_rx_hold(s)                   # off: no ticks, the receiver does not run
        s.rx_last = lvl_p
        if cmd is not None:
            kind = cmd[0]
            if kind == _SER_TX:                    # tx_full(c) = 0: the engine did not take it
                s.tx_hold = cmd[1]
                s.tx_hold_c = cmd[2]
                s.tx_full = 1
            elif kind == _SER_RXB:
                s.rx_valid = 0
            elif kind == _SER_RXE:
                s.rx_end = 0

    @staticmethod
    def _ser_rx_hold(s):
        """15.4: a cycle in which the receiver does not run."""
        s.rx_state = SER_RX_HUNT
        s.rx_sh = M8
        s.rx_n = 0
        s.rx_ones = 0
        s.rx_psym = 0
        s.rx_cnt = 0
        s.rx_w = 1
        s.rx_first = 0

    def _ser_rx(self, o, s, level, tper, mode, cfg, p):
        """15.6: clock recovery and decoding; reads o (the state during c),
        writes s."""
        sym = (level >> p) & 1
        edge = sym != o.rx_last
        if mode == 1:
            if edge:
                s.rx_cnt = tper >> 1
            elif o.rx_cnt != 0:
                s.rx_cnt = (o.rx_cnt - 1) & M16
            else:                                  # sample cycle
                s.rx_cnt = (tper - 1) & M16
                if sym == 0 and ((level >> (p + 1)) & 1) == 0:
                    self._ser_end(o, s, cfg)
                    s.rx_psym = 0
                else:
                    s.rx_psym = sym
                    self._ser_bit(o, s, 1 if sym == o.rx_psym else 0, mode, cfg)
        else:
            if edge and o.rx_w:                    # mid-bit transition
                s.rx_w = 0
                s.rx_cnt = (tper + (tper >> 1) - 2) & M16
                self._ser_bit(o, s, sym, mode, cfg)
            elif o.rx_cnt != 0:
                s.rx_cnt = (o.rx_cnt - 1) & M16
            elif not o.rx_w:
                s.rx_w = 1
                s.rx_cnt = (2 * tper - 1) & M16
            else:                                  # idle
                self._ser_end(o, s, cfg)

    @staticmethod
    def _ser_bit(o, s, d, mode, cfg):
        """bit(d), section 15.5."""
        stuff = (cfg >> 2) & 1
        in_data = o.rx_state == SER_RX_DATA
        if in_data and stuff and o.rx_ones == 6:   # a stuffed bit: discarded
            s.rx_ones = 0
            if d:
                s.rx_serr = 1
            return
        if in_data and stuff:
            s.rx_ones = ((o.rx_ones + 1) & 7) if d else 0
        v = (d << 7) | (o.rx_sh >> 1)
        s.rx_sh = v
        crc32 = (cfg >> 3) & 1
        if not in_data:
            if v == (SER_SYNC_NRZI if mode == 1 else SER_SYNC_MAN):   # a frame starts
                s.rx_state = SER_RX_DATA
                s.rx_n = 0
                s.rx_first = 1
                s.rx_ones = (1 if mode == 1 else 2) if stuff else 0
                s.crc5 = SER_CRC5_INIT
                s.crc_m = SER_INIT_M[crc32]
                s.rx_valid = 0
                s.rx_end = 0
                s.rx_ovr = 0
                s.rx_serr = 0
                s.rx_ferr = 0
            return
        if not ((cfg >> 5) & 1 and o.rx_first):
            s.crc5 = _crc_step(o.crc5, d, SER_CRC5_POLY)
            s.crc_m = _crc_step(o.crc_m, d, SER_POLY_M[crc32])
        if o.rx_n == 7:                            # byte complete
            s.rx_n = 0
            s.rx_first = 0
            if o.rx_valid:
                s.rx_ovr = 1                       # the byte is lost
            else:
                s.rx_hold = v
                s.rx_valid = 1
        else:
            s.rx_n = o.rx_n + 1

    @staticmethod
    def _ser_end(o, s, cfg):
        """end(), section 15.5."""
        if o.rx_state == SER_RX_DATA:
            s.rx_end = 1
            s.rx_ferr = int(o.rx_n != 0)
            s.rx_c5ok = int(o.crc5 == SER_CRC5_RES)
            s.rx_cok = int(o.crc_m == SER_RES_M[(cfg >> 3) & 1])
        s.rx_state = SER_RX_HUNT
        s.rx_sh = M8
        s.rx_n = 0
        s.rx_ones = 0
        s.rx_first = 0

    def _ser_emit(self, o, s, b, mode, p):
        """emit(b), section 15.3."""
        if mode == 1:
            sv = o.tx_line if b else 1 - o.tx_line
            s.tx_line = sv
            self._ser_line(p, sv)
        else:
            self._ser_line(p, 1 - b)
            s.tx_bit = b
            s.tx_half = 1

    @staticmethod
    def _ser_count(o, s, b, stuff):
        if stuff:
            s.tx_ones = ((o.tx_ones + 1) & 7) if b else 0

    def _ser_tx(self, o, s, mode, cfg, p):
        """15.3, in a symbol tick: exactly the first rule that applies."""
        stuff = (cfg >> 2) & 1
        crc32 = (cfg >> 3) & 1
        # 1. second half
        if mode == 2 and o.tx_half:
            self._ser_line(p, o.tx_bit)
            s.tx_half = 0
            return
        st = o.tx_state
        # 2. start
        if st == SER_TX_IDLE:
            if o.tx_full:
                s.tx_sh = o.tx_hold
                s.tx_c = o.tx_hold_c
                s.tx_app = o.tx_hold_c
                s.tx_full = 0
                s.tx_state = SER_TX_DATA
                s.tx_n = 0
                s.tx_ones = 0
                s.tx_line = 0
                s.crc_m = SER_INIT_M[crc32]
            return
        # 3. stuffed bit
        if stuff and o.tx_ones == 6:
            self._ser_emit(o, s, 0, mode, p)
            s.tx_ones = 0
            return
        # 4. data bit
        if st == SER_TX_DATA:
            b = o.tx_sh & 1
            self._ser_emit(o, s, b, mode, p)
            self._ser_count(o, s, b, stuff)
            if o.tx_c:
                s.crc_m = _crc_step(o.crc_m, b, SER_POLY_M[crc32])
            if o.tx_n == 7:
                s.tx_n = 0
                if o.tx_full:
                    s.tx_sh = o.tx_hold
                    s.tx_c = o.tx_hold_c
                    s.tx_app = o.tx_app | o.tx_hold_c
                    s.tx_full = 0
                else:
                    s.tx_state = SER_TX_CRC if o.tx_app else SER_TX_TAIL
            else:
                s.tx_sh = o.tx_sh >> 1
                s.tx_n = o.tx_n + 1
            return
        # 5. CRC bit
        if st == SER_TX_CRC:
            b = (~o.crc_m) & 1
            self._ser_emit(o, s, b, mode, p)
            self._ser_count(o, s, b, stuff)
            s.crc_m = o.crc_m >> 1
            if o.tx_n == SER_W[crc32] - 1:
                s.tx_n = 0
                s.tx_state = SER_TX_TAIL
            else:
                s.tx_n = o.tx_n + 1
            return
        # 6. tail
        n = o.tx_n
        done = False
        if mode == 1:
            if n <= 1:
                self._ser_se0(p)
            elif n == 2:
                self._ser_line(p, 0)                   # J
            elif n == 3:
                for q in (p, p + 1):                   # release; uio_out is not written
                    if not (self.od_mask >> q) & 1:
                        self.uio_oe &= ~(1 << q) & M8
                done = True
        else:
            if n == 0:
                self._ser_line(p, 1)
            elif n == 6:
                self._ser_se0(p)
                done = True
        if done:
            s.tx_state = SER_TX_IDLE
            s.tx_n = 0
        else:
            s.tx_n = (n + 1) & 0x1F

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
                           | t.tid << 6
                           | self.cr.cap_active() << 7
                           | self.cr.rep_active << 8)
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

    def _x_CAPC(self, t, o):
        # rs[3:0] as the CR_CTRL control byte, applied at the end of the slot
        # together with any host CR_CTRL write of the same cycle (14.2, 14.5)
        self._cr_ctl |= t.regs[o["rs"]] & 0xF
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


    # ---- serializer (15.2, 7.4) -------------------------------------------
    # The handlers observe self.ser as it is during the cycle (the engine has
    # not run yet) and leave their effect in self._ser_cmd, which
    # _ser_cycle() merges with the engine's update at the end of the cycle.

    def _x_SERCFG(self, t, o):
        self._ser_cmd = (_SER_CFG, t.regs[o["rs"]] & M8, t.tid)
        return True

    def _ser_tx_cmd(self, t, byte, mark, timed):
        done, base = self._timed(t, not self.ser.tx_full, timed)
        if base:
            self._ser_cmd = (_SER_TX, byte & M8, mark)
        return done

    def _x_SERTX(self, t, o):
        return self._ser_tx_cmd(t, t.regs[o["rs"]], 0, False)

    def _x_SERTXC(self, t, o):
        return self._ser_tx_cmd(t, t.regs[o["rs"]], 1, False)

    def _x_SERTXT(self, t, o):
        return self._ser_tx_cmd(t, t.regs[o["rs"]], 0, True)

    def _x_SERTXCT(self, t, o):
        return self._ser_tx_cmd(t, t.regs[o["rs"]], 1, True)

    def _x_SERI(self, t, o):
        return self._ser_tx_cmd(t, o["n"], 0, False)

    def _x_SERIC(self, t, o):
        return self._ser_tx_cmd(t, o["n"], 1, False)

    def _ser_rx_cmd(self, t, o, timed):
        s = self.ser
        done, base = self._timed(t, bool(s.rx_valid or s.rx_end), timed)
        if base:
            if s.rx_valid:
                t.regs[o["rd"]] = s.rx_hold
                t.z = 0
                self._ser_cmd = (_SER_RXB,)
            else:
                t.regs[o["rd"]] = s.status()
                t.z = 1
                self._ser_cmd = (_SER_RXE,)
        return done

    def _x_SERRX(self, t, o):
        return self._ser_rx_cmd(t, o, False)

    def _x_SERRXT(self, t, o):
        return self._ser_rx_cmd(t, o, True)

    def _x_SERST(self, t, o):
        t.regs[o["rd"]] = self.ser.status()
        return True

    def _x_SERWT(self, t, o):
        s = self.ser
        return self._timed(t, s.tx_state == SER_TX_IDLE and not s.tx_full, False)[0]

    def _x_SERWTT(self, t, o):
        s = self.ser
        return self._timed(t, s.tx_state == SER_TX_IDLE and not s.tx_full, True)[0]


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
