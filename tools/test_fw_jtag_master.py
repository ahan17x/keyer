"""fw/jtag_master.s on the golden model with the TAP model of
protomodels_jtag_master.py, and tests of that model on its own.
Run: python3 -m pytest tools/test_fw_jtag_master.py -q
"""

import bisect
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyer_isa  # noqa: E402
import keyerasm  # noqa: E402
import protomodels as pm  # noqa: E402
import protomodels_jtag_master as jm  # noqa: E402
from keyersim import Machine  # noqa: E402

FW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fw", "jtag_master.s")

TCK, TMS, TDI, TDO_UI = 19, 20, 21, 4          # uo3, uo4, uo5, ui4
IDCODE = 0x14B592AB
IDCODE_IR, BYPASS = 0b0010, 0b1111             # 4-bit instruction register
RESET, DR, IR = 1, 2, 3                        # the firmware's command bytes
HALF = 24                                      # TCK half period used unless a test says otherwise
RTI = "Run-Test/Idle"


def load_fw(symbols=None):
    with open(FW) as f:
        words, syms, _ = keyerasm.assemble(f.read(), symbols=symbols)
    assert max(words) < 256
    return keyerasm.to_list(words), syms, len(words)


def scan(cmd, n, value):
    """The command bytes of one scan of n bits (1..256) of `value`, LSB first."""
    return [cmd, n & 0xFF] + list(value.to_bytes((n + 7) // 8, "little"))


def le(value, nbits):
    return list(value.to_bytes((nbits + 7) // 8, "little"))


def scan_states(kind, n):
    """TAP states after each rising edge of one scan that starts in Run-Test/Idle."""
    head = ["Select-DR-Scan"] + (["Select-IR-Scan"] if kind == "IR" else [])
    return head + ["Capture-" + kind] + ["Shift-" + kind] * n + ["Exit1-" + kind, "Update-" + kind, RTI]


class Bench:
    """The firmware on the golden model, a host feeding the inbox and
    reading the outbox, and the TAP model on the pins."""

    def __init__(self, cmds=(), half=HALF, feeder=None, extra=(), trace=False, **tap_kw):
        """half=None assembles with the default JTAG_HALF of the source (50);
        tap_kw overrides the model's timing limits, which default to what the
        firmware header promises for this half period."""
        words, self.syms, self.size = load_fw(None if half is None else {"JTAG_HALF": half})
        half = 50 if half is None else half
        self.m = Machine(trace=trace)
        self.m.load(words)
        self.host = feeder or pm.HostFeeder(0, cmds)
        kw = dict(setup=half - 6, hold=half + 2, min_high=half, min_low=half, idcode=IDCODE,
                  ir_len=4, idcode_ir=IDCODE_IR)
        kw.update(tap_kw)
        self.tap = jm.JtagTapModel(tck=TCK, tms=TMS, tdi=TDI, tdo_ui_bit=TDO_UI, **kw)
        self.models = [self.host, self.tap] + list(extra)
        self.out = []
        self.m.host_run(0, True)

    def step(self, drain=True):
        for mod in self.models:
            mod.on_cycle(self.m)
        self.m.step()
        if drain:
            b = self.m.host_outbox_pop(0)
            if b is not None:
                self.out.append(b)

    def idle(self):
        """Every command consumed and the thread waiting for the next one."""
        t = self.m.threads[0]
        return (not self.host.pending and not t.inbox and t.running and t.blocked
                and t.pc == self.syms["jtag_cmd"])

    def run(self, limit=400000, drain=True):
        for _ in range(limit):
            self.step(drain)
            if self.idle() and not (drain and self.m.threads[0].outbox):
                break
        else:
            raise AssertionError("the firmware did not come back to the command loop")
        for _ in range(4):                       # let the model see the last pad state
            self.step(drain)
        return self

    # ---- what the TAP model saw
    def states(self):
        return [s for _, s in self.tap.state_log]

    def highs(self):
        return [f - r for r, f in zip(self.tap.tck_rises, self.tap.tck_falls)]

    def lows(self):
        return [r - f for f, r in zip(self.tap.tck_falls, self.tap.tck_rises[1:])]

    def since_fall(self, c):
        f = self.tap.tck_falls
        i = bisect.bisect_right(f, c) - 1
        return c - f[i] if i >= 0 else None

    def to_rise(self, c):
        r = self.tap.tck_rises
        return r[bisect.bisect_left(r, c)] - c

    def since_rise(self, c):
        r = self.tap.tck_rises
        i = bisect.bisect_right(r, c) - 1
        return c - r[i] if i >= 0 else None


# ================================================================ the model alone

class Wires:
    """Stands in for the Machine: TCK, TMS and TDI are set by the test."""

    def __init__(self):
        self.cycle, self.ext_ui, self.ext_uio = 0, 0, 0xFF
        self.tck = self.tms = self.tdi = 0

    def pad(self):
        return ((self.ext_ui & 0xFF) << 8) | (self.tck << TCK) | (self.tms << TMS) | (self.tdi << TDI)


class BitBang:
    """A plain JTAG master: TMS/TDI change with the falling edge, TDO is read
    just before the rising edge."""

    def __init__(self, tap, half=4):
        self.w, self.tap, self.half = Wires(), tap, half
        self.wait(half)

    def wait(self, n):
        for _ in range(n):
            self.tap.on_cycle(self.w)
            self.w.cycle += 1

    def tdo(self):
        return (self.w.ext_ui >> TDO_UI) & 1

    def clock(self, tms, tdi=0):
        self.w.tck, self.w.tms, self.w.tdi = 0, tms, tdi
        self.wait(self.half)
        tdo = self.tdo()
        self.w.tck = 1
        self.wait(self.half)
        self.w.tck = 0
        return tdo

    def tms(self, bits):
        for b in bits:
            self.clock(b)

    def shift(self, n, value, last=True):
        """n clocks in a Shift state; the last leaves it unless last=False."""
        got = 0
        for i in range(n):
            got |= self.clock(1 if last and i == n - 1 else 0, (value >> i) & 1) << i
        return got

    def dr(self, n, value):
        self.tms([1, 0, 0])
        got = self.shift(n, value)
        self.tms([1, 0])
        return got

    def ir(self, n, value):
        self.tms([1, 1, 0, 0])
        got = self.shift(n, value)
        self.tms([1, 0])
        return got


def new_tap(**kw):
    args = dict(tck=TCK, tms=TMS, tdi=TDI, tdo_ui_bit=TDO_UI, idcode=IDCODE, ir_len=4, idcode_ir=IDCODE_IR)
    args.update(kw)
    return jm.JtagTapModel(**args)


def test_model_walks_all_sixteen_states():
    """The state diagram of the standard, written out here independently of
    the model's table: one walk through every state and both scan columns."""
    walk = [(0, RTI), (1, "Select-DR-Scan"), (0, "Capture-DR"), (0, "Shift-DR"), (0, "Shift-DR"),
            (1, "Exit1-DR"), (0, "Pause-DR"), (0, "Pause-DR"), (1, "Exit2-DR"), (0, "Shift-DR"),
            (1, "Exit1-DR"), (1, "Update-DR"), (1, "Select-DR-Scan"), (1, "Select-IR-Scan"),
            (0, "Capture-IR"), (0, "Shift-IR"), (0, "Shift-IR"), (1, "Exit1-IR"), (0, "Pause-IR"),
            (0, "Pause-IR"), (1, "Exit2-IR"), (0, "Shift-IR"), (1, "Exit1-IR"), (1, "Update-IR"),
            (0, RTI), (0, RTI), (1, "Select-DR-Scan"), (0, "Capture-DR"), (1, "Exit1-DR"),
            (0, "Pause-DR"), (1, "Exit2-DR"), (1, "Update-DR"), (0, RTI), (1, "Select-DR-Scan"),
            (1, "Select-IR-Scan"), (0, "Capture-IR"), (1, "Exit1-IR"), (0, "Pause-IR"),
            (1, "Exit2-IR"), (1, "Update-IR"), (1, "Select-DR-Scan"), (1, "Select-IR-Scan"),
            (1, "Test-Logic-Reset"), (1, "Test-Logic-Reset"), (0, RTI)]
    tap = new_tap(start_state="Test-Logic-Reset")
    bb = BitBang(tap)
    for tms, want in walk:
        bb.clock(tms)
        assert tap.state == want
    assert [s for _, s in tap.state_log] == ["Test-Logic-Reset"] + [s for _, s in walk]
    assert set(s for _, s in tap.state_log) == set(jm.TAP_STATES) and len(jm.TAP_STATES) == 16
    assert [t for _, t, _ in tap.sampled] == [t for t, _ in walk]
    assert tap.errors == []


@pytest.mark.parametrize("start", jm.TAP_STATES)
def test_model_five_tms_high_clocks_reach_reset_from_anywhere(start):
    tap = new_tap(start_state=start)
    bb = BitBang(tap)
    bb.tms([1] * 5)
    bb.wait(8)
    assert tap.state == "Test-Logic-Reset" and tap.ir == IDCODE_IR and tap.errors == []


@pytest.mark.parametrize("start", ["Shift-DR", "Pause-DR", "Shift-IR", "Pause-IR"])
def test_model_four_tms_high_clocks_are_not_enough(start):
    """So a reset that is one clock short would be noticed."""
    tap = new_tap(start_state=start)
    bb = BitBang(tap)
    bb.tms([1] * 4)
    bb.wait(8)
    assert tap.state == "Select-IR-Scan" and tap.ir != IDCODE_IR


def test_model_registers():
    """IDCODE after reset, the 01 captured into the instruction register,
    BYPASS (also for an unknown instruction) with its one-bit delay, a scan
    through Pause, TDO released outside the Shift states."""
    tap = new_tap(start_state="Pause-DR")
    bb = BitBang(tap)
    assert tap.ir == BYPASS and bb.tdo() == 1
    bb.tms([1, 1, 1, 1, 1, 0])
    assert tap.state == RTI and tap.ir == IDCODE_IR
    assert bb.dr(32, 0) == IDCODE
    assert bb.dr(40, 0xA5 << 32 | 0xDEADBEEF) == (0xEF << 32) | IDCODE     # TDI comes out 32 bits later
    assert bb.ir(4, BYPASS) == 0b0001                                     # the mandatory capture value
    bb.wait(8)
    assert tap.ir == BYPASS and bb.tdo() == 1                             # released in Run-Test/Idle
    assert bb.dr(9, 0b110100111) == 0b101001110                           # a zero, then TDI one bit late
    assert bb.ir(4, 0b0101) == 0b0001                                     # no such instruction
    assert bb.dr(5, 0b10110) == 0b01100                                   # -> BYPASS
    assert bb.ir(4, IDCODE_IR) == 0b0001
    # one DR scan in two pieces with a stay in Pause-DR: 12 + 20 bits
    bb.tms([1, 0, 0])
    lo = bb.shift(12, 0)                                                  # -> Exit1-DR
    bb.tms([0, 0, 1, 0])                                                  # Pause, Pause, Exit2, Shift
    hi = bb.shift(20, 0)
    bb.tms([1, 0])
    bb.wait(8)
    assert lo | (hi << 12) == IDCODE and tap.state == RTI
    got = [(s.kind, s.nbits, s.tdi, s.tdo, s.ir) for s in tap.scans]
    assert got == [("DR", 0, 0, 0, BYPASS),                              # the reset passing through Update-DR
                   ("DR", 32, 0, IDCODE, IDCODE_IR),
                   ("DR", 40, 0xA5DEADBEEF, 0xEF14B592AB, IDCODE_IR),
                   ("IR", 4, BYPASS, 1, IDCODE_IR),
                   ("DR", 9, 0b110100111, 0b101001110, BYPASS),
                   ("IR", 4, 0b0101, 1, BYPASS),
                   ("DR", 5, 0b10110, 0b01100, 0b0101),
                   ("IR", 4, IDCODE_IR, 1, 0b0101),
                   ("DR", 32, 0, IDCODE, IDCODE_IR)]
    assert tap.errors == []


@pytest.mark.parametrize("delay", [0, 3])
def test_model_tdo_changes_on_the_falling_edge_after_its_delay(delay):
    """Bits 1 and 2 of the IDCODE are 1 and 0: the falling edge that brings
    out bit 2 is visible on TDO, `delay` cycles after the pad edge; the rising
    edge before it changes nothing; leaving Shift-DR releases TDO again."""
    assert (IDCODE >> 1) & 3 == 0b01
    tap = new_tap(start_state=RTI, start_ir=IDCODE_IR, tdo_delay=delay)
    bb = BitBang(tap, half=6)

    def watch(n):
        seen = []
        for _ in range(n):
            bb.wait(1)                           # the model has now driven TDO for cycle w.cycle - 1
            seen.append(bb.tdo())
        return seen

    bb.tms([1, 0, 0])                            # in Shift-DR
    assert bb.clock(0) == 1                      # bit 0
    bb.wait(6)                                   # TDO now carries bit 1
    bb.w.tck = 1
    assert watch(6) == [1] * 6                   # the rising edge shifts but TDO keeps bit 1
    bb.w.tck = 0
    fall = bb.w.cycle
    assert watch(6) == [1] * delay + [0] * (6 - delay)
    assert tap.tck_falls[-1] == fall
    bb.w.tms = 1                                 # leave: Exit1-DR at the next rising edge
    bb.wait(2)
    bb.w.tck = 1
    assert watch(6) == [0] * 6
    bb.w.tck = 0
    assert watch(6) == [0] * delay + [1] * (6 - delay)
    assert tap.state == "Exit1-DR" and tap.errors == []


def test_model_reports_setup_hold_and_pulse_width():
    def wave(levels, **kw):
        """levels: one (tck, tms, tdi) per cycle."""
        tap = new_tap(start_state=RTI, **kw)
        w = Wires()
        for tck, tms, tdi in levels:
            w.tck, w.tms, w.tdi = tck, tms, tdi
            tap.on_cycle(w)
            w.cycle += 1
        return tap

    lim = dict(setup=2, hold=2, min_high=4, min_low=4)
    low, high = [(0, 0, 0)] * 4, [(1, 0, 0)] * 4
    assert wave(low + high + low + high + low, **lim).errors == []
    # TMS two cycles before the edge and two after it: just legal
    ok = [(0, 0, 0)] * 2 + [(0, 1, 0)] * 2 + [(1, 1, 0)] * 2 + [(1, 0, 0)] * 2 + low
    assert wave(ok, **lim).errors == []
    # TMS one cycle before the rising edge (which is at cycle 4)
    t = wave([(0, 0, 0)] * 3 + [(0, 1, 0)] + [(1, 1, 0)] * 4 + low, **lim)
    assert t.errors == [("tms setup", 4, 1)]
    # TMS in the very cycle of the edge: the TAP still takes the old level
    t = wave(low + [(1, 1, 0)] * 4 + low, **lim)
    assert t.errors == [("tms setup", 4, 0)] and t.state == RTI and t.sampled == [(4, 0, 0)]
    # TDI one cycle after the rising edge
    t = wave(low + [(1, 0, 0)] + [(1, 0, 1)] * 3 + low, **lim)
    assert t.errors == [("tdi hold", 5, 1)]
    t = wave([(0, 0, 0)] * 3 + [(0, 0, 1)] + [(1, 0, 1)] * 4 + low, **lim)
    assert t.errors == [("tdi setup", 4, 1)]
    t = wave(low + [(1, 0, 0)] + [(1, 1, 0)] * 3 + low, **lim)
    assert t.errors == [("tms hold", 5, 1)]
    # a three-cycle high phase, then a three-cycle low phase
    t = wave(low + [(1, 0, 0)] * 3 + low + high + low, **lim)
    assert t.errors == [("tck high too short", 7, 3)]
    t = wave(low + high + [(0, 0, 0)] * 3 + high + low, **lim)
    assert t.errors == [("tck low too short", 11, 3)]


def test_model_rejects_impossible_devices():
    with pytest.raises(ValueError):
        new_tap(idcode=0x14B592AA)               # bit 0 of an IDCODE is 1
    with pytest.raises(ValueError):
        new_tap(idcode_ir=0b1111)                # all ones is BYPASS
    with pytest.raises(ValueError):
        new_tap(start_state="Idle")


# ================================================================ the firmware

def test_image_size_and_default_timing():
    b = Bench([RESET] + scan(DR, 32, 0), half=None).run()
    assert b.size <= 80                          # words; well under half the program memory
    assert b.out == le(IDCODE, 32) and b.tap.errors == []
    assert set(b.highs()) == {50}                # the .equ default: 500 kHz at 50 MHz


@pytest.mark.parametrize("start", jm.TAP_STATES)
def test_reset_from_any_state_then_idcode(start):
    """The TAP wakes up in `start` with BYPASS selected (IDCODE only if it
    wakes in Test-Logic-Reset itself), so the IDCODE can only come out if the
    reset really reaches Test-Logic-Reset."""
    b = Bench([RESET] + scan(DR, 32, 0), start_state=start).run()
    tap = b.tap
    assert b.out == [0xAB, 0x92, 0xB5, 0x14]                     # little-endian at the host
    assert tap.errors == []
    assert [t for _, t, _ in tap.sampled[:6]] == [1, 1, 1, 1, 1, 0]
    states = b.states()
    assert states[0] == start and states[5] == "Test-Logic-Reset" and states[6] == RTI
    assert states[7:] == scan_states("DR", 32)
    assert tap.state == RTI and tap.ir == IDCODE_IR
    s = tap.scans[-1]
    assert (s.kind, s.nbits, s.tdi, s.tdo, s.ir) == ("DR", 32, 0, IDCODE, IDCODE_IR)
    # anything else in the scan log is the reset walking through an Update state
    assert all(x.cycle < tap.state_log[5][0] for x in tap.scans[:-1])
    assert len(tap.tck_rises) == len(tap.tck_falls) == 6 + 37
    assert (b.m.pad() >> TCK) & 3 == 0                           # TCK and TMS low at rest
    t = b.m.threads[0]
    assert t.running and t.blocked and t.pc == b.syms["jtag_cmd"]


def test_without_the_reset_the_idcode_does_not_come_out():
    """The control for the test above: same scan, no RESET command."""
    b = Bench(scan(DR, 32, 0), start_state=RTI).run()
    assert b.out == [0, 0, 0, 0] and b.tap.scans[-1].ir == BYPASS and b.tap.errors == []


def test_ir_scan_selects_bypass_and_dr_scan_shows_the_one_bit_delay():
    data = 0x1BA5                                                # 13 bits: not a multiple of 8
    b = Bench([RESET] + scan(IR, 4, BYPASS) + scan(DR, 13, data), start_state="Exit2-DR").run()
    tap = b.tap
    assert tap.errors == []
    assert b.out == [0x01] + le((data << 1) & 0x1FFF, 13)        # IR capture 0001, then the delayed data
    got = [(s.kind, s.nbits, s.tdi, s.tdo, s.ir) for s in tap.scans[-2:]]
    assert got == [("IR", 4, BYPASS, 0b0001, IDCODE_IR), ("DR", 13, data, (data << 1) & 0x1FFF, BYPASS)]
    assert b.states()[7:] == scan_states("IR", 4) + scan_states("DR", 13)
    assert tap.state == RTI and tap.ir == BYPASS


@pytest.mark.parametrize("n", [1, 2, 7, 8, 9, 15, 16, 17, 31, 32, 33, 64, 100, 255, 256])
def test_dr_scan_of_any_length(n):
    """Through BYPASS: n bits in, the same bits one late out. The unused high
    bits of the last TDI byte are set and must be ignored; the last byte
    pushed is right-aligned with zeros above."""
    rng = random.Random(n)
    raw = [rng.randrange(256) for _ in range((n + 7) // 8)]
    mask = (1 << n) - 1
    value = int.from_bytes(bytes(raw), "little") & mask
    if n % 8:
        raw[-1] |= (0xFF << (n % 8)) & 0xFF
    b = Bench([RESET] + scan(IR, 4, BYPASS) + [DR, n & 0xFF] + raw).run()
    assert b.tap.errors == []
    s = b.tap.scans[-1]
    assert (s.kind, s.nbits, s.tdi, s.tdo) == ("DR", n, value, (value << 1) & mask)
    assert b.out == [0x01] + le((value << 1) & mask, n)
    assert b.tap.state == RTI


@pytest.mark.parametrize("n", [1, 3, 4, 8, 11])
def test_ir_scan_of_any_length(n):
    """The instruction register is four bits long: a scan of n bits returns
    the captured 0001 followed by the first n - 4 bits sent, and the last
    four bits sent are the instruction (n < 4: what is left of the capture
    value stays in the upper bits)."""
    value = 0b10111010011 & ((1 << n) - 1)
    b = Bench([RESET] + scan(IR, n, value)).run()
    reg = (0b0001 | (value << 4))
    assert b.out == le(reg & ((1 << n) - 1), n) and b.tap.errors == []
    assert b.tap.ir == (reg >> n) & 0xF and b.tap.state == RTI
    assert b.states()[7:] == scan_states("IR", n)


def test_idcode_register_passes_tdi_through_after_32_bits():
    value = 0x5AC3F00F12
    b = Bench([RESET] + scan(DR, 40, value)).run()
    assert b.out == le(((value & 0xFF) << 32) | IDCODE, 40) and b.tap.errors == []
    assert b.tap.scans[-1].tdi == value


@pytest.mark.parametrize("half", [18, 24, 50])
def test_tck_timing(half):
    """Every TCK edge on one grid of JTAG_HALF cycles, TCK high exactly
    JTAG_HALF, TMS/TDI moving only just after a falling edge, TDO sampled one
    cycle before the rising edge."""
    cmds = [RESET] + scan(DR, 32, 0x12345678) + scan(IR, 4, BYPASS) + scan(DR, 13, 0x1BA5)
    b = Bench(cmds, half=half, trace=True).run()
    tap = b.tap
    assert tap.errors == [] and b.out == le(IDCODE, 32) + [0x01] + le(0x174A, 13)
    r, f = tap.tck_rises, tap.tck_falls
    counts = [6, 2 + 33 + 2, 3 + 5 + 2, 2 + 14 + 2]             # TCK cycles per command
    assert len(r) == len(f) == sum(counts)
    assert all((e - r[0]) % half == 0 for e in r + f)           # one grid for all four commands
    assert set(b.highs()) == {half}
    lows = b.lows()
    assert all(x % half == 0 and x >= half for x in lows)
    first = [sum(counts[:i]) for i in range(1, len(counts))]    # index of the first rise of commands 2..4
    inside = [x for i, x in enumerate(lows) if i + 1 not in first]
    if half >= 24:
        assert set(inside) == {half}                            # a square wave within a command
    else:
        # below 24 a byte boundary takes a second tick: in the 32-bit scan four
        # bytes pushed and r0 used up four times, in the 13-bit scan one of each
        assert set(inside) == {half, 2 * half} and inside.count(2 * half) == 8 + 2
    # TDI: only ever two cycles after a falling edge
    assert tap.tdi_changes and all(b.since_fall(c) == 2 for c in tap.tdi_changes)
    # TMS: two (TMS-only clocks) or six (shift loop) cycles after a falling edge, or from idle
    idle = [c for c in tap.tms_changes if b.since_fall(c) not in (2, 6)]
    assert len(idle) == 4 and idle[0] < r[0]                    # the first TMS high of each command
    assert all(b.to_rise(c) >= half for c in idle)              # a full tick of setup
    assert min(b.to_rise(c) for c in tap.tms_changes + tap.tdi_changes) == half - 6
    assert min(b.since_rise(c) for c in tap.tms_changes + tap.tdi_changes if b.since_rise(c) is not None) == half + 2
    # TDO: the RDC that samples it completes one cycle after the pad edge and
    # reads the level of two cycles earlier, i.e. the pad one cycle before the edge
    rdc = [t.cycle for t in b.m.trace if t.done and keyer_isa.disasm(t.word) == "RDC %d" % (8 + TDO_UI)]
    assert len(rdc) == 33 + 5 + 14 and all(c - 1 in set(r) for c in rdc)


def test_odd_half_period_alternates_by_one_cycle():
    half = 25                                    # a thread runs every second cycle: all margins give one cycle
    b = Bench([RESET] + scan(DR, 32, 0), half=half, min_high=half - 1, min_low=half - 1,
              setup=half - 7, hold=half + 1).run()
    assert b.out == le(IDCODE, 32) and b.tap.errors == []
    assert set(b.highs()) <= {half - 1, half + 1}
    r = b.tap.tck_rises
    assert all((e - r[0]) % half in (0, 1, half - 1) for e in r + b.tap.tck_falls)


@pytest.mark.parametrize("half", [12, 14, 16])
def test_half_period_below_18_doubles_the_low_half_of_the_shift_loop(half):
    b = Bench([RESET] + scan(DR, 32, 0), half=half).run()
    assert b.out == le(IDCODE, 32) and b.tap.errors == []
    r = b.tap.tck_rises
    assert set(b.highs()) == {half} and all((e - r[0]) % half == 0 for e in r + b.tap.tck_falls)
    lows = b.lows()
    assert set(lows[8:8 + 32]) == {2 * half}                     # the 33 clocks of the shift loop
    assert set(lows[:5]) == {half}                               # the TMS-only clocks keep up


def test_half_period_below_12_leaves_the_grid_but_shortens_nothing():
    half = 8
    b = Bench([RESET] + scan(DR, 32, 0), half=half).run()
    assert b.out == le(IDCODE, 32) and b.tap.errors == []        # min_high = min_low = 8, setup = 2
    highs = b.highs()
    assert set(highs[:6]) == {half} and set(highs[8:8 + 33]) == {12}   # six slots between the WAITDs
    assert min(b.lows()) >= half
    r = b.tap.tck_rises
    assert any((e - r[0]) % half for e in r + b.tap.tck_falls)


class SlowHost:
    """A host that delivers one inbox byte every `gap` cycles."""

    def __init__(self, tid, data, gap):
        self.tid, self.pending, self.gap, self._next = tid, list(data), gap, 0

    def on_cycle(self, m):
        if self.pending and m.cycle >= self._next and m.host_inbox_push(self.tid, self.pending[0]):
            self.pending.pop(0)
            self._next = m.cycle + self.gap


def test_slow_host_stalls_the_scan_with_tck_low():
    """The inbox runs dry in the middle of a scan: the firmware waits in
    Shift-DR with TCK low and TMS/TDI untouched, then carries on; nothing is
    shortened and the edges are still on the grid."""
    value = 0x1ACE5B3
    gap = 1500                                   # cycles between inbox bytes; 8 bits take 384
    host = SlowHost(0, [RESET] + scan(IR, 4, BYPASS) + scan(DR, 29, value), gap)
    b = Bench(feeder=host, start_state="Capture-IR").run()
    tap = b.tap
    assert tap.errors == []
    assert b.out == [0x01] + le((value << 1) & 0x1FFFFFFF, 29)
    assert tap.scans[-1][:4] == ("DR", 29, value, (value << 1) & 0x1FFFFFFF)
    r, f = tap.tck_rises, tap.tck_falls
    assert all((e - r[0]) % HALF == 0 for e in r + f) and set(b.highs()) == {HALF}
    # the long low phases, and the state the TAP sat in during each
    stalls = [(tap.state_log[i + 1][1], x) for i, x in enumerate(b.lows()) if x > 20 * HALF]
    in_shift = [x for s, x in stalls if s == "Shift-DR"]
    assert len(in_shift) == 3                                    # waiting for data bytes 2, 3 and 4
    assert all(x % HALF == 0 for _, x in stalls)
    assert all(b.since_fall(c) == 2 for c in tap.tdi_changes)    # TDI never moved during a stall
    assert tap.state == RTI


def test_full_outbox_pauses_the_scan_until_the_host_reads():
    """A 256-bit scan returns 32 bytes and the outbox holds 16: with a host
    that is not reading, the firmware stops with TCK low in Shift-DR, and
    finishes with nothing lost once the host reads."""
    rng = random.Random(7)
    value = rng.getrandbits(256)
    mask = (1 << 256) - 1
    b = Bench([RESET] + scan(IR, 4, BYPASS) + scan(DR, 256, value))
    t = b.m.threads[0]
    for _ in range(40000):
        b.step(drain=False)
        if len(t.outbox) == 16 and t.blocked and keyer_isa.disasm(b.m.imem[t.pc]).startswith("PUSH"):
            break
    else:
        raise AssertionError("the outbox never filled")
    edges = len(b.tap.tck_rises) + len(b.tap.tck_falls)
    for _ in range(1000):
        b.step(drain=False)
    assert len(b.tap.tck_rises) + len(b.tap.tck_falls) == edges   # TCK stopped...
    assert (b.m.pad() >> TCK) & 1 == 0 and b.tap.state == "Shift-DR"   # ...low, in the middle of the scan
    assert t.blocked and len(t.outbox) == 16
    b.run()
    assert b.tap.errors == []
    assert b.out == [0x01] + le((value << 1) & mask, 256)
    assert b.tap.scans[-1][:4] == ("DR", 256, value, (value << 1) & mask)
    r = b.tap.tck_rises
    assert all((e - r[0]) % HALF == 0 for e in r + b.tap.tck_falls) and set(b.highs()) == {HALF}


class StuckTdo:
    """A broken TDO connection: the input sees a fixed level whatever the TAP drives."""

    def __init__(self, level):
        self.level = level

    def on_cycle(self, m):
        m.ext_ui = (m.ext_ui & ~(1 << TDO_UI)) | (self.level << TDO_UI)


@pytest.mark.parametrize("level,code", [(1, 0xFFFFFFFF), (0, 0)])
def test_dead_tdo_cannot_hang_the_thread(level, code):
    """No target answers (TDO pulled up, or shorted low): the scan still runs
    to the end on the timer alone, the host reads all ones (or zeros), which
    no IDCODE can be, and the thread is back at the command loop."""
    b = Bench([RESET] + scan(DR, 32, 0), extra=[StuckTdo(level)]).run()
    assert b.out == le(code, 32)
    assert b.tap.errors == [] and b.tap.state == RTI
    assert b.tap.scans[-1].tdo == IDCODE                         # the TAP did send it
    t = b.m.threads[0]
    assert t.running and t.blocked and t.pc == b.syms["jtag_cmd"]


def test_unknown_command_bytes_are_ignored():
    junk = [0x00, 0x04, 0x05, 0x80, 0xFF]
    b = Bench(junk + [RESET] + junk + scan(DR, 32, 0) + junk).run()
    assert b.out == le(IDCODE, 32) and b.tap.errors == []
    assert len(b.tap.tck_rises) == 6 + 37 and b.tap.state == RTI
    assert len(b.tap.tms_changes) == 6                           # reset: up, down; scan: up, down, up, down


def test_tdo_sample_point_margin():
    """The firmware reads the TDO pad one cycle before TCK rises, so a target
    may take up to JTAG_HALF - 1 cycles from the falling edge to drive the
    bit; one cycle more and every bit arrives a clock late."""
    b = Bench([RESET] + scan(DR, 32, 0), tdo_delay=HALF - 1).run()
    assert b.out == le(IDCODE, 32) and b.tap.errors == []
    b = Bench([RESET] + scan(DR, 32, 0), tdo_delay=HALF).run()
    assert b.out == le(((IDCODE << 1) | 1) & 0xFFFFFFFF, 32)     # the released level, then bits 0..30
    assert b.tap.scans[-1].tdo == IDCODE


class RandomHost:
    """A host with no sense of time: inbox bytes arrive after random gaps."""

    def __init__(self, tid, data, rng, p):
        self.tid, self.pending, self.rng, self.p = tid, list(data), rng, p

    def on_cycle(self, m):
        if self.pending and self.rng.random() < self.p and m.host_inbox_push(self.tid, self.pending[0]):
            self.pending.pop(0)


@pytest.mark.parametrize("seed,half,p_feed,p_read", [
    (0, 18, 1.0, 1.0), (1, 18, 0.004, 0.003), (2, 20, 0.05, 0.02), (3, 22, 1.0, 0.003),
    (4, 24, 0.004, 1.0), (5, 26, 0.05, 0.003), (6, 30, 1.0, 0.02), (7, 40, 0.004, 0.02)])
def test_random_sessions_against_a_reference(seed, half, p_feed, p_read):
    """Random command streams (resets, IR and DR scans of random length and
    data, junk bytes) from a random power-up state, with a host that feeds
    the inbox and reads the outbox at random moments (probability per cycle
    p_feed, p_read). The expected bytes come from a reference written here
    from the standard: the 4-bit instruction register captures 0001; IDCODE
    is a 32-bit register, everything else a one-bit BYPASS."""
    rng = random.Random(1000 + seed)
    cmds, want, scans = [RESET], [], []
    ir = IDCODE_IR
    for _ in range(rng.randrange(6, 12)):
        kind = rng.choice([RESET, IR, IR, DR, DR, DR, 0, 9])
        if kind == RESET:
            cmds.append(RESET)
            ir = IDCODE_IR
        elif kind in (IR, DR):
            n = rng.choice([1, 2, 3, 4, 5, 7, 8, 9, 12, 16, 23, 32, 33, 47, 64])
            value = rng.getrandbits(n)
            if kind == IR:
                reg = 0b0001 | (value << 4)
                new_ir = (reg >> n) & 0xF
            else:
                reg = (IDCODE | (value << 32)) if ir == IDCODE_IR else (value << 1)
            tdo = reg & ((1 << n) - 1)
            cmds += scan(kind, n, value)
            want += le(tdo, n)
            scans.append(("IR" if kind == IR else "DR", n, value, tdo, ir))
            if kind == IR:
                ir = new_ir
        else:
            cmds.append(kind)                    # not a command: ignored
    host = RandomHost(0, cmds, rng, p_feed)
    b = Bench(feeder=host, half=half, start_state=rng.choice(jm.TAP_STATES))
    t = b.m.threads[0]
    for _ in range(2000000):
        b.step(drain=rng.random() < p_read)
        if b.idle() and not t.outbox:
            break
    else:
        raise AssertionError("the firmware did not come back to the command loop")
    for _ in range(4):
        b.step()
    tap = b.tap
    assert tap.errors == []
    assert b.out == want
    last_reset = max(i for i, (_, s) in enumerate(tap.state_log) if s == "Test-Logic-Reset")
    first = tap.state_log[5][0]                  # the first reset is complete here
    assert [(s.kind, s.nbits, s.tdi, s.tdo, s.ir) for s in tap.scans if s.cycle > first] == scans
    assert tap.state == RTI and tap.ir == ir and last_reset >= 5
    r, f = tap.tck_rises, tap.tck_falls
    assert all((e - r[0]) % half == 0 for e in r + f) and set(b.highs()) == {half}
    assert all(b.since_fall(c) == 2 for c in tap.tdi_changes)
