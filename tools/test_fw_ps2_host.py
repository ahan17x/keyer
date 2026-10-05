"""fw/ps2_host.s on the golden model with an independent PS/2 device model.
Run: python3 -m pytest tools/test_fw_ps2_host.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyer_isa  # noqa: E402
import keyerasm  # noqa: E402
from keyersim import Machine  # noqa: E402
from protomodels_ps2_host import Ps2DeviceModel, odd_parity  # noqa: E402

FW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fw", "ps2_host.s")
CLK, DATA = 0, 1                         # uio0, uio1
ESC = 0xA5
EV_PARITY, EV_FRAME, EV_RX_TMO, EV_NOACK, EV_TX_TMO = 1, 2, 3, 4, 5

# Fast timing for the tests: a CLK half period of 32 cycles, a 40-cycle tick.
H = 32
TICK, INH, RTS, TMO = 40, 3, 30, 20
MIN_INH = 100                            # the device model's shortest legal inhibit
FAST = {"PS2_TICK": TICK, "PS2_INH_TICKS": INH, "PS2_RTS_TICKS": RTS, "PS2_TMO_TICKS": TMO}
FRAME = 22 * H                           # eleven clock periods
TURNAROUND = 71                          # cycles from the last falling edge of a frame to polling again


def load_fw(symbols=None):
    with open(FW) as f:
        words, syms, _ = keyerasm.assemble(f.read(), symbols=symbols)
    assert max(words) < 256, "program too large: %d words" % (max(words) + 1)
    return keyerasm.to_list(words), syms, len(words)


class Bench:
    """The firmware on one thread, a device on the pins, and a host that
    empties the outbox as bytes appear (unless drain is turned off)."""

    def __init__(self, symbols=FAST, half=H, min_inhibit=MIN_INH, tid=0, trace=False, **dev):
        self.words, self.syms, self.size = load_fw(symbols)
        self.m = Machine(trace=trace)
        self.m.load(self.words)
        self.tid = tid
        if tid:
            self.m.host_set_pc(tid, self.syms["ps2_init"])
        self.m.host_run(tid, True)
        self.dev = Ps2DeviceModel(CLK, DATA, half, min_inhibit, **dev)
        self.out, self.out_log = [], []          # bytes; (cycle, byte)
        self.drain = True

    def step(self):
        m = self.m
        self.dev.on_cycle(m)
        m.step()
        if self.drain:
            self.pop_all()

    def pop_all(self):
        while True:
            b = self.m.host_outbox_pop(self.tid)
            if b is None:
                return
            self.out.append(b)
            self.out_log.append((self.m.cycle, b))

    def run(self, cycles):
        for _ in range(cycles):
            self.step()

    def run_until(self, cond, limit):
        for _ in range(limit):
            if cond():
                return
            self.step()
        raise AssertionError("condition not reached in %d cycles (cycle %d)" % (limit, self.m.cycle))

    def command(self, byte):
        assert self.m.host_inbox_push(self.tid, byte)

    def thread(self):
        return self.m.threads[self.tid]

    def idle(self):
        """The thread is in its two-instruction polling loop."""
        t = self.thread()
        return t.running and t.pc in (self.syms["idle"], self.syms["idle"] + 1)

    def released(self):
        return self.m.uio_oe & 0x03 == 0

    def settle(self, cycles=3 * FRAME):
        """Run on, then check the quiet state every test must end in."""
        self.run(cycles)
        assert self.idle(), "thread not back in the idle loop: pc=0x%02X" % self.thread().pc
        assert self.released(), "a line is still pulled low: oe=%02X" % self.m.uio_oe
        assert self.m.pad() & 0x03 == 0x03


def stuffed(data):
    """The outbox stream for correctly received bytes."""
    out = []
    for b in data:
        out += [ESC, ESC] if b == ESC else [b]
    return out


# ---------------------------------------------------------------- size

def test_image_fits_program_memory():
    _, syms, size = load_fw()
    assert size <= 256
    assert syms["ps2_init"] == 0                     # the default PC after reset
    # the escape must not collide with an event code
    assert ESC == syms["PS2_ESC"] and ESC not in (EV_PARITY, EV_FRAME, EV_RX_TMO, EV_NOACK, EV_TX_TMO)


# ---------------------------------------------------------------- device to host

SCAN = [0x1C, 0xF0, 0x1C, 0xE0, 0x75, 0x00, 0xFF, ESC, 0x5A, 0xAA, 0x55, 0x01, 0x80]


@pytest.mark.parametrize("tid", [0, 1])
def test_scan_codes_received(tid):
    """Several scan codes, including the escape value, 0x00 and 0xFF, arrive
    in order; the host never touches the lines; every bit is sampled early in
    the CLK low phase."""
    b = Bench(tid=tid, trace=True)
    for code in SCAN:
        b.dev.send(code)
    b.run_until(lambda: not b.dev.pending, len(SCAN) * (FRAME + 4 * H) + 1000)
    b.settle()
    assert b.out == stuffed(SCAN)
    assert b.dev.sent == SCAN
    assert b.dev.errors == []
    assert b.dev.aborted == [] and b.dev.inhibits == [] and b.dev.requests == []
    assert all(oe & 0x03 == 0 for _, _, oe, _ in b.m.pin_events), "the host drove CLK or DATA while receiving"
    # sample points: an INR at cycle c reads the pad of cycle c - 2
    samples = [r.cycle - 2 for r in b.m.trace
               if r.done and r.tid == tid and keyer_isa.disasm(r.word).startswith("INR")]
    assert len(samples) == 11 * len(SCAN)
    for i, (start, end, _) in enumerate(b.dev.sent_log):
        falls = [f for f in b.dev.clk_falls if start <= f <= end]
        assert len(falls) == 11
        for k, (f, s) in enumerate(zip(falls, samples[11 * i:11 * i + 11])):
            # the start bit is found by polling (4-cycle loop), the rest by an edge wait
            assert 4 <= s - f <= (7 if k == 0 else 5), (i, k, s - f)
            assert s - f < H                              # inside the low phase


def test_every_byte_value_received():
    b = Bench()
    data = list(range(256))
    for v in data:
        b.dev.send(v)
    b.run_until(lambda: not b.dev.pending, 256 * (FRAME + 4 * H) + 1000)
    b.settle()
    assert b.out == stuffed(data)
    assert b.dev.sent == data and b.dev.errors == [] and b.dev.aborted == []


def test_parity_error_is_reported_not_delivered():
    b = Bench()
    b.dev.send(0x1C)
    b.dev.send(0x32, parity_error=True)
    b.dev.send(0xFE, parity_error=True)              # an even and an odd number of ones
    b.dev.send(0x21)
    b.run_until(lambda: not b.dev.pending, 6 * FRAME)
    b.settle()
    assert b.out == [0x1C, ESC, EV_PARITY, ESC, EV_PARITY, 0x21]
    assert b.dev.errors == []


def test_framing_errors_are_reported_not_delivered():
    """A stop bit of 0, a start bit of 1, and both; parity is right in all."""
    b = Bench()
    b.dev.send(0x1C, stop_bit=0)
    b.dev.send(0x1C, start_bit=1)
    b.dev.send(0x1C, start_bit=1, stop_bit=0)
    b.dev.send(0x23)
    b.run_until(lambda: not b.dev.pending, 6 * FRAME)
    b.settle()
    assert b.out == [ESC, EV_FRAME, ESC, EV_FRAME, ESC, EV_FRAME, 0x23]
    assert b.dev.errors == []


@pytest.mark.parametrize("pulses", [1, 2, 5, 10])
def test_abandoned_frame_times_out_and_the_next_is_received(pulses):
    """The device stops clocking after `pulses` clock pulses. The firmware
    reports ESC 0x03 when the frame deadline passes (TMO ticks after the
    first falling edge, the tick phase being free: TMO - 1 to TMO ticks) and
    receives the next frame normally."""
    b = Bench()
    b.dev.send(0x1C)
    b.dev.send(0x77, stop_after=pulses)
    b.dev.send(0x5A, delay=TMO * TICK)
    b.run_until(lambda: not b.dev.pending, 6 * FRAME + TMO * TICK)
    b.settle()
    assert b.out == [0x1C, ESC, EV_RX_TMO, 0x5A]
    assert b.dev.sent == [0x1C, 0x5A] and [x[1] for x in b.dev.abandoned] == [0x77]
    assert b.dev.errors == [] and b.dev.inhibits == []
    first_fall = [f for f in b.dev.clk_falls if f > b.dev.sent_log[0][1]][0]
    report = [c for c, v in b.out_log if v == ESC][0]
    assert (TMO - 1) * TICK <= report - first_fall <= TMO * TICK + 30, report - first_fall


# ---------------------------------------------------------------- host to device

def check_command_timing(b, entry, inhibit):
    """The wire rules of one command, from what the device model recorded."""
    t0, end, byte, _ = entry
    start, length = inhibit
    # CLK was held low for the configured time (and never less than the device needs)
    assert INH * TICK <= length <= INH * TICK + 16, length
    assert length >= b.dev.min_inhibit
    edges = [(c, lvl) for c, lvl in b.dev.host_data_edges if start <= c <= end]
    # request to send: DATA goes low while CLK is still held, CLK is released two cycles later
    rts_cycle, lvl = edges[0]
    assert lvl == 0 and rts_cycle == start + length - 2
    # every later change of DATA is 13 to 16 cycles after a falling edge, CLK low,
    # and so at least H - 16 cycles before the rising edge on which the device samples
    falls = [f for f in b.dev.clk_falls if t0 <= f <= end]
    assert len(falls) == 11
    for c, _ in edges[1:]:
        f = max(x for x in falls if x <= c)
        assert 13 <= c - f <= 16, (c, f)
        assert (f + b.dev.half) - c >= b.dev.half - 16
    # what is on the wire at each rising edge: data LSB first, odd parity, stop (released)
    def level_at(cycle):
        lvl = 1
        for c, v in edges:
            if c <= cycle:
                lvl = v
        return lvl

    wire = [level_at(f + b.dev.half) for f in falls[:10]]
    assert wire == [(byte >> i) & 1 for i in range(8)] + [odd_parity(byte), 1], (hex(byte), wire)
    assert level_at(end) == 1                         # and DATA stays released to the end


@pytest.mark.parametrize("tid", [0, 1])
def test_command_sent_and_reply_received(tid):
    """Reset (0xFF): the device acknowledges the frame and answers 0xFA 0xAA.
    Then Enable (0xF4) and Set LEDs (0xED, argument 0x02), each sent after
    the previous answer, as a host driver does."""
    b = Bench(tid=tid)
    b.dev.default_reply = [0xFA]
    b.dev.replies[0xFF] = [0xFA, 0xAA]
    expect = []
    for n, (cmd, reply) in enumerate([(0xFF, [0xFA, 0xAA]), (0xF4, [0xFA]), (0xED, [0xFA]), (0x02, [0xFA])]):
        b.command(cmd)
        expect += reply
        b.run_until(lambda: b.out == expect, 5 * FRAME)
        assert b.dev.commands[-1] == cmd and len(b.dev.commands) == n + 1
        check_command_timing(b, b.dev.command_log[-1], b.dev.inhibits[-1])
    b.settle()
    assert b.out == expect and b.dev.errors == []
    assert all(acked for _, _, _, acked in b.dev.command_log)
    assert len(b.dev.inhibits) == 4 and len(b.dev.requests) == 4


def test_every_byte_value_sent():
    """All 256 command bytes, queued back to back (no answers configured).
    The device model checks the parity and stop bit of each."""
    b = Bench()
    pending = list(range(256))
    for _ in range(256 * (FRAME + INH * TICK + 6 * H) + 2000):
        while pending and b.m.host_inbox_push(0, pending[0]):
            pending.pop(0)
        b.step()
        if len(b.dev.commands) == 256:
            break
    b.settle()
    assert b.dev.commands == list(range(256))
    assert b.dev.errors == [] and b.dev.bad_commands == []
    assert b.out == []                                # success reports nothing
    assert all(length >= b.dev.min_inhibit for _, length in b.dev.inhibits)


def test_missing_ack_is_reported():
    """The device clocks all eleven pulses but does not pull DATA low for
    the ACK: ESC 0x04. The next command goes through."""
    b = Bench()
    b.dev.default_reply = [0xFA]
    b.dev.acks = [False]
    b.command(0xED)
    b.run_until(lambda: len(b.out) == 2, 4 * FRAME)
    assert b.out == [ESC, EV_NOACK]
    b.settle()                                        # the device finishes its eleventh pulse
    assert b.dev.command_log[-1][2:] == (0xED, False)
    b.command(0xEE)
    b.run_until(lambda: len(b.out) == 3, 5 * FRAME)
    b.settle()
    assert b.out == [ESC, EV_NOACK, 0xFA]
    assert b.dev.commands == [0xED, 0xEE] and b.dev.errors == []


def test_device_that_never_clocks_a_command():
    """No clock after the request to send: after PS2_RTS_TICKS the firmware
    releases DATA and reports ESC 0x05; it then works normally."""
    b = Bench()
    b.dev.default_reply = [0xFA]
    b.dev.respond = False
    b.command(0xF4)
    b.run_until(lambda: len(b.out) == 2, RTS * TICK + 20 * TICK)
    assert b.out == [ESC, EV_TX_TMO]
    b.settle()
    assert b.dev.commands == [] and len(b.dev.requests) == 1
    # DATA was held low for the request for RTS - 1 to RTS ticks after CLK was released
    (low, _), (high, _) = b.dev.host_data_edges
    start, length = b.dev.inhibits[0]
    assert (RTS - 1) * TICK <= high - (start + length) <= RTS * TICK + 10, high - (start + length)
    assert low == start + length - 2
    b.dev.respond = True
    b.dev.send(0x1C)
    b.command(0xF4)
    b.run_until(lambda: len(b.out) == 4, 8 * FRAME)
    b.settle()
    assert sorted(b.out[2:]) == [0x1C, 0xFA] and b.dev.commands == [0xF4]
    assert b.dev.errors == []


@pytest.mark.parametrize("pulses", [1, 4, 9, 10])
def test_device_that_stops_clocking_in_a_command(pulses):
    """The device clocks only `pulses` pulses of the command: ESC 0x05 when
    the frame deadline passes, DATA released."""
    b = Bench()
    b.dev.default_reply = [0xFA]
    b.dev.stall_after = pulses
    b.command(0x00)                                   # all data bits 0: DATA is held low when the clock stops
    b.run_until(lambda: len(b.out) == 2, 4 * FRAME + TMO * TICK)
    assert b.out == [ESC, EV_TX_TMO]
    assert b.dev.stalled and b.dev.stalled[0][1] == pulses
    b.settle()
    b.command(0xF4)
    b.run_until(lambda: len(b.out) == 3, 5 * FRAME)
    b.settle()
    assert b.out[2:] == [0xFA] and b.dev.commands == [0xF4]
    assert b.dev.errors == []


# ---------------------------------------------------------------- both directions at once

def test_command_colliding_with_a_device_frame():
    """The host's command byte arrives at every cycle around the start of a
    device frame. Whatever the phase, nothing is lost and no rule is broken:
    either the frame is received first and the command follows, or the
    inhibit cuts the frame off, the device aborts and sends it again."""
    ref = Bench()
    ref.dev.send(0x1C, delay=200)
    ref.run_until(lambda: ref.dev.sent, 2000)
    t0 = ref.dev.sent_log[0][0]                       # the cycle the start bit goes onto DATA
    outcomes = set()
    # every cycle from before the start bit to the end of the first clock pulse, then coarser
    offsets = list(range(-20, 2 * H)) + list(range(2 * H, FRAME + 5 * H, 41))
    for off in offsets:
        b = Bench()
        b.dev.default_reply = [0xFA]
        b.dev.send(0x1C, delay=200)
        b.run(t0 + off)
        b.command(0xF4)
        b.run_until(lambda: len(b.out) == 2 and not b.dev.pending, 8 * FRAME)
        b.settle(FRAME)
        assert b.dev.errors == [], (off, b.dev.errors)
        assert b.dev.commands == [0xF4], off
        assert b.dev.sent == [0x1C, 0xFA] or b.dev.sent == [0xFA, 0x1C], (off, b.dev.sent)
        assert sorted(b.out) == [0x1C, 0xFA], (off, b.out)
        assert len(b.dev.aborted) <= 1
        outcomes.add((len(b.dev.aborted), tuple(b.out)))
    # both things happened somewhere in the sweep
    assert (0, (0x1C, 0xFA)) in outcomes                    # frame first, command after
    assert any(aborted == 1 for aborted, _ in outcomes)     # frame cut off and sent again


def test_full_outbox_inhibits_the_device():
    """Nobody reads the outbox. When it is full the firmware holds CLK low,
    the device keeps its data, and everything arrives once the host reads."""
    b = Bench()
    b.drain = False
    data = [(7 * i + 3) & 0xFF for i in range(24)]
    assert ESC not in data
    for v in data:
        b.dev.send(v)
    b.run(24 * (FRAME + 4 * H))                       # long enough for all 24 had the host been reading
    t = b.thread()
    assert len(t.outbox) == 16
    assert b.dev.sent == data[:17]                    # the 17th was received and is waiting for room
    assert b.m.uio_oe & 0x03 == 0x01                  # CLK held low, DATA released
    assert b.dev.state == "inhibit" and b.dev.pending == 7
    held_from = b.m.cycle
    b.run(5 * FRAME)                                  # it stays that way
    assert b.dev.sent == data[:17] and len(t.outbox) == 16
    b.drain = True
    b.run_until(lambda: not b.dev.pending, 10 * (FRAME + INH * TICK + 4 * H))
    b.settle()
    assert b.out == data
    assert b.dev.sent == data and b.dev.errors == []
    start, length = b.dev.inhibits[0]
    assert start < held_from and length >= 5 * FRAME + INH * TICK


def test_full_outbox_in_the_middle_of_a_report():
    """One free place and a two-byte report: the ESC goes in, the code waits
    with the device inhibited; nothing is lost or reordered."""
    b = Bench()
    b.drain = False
    data = list(range(0x10, 0x1F))                   # 15 bytes
    for v in data:
        b.dev.send(v)
    b.dev.send(0x44, parity_error=True)
    b.dev.send(0x45)
    b.run(18 * (FRAME + 4 * H))
    assert list(b.thread().outbox) == data + [ESC]
    assert b.m.uio_oe & 0x03 == 0x01 and b.dev.pending == 1
    b.drain = True
    b.run_until(lambda: not b.dev.pending, 4 * (FRAME + INH * TICK + 4 * H))
    b.settle()
    assert b.out == data + [ESC, EV_PARITY, 0x45]
    assert b.dev.errors == []


# ---------------------------------------------------------------- the model itself

def test_model_frame_on_the_wire():
    """The device model alone (no thread running): the frame for 0x1C, bit
    by bit against the PS/2 frame written out by hand, and its clock."""
    m = Machine()
    dev = Ps2DeviceModel(CLK, DATA, H, MIN_INH)
    dev.send(0x1C)
    wave = []
    for _ in range(FRAME + 4 * H):
        dev.on_cycle(m)
        wave.append(m.pad() & 3)
        m.step()
    clk = [w & 1 for w in wave]
    dat = [(w >> 1) & 1 for w in wave]
    falls = [i for i in range(1, len(clk)) if clk[i - 1] == 1 and clk[i] == 0]
    rises = [i for i in range(1, len(clk)) if clk[i - 1] == 0 and clk[i] == 1]
    assert falls == dev.clk_falls and rises == dev.clk_rises and len(falls) == 11
    # start, 0x1C LSB first (0 0 1 1 1 0 0 0), odd parity (three ones: 0), stop
    assert [dat[f] for f in falls] == [0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 1]
    assert all(r - f == H for f, r in zip(falls, rises))                 # low time
    assert all(f2 - r == H for r, f2 in zip(rises, falls[1:]))           # high time
    # DATA changes only in the middle of the high phase, never while CLK is low
    changes = [i for i in range(1, len(dat)) if dat[i] != dat[i - 1]]
    assert changes[0] == falls[0] - H // 2                               # the start bit leads the first fall
    for c in changes[1:]:
        assert clk[c] == 1 and clk[c - 1] == 1
        assert c - max(r for r in rises if r <= c) == H // 2
    assert odd_parity(0x00) == 1 and odd_parity(0xFF) == 1 and odd_parity(0xF4) == 0
    assert dev.sent == [0x1C] and dev.errors == []


def bad_host(src, cycles=600, **dev):
    """A hand-written host that breaks one rule; returns the kinds of error
    the device model reported."""
    words, _, _ = keyerasm.assemble(src)
    m = Machine()
    m.load(keyerasm.to_list(words))
    m.host_run(0, True)
    model = Ps2DeviceModel(CLK, DATA, H, MIN_INH, **dev)
    for _ in range(cycles):
        model.on_cycle(m)
        m.step()
    return {e[0] for e in model.errors}, model


def test_model_reports_a_short_inhibit():
    kinds, model = bad_host("""
        od    uio0
        od    uio1
        clr   uio0
        delay 20                ; 21 slots: CLK low for about 44 cycles, the model wants 100
        set   uio0
        halt
    """)
    assert kinds == {"inhibit too short"}
    assert len(model.inhibits) == 1 and model.inhibits[0][1] < MIN_INH


def test_model_reports_a_line_driven_high():
    kinds, _ = bad_host("""
        clr   uio0
        oen   uio0              ; push-pull: CLK driven low (an inhibit)
        delay 60
        set   uio0              ; and then driven high instead of released
        halt
    """)
    assert kinds == {"host drives clk high"}


def test_model_reports_a_request_without_inhibit():
    kinds, model = bad_host("""
        od    uio0
        od    uio1
        clr   uio1              ; DATA low with CLK never held low first
        halt
    """, cycles=200)
    assert kinds == {"request to send without inhibit"}


def test_model_reports_data_changed_while_clk_high():
    kinds, model = bad_host("""
        od    uio0
        od    uio1
        clr   uio0
        delay 60                ; a proper inhibit
        clr   uio1
        set   uio0              ; request to send
        wtf   uio0              ; the device's first falling edge
        wtr   uio0              ; CLK is high again
        set   uio1              ; changing DATA now breaks the rule
        halt
    """)
    assert "data changed while clk high" in kinds
    assert "inhibit too short" not in kinds


def mutated_fw(old, new):
    with open(FW) as f:
        src = f.read()
    assert src.count(old) == 1, old
    words, syms, _ = keyerasm.assemble(src.replace(old, new), symbols=FAST)
    return keyerasm.to_list(words)


def test_model_reports_wrong_parity_and_withholds_the_ack():
    """The firmware with its parity seed flipped sends even parity: the
    device model reports it and does not acknowledge, and the firmware
    reports the missing ACK."""
    b = Bench()
    b.m.load(mutated_fw("ldi   r3, 1 ", "ldi   r3, 0 "))
    b.command(0xF4)
    b.run_until(lambda: len(b.out) == 2, 4 * FRAME)
    b.settle()
    assert b.out == [ESC, EV_NOACK]
    assert [e[0] for e in b.dev.errors] == ["host parity"] and b.dev.errors[0][2] == 0xF4
    assert b.dev.commands == []


def test_model_reports_a_stop_bit_held_low():
    """The firmware with CLC for the stop bit keeps DATA low: the device
    model reports the stop bit and records no command. (What follows is not
    checked: DATA stays low, which the device takes for further requests.)"""
    b = Bench()
    b.m.load(mutated_fw("setc   ", "clc    "))
    b.command(0xF4)
    b.run_until(lambda: b.dev.errors, 4 * FRAME)
    b.run(H)                                          # to the end of the eleventh pulse
    assert b.dev.errors == [("host stop bit", b.dev.errors[0][1], 0xF4)]
    assert b.dev.commands == [] and len(b.dev.bad_commands) == 1


# ---------------------------------------------------------------- limits and defaults

def test_fastest_clock():
    """The header's limit: a CLK half period of 20 cycles in both directions."""
    half = 20
    sym = {"PS2_TICK": 24, "PS2_INH_TICKS": 3, "PS2_RTS_TICKS": 30, "PS2_TMO_TICKS": 22}
    b = Bench(symbols=sym, half=half, min_inhibit=60, idle=TURNAROUND)
    b.dev.default_reply = [0xFA]
    data = [0x00, 0xFF, 0x55, 0xAA, ESC, 0x81]
    for v in data:
        b.dev.send(v)
    b.run_until(lambda: len(b.out) == len(data) + 1, 8 * (22 * half + TURNAROUND) + 1000)
    for cmd in (0x00, 0xFF, 0x55, 0xAA, 0xF4):
        n = len(b.out)
        b.command(cmd)
        b.run_until(lambda: len(b.out) == n + 1, 4 * 22 * half + 1000)
    b.run(200)
    assert b.out == stuffed(data) + [0xFA] * 5
    assert b.dev.commands == [0x00, 0xFF, 0x55, 0xAA, 0xF4] and b.dev.errors == []
    assert b.idle() and b.released()


def test_receive_only_down_to_a_half_period_of_9():
    """The header's receive limit, with the bus left idle between frames for
    the 71 cycles the firmware needs to be polling again."""
    half = 9
    sym = {"PS2_TICK": 12, "PS2_INH_TICKS": 3, "PS2_RTS_TICKS": 30, "PS2_TMO_TICKS": 20}
    b = Bench(symbols=sym, half=half, min_inhibit=30, idle=TURNAROUND)
    data = [0x00, 0xFF, 0x55, 0xAA, ESC, 0x81, 0x7E]
    for v in data:
        b.dev.send(v)
    b.dev.send(0x3C, parity_error=True)
    b.dev.send(0x3C)
    b.run_until(lambda: not b.dev.pending, 10 * (22 * half + TURNAROUND) + 500)
    b.run(100)
    assert b.out == stuffed(data) + [ESC, EV_PARITY, 0x3C] and b.dev.errors == []


def test_turnaround_between_frames():
    """After the eleventh falling edge of a frame the firmware is polling
    for the next one within TURNAROUND cycles, whatever it had to push."""
    b = Bench(trace=True)
    b.dev.send(0x1C)
    b.dev.send(ESC)                                   # two pushes
    b.dev.send(0x55, parity_error=True)               # a report
    b.dev.send(0x66, stop_bit=0)
    b.run_until(lambda: not b.dev.pending, 6 * FRAME)
    b.settle()
    polls = [r.cycle for r in b.m.trace if r.pc == b.syms["idle"]]
    worst = 0
    for start, end, _ in b.dev.sent_log:
        last_fall = [f for f in b.dev.clk_falls if start <= f <= end][-1]
        worst = max(worst, min(p for p in polls if p > last_fall) - last_fall)
    assert 50 <= worst <= TURNAROUND, worst


@pytest.mark.parametrize("half", [1500, 2500])       # 16.7 kHz and 10 kHz at 50 MHz
def test_default_constants_at_50_mhz(half):
    """The .equ defaults against a device at both ends of the PS/2 clock
    range: a scan code and a command with its answer; at the slow end also an
    abandoned frame, whose report comes 1.9 to 2.0 ms after the frame's first
    falling edge, and the frame after it."""
    b = Bench(symbols=None, half=half, min_inhibit=5000, idle=2500, rts_delay=10000)
    b.dev.default_reply = [0xFA]
    frame = 22 * half
    b.dev.send(ESC)
    b.run_until(lambda: len(b.out) == 2, 2 * frame)
    b.command(0xF4)
    b.run_until(lambda: len(b.out) == 3, 4 * frame + 20000)
    assert b.out == [ESC, ESC, 0xFA] and b.dev.commands == [0xF4]
    start, length = b.dev.inhibits[0]
    assert 5000 <= length <= 5016                     # 100 us, the protocol's minimum
    if half == 2500:
        b.dev.send(0x77, stop_after=6)
        b.dev.send(0x5A, delay=100000)                # 2 ms later
        mark = b.m.cycle
        b.run_until(lambda: len(b.out) == 6, 4 * frame + 220000)
        assert b.out[3:] == [ESC, EV_RX_TMO, 0x5A]
        first_fall = [f for f in b.dev.clk_falls if f > mark][0]
        report = [c for c, v in b.out_log if c > mark and v == ESC][0]
        assert 95000 <= report - first_fall <= 100030, report - first_fall
    b.run(3 * half)
    assert b.dev.errors == [] and b.idle() and b.released()
