"""fw/ws2812.s on the golden model with the WS2812B pixel-chain model.
Run: python3 -m pytest tools/test_fw_ws2812.py -q

The model (tools/protomodels_ws2812.py) knows only the datasheet. Its
windows are derived here from the datasheet's nanoseconds and the clock
period; the firmware's constants are the datasheet's typical times divided
by the same clock period.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyerasm  # noqa: E402
import protomodels as pm  # noqa: E402
import protomodels_ws2812 as wsm  # noqa: E402
from keyersim import Machine  # noqa: E402

FW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fw", "ws2812.s")
DOUT = 18                                   # uo2
OK, UNDERRUN = 0x00, 0xFF

CLK_NS = 20                                 # 50 MHz
T = wsm.cycles_from_ns(CLK_NS)              # datasheet windows in cycles
REAL = {"WS_T0H": 400 // CLK_NS, "WS_T1H": 800 // CLK_NS, "WS_BIT": 1250 // CLK_NS // 2 * 2,
        "WS_RESET": 52000 // CLK_NS}        # 20, 40, 62 cycles; reset 2600 cycles = 52 us


def load_fw(symbols=None):
    with open(FW) as f:
        words, syms, _ = keyerasm.assemble(f.read(), symbols=symbols)
    assert max(words) < 256, "program too large: %d words" % (max(words) + 1)
    return keyerasm.to_list(words), syms


def frame(data):
    """One frame on the inbox: 16-bit byte count, little-endian, then the bytes."""
    return [len(data) & 0xFF, len(data) >> 8] + list(data)


def grb(data):
    return [tuple(data[i:i + 3]) for i in range(0, len(data) - 2, 3)]


def bits_of(data):
    return [(b >> i) & 1 for b in data for i in range(7, -1, -1)]


def run(m, cycles, models):
    for _ in range(cycles):
        for mod in models:
            mod.on_cycle(m)
        m.step()


def outbox(m):
    out = []
    while True:
        b = m.host_outbox_pop(0)
        if b is None:
            return out
        out.append(b)


def start(symbols, timing=T, trace=False):
    words, syms = load_fw(symbols)
    m = Machine(trace=trace)
    m.load(words)
    m.host_run(0, True)
    return m, syms, wsm.Ws2812Chain(pin=DOUT, **timing)


def span(sym, nbytes, frames=1):
    """Cycles for `frames` frames of `nbytes` bytes in total, with every reset gap."""
    return (frames + 1) * (sym["WS_RESET"] + 100) + nbytes * 8 * sym["WS_BIT"] + 400


def check_timing(chain, sym, timing=T):
    """Every edge inside a frame: exactly the firmware's constants, and
    inside the datasheet windows (the second is what the strip needs; the
    first shows the edges come from the timer, not from the code path)."""
    assert chain.highs
    for c, high, bit in chain.highs:
        assert high == (sym["WS_T1H"] if bit else sym["WS_T0H"]), (c, high, bit)
        w = timing["t1h"] if bit else timing["t0h"]
        assert w[0] <= high <= w[1], (c, high, bit)
    for c, low, bit in chain.lows:
        assert low == sym["WS_BIT"] - (sym["WS_T1H"] if bit else sym["WS_T0H"]), (c, low, bit)
        w = timing["t1l"] if bit else timing["t0l"]
        assert w[0] <= low <= w[1], (c, low, bit)
    for c, per in chain.periods:
        assert per == sym["WS_BIT"], (c, per)
        assert timing["period"][0] <= per <= timing["period"][1], (c, per)
    for c, low in chain.gaps:
        assert low >= sym["WS_RESET"] >= timing["reset"], (c, low)


class TimedFeeder:
    """A host that pushes each chunk no earlier than its cycle, as space allows."""

    def __init__(self, tid, chunks):
        self.tid, self.chunks = tid, [(c, list(d)) for c, d in chunks]

    @property
    def pending(self):
        return sum(len(d) for _, d in self.chunks)

    def on_cycle(self, m):
        while self.chunks and m.cycle >= self.chunks[0][0]:
            data = self.chunks[0][1]
            while data and m.host_inbox_push(self.tid, data[0]):
                data.pop(0)
            if data:
                return
            self.chunks.pop(0)


# ---------------------------------------------------------------- the model on its own

class Wave:
    """Stands in for the machine: plays a list of (level, cycles) to a model."""

    def __init__(self, pin):
        self.pin, self.cycle, self.level = pin, 0, 0

    def pad(self):
        return self.level << self.pin

    def play(self, model, segments):
        for level, n in segments:
            self.level = level
            for _ in range(n):
                model.on_cycle(self)
                self.cycle += 1


def wave(bits, t0h=20, t1h=40, period=62):
    out = []
    for b in bits:
        high = t1h if b else t0h
        out += [(1, high), (0, period - high)]
    return out


def test_model_decodes_a_datasheet_waveform():
    chain = wsm.Ws2812Chain(pin=DOUT, **T)
    data = [0x12, 0xFF, 0x00, 0x80, 0x01, 0xA5]
    w = Wave(DOUT)
    w.play(chain, [(0, 50)] + wave(bits_of(data)))
    assert chain.frames == []                       # nothing latches before the reset time
    w.play(chain, [(0, T["reset"])])
    assert chain.frames == [data] and chain.errors == []
    assert chain.pixels == [[(0x12, 0xFF, 0x00), (0x80, 0x01, 0xA5)]]
    assert [b for _, _, b in chain.highs] == bits_of(data)
    assert len(chain.periods) == 47 and {p for _, p in chain.periods} == {62}
    assert {(low, b) for _, low, b in chain.lows} == {(42, 0), (22, 1)}
    # the window edges are part of the window: 0.26 us and 0.54 us are a 0, 0.66 and 0.94 a 1
    chain = wsm.Ws2812Chain(pin=DOUT, **T)
    w = Wave(DOUT)
    w.play(chain, [(0, 9)] + [(1, 13), (0, 49), (1, 27), (0, 35), (1, 33), (0, 30), (1, 47), (0, 15)] * 6
           + [(0, T["reset"])])
    assert chain.errors == [] and chain.frames == [[0x33] * 3]


def test_model_cycle_windows_follow_the_clock():
    assert T == {"t0h": (13, 27), "t1h": (33, 47), "t0l": (35, 50), "t1l": (15, 30),
                 "period": (33, 92), "reset": 2501}
    t30 = wsm.cycles_from_ns(30)
    assert t30["t0h"] == (9, 18) and t30["t1h"] == (22, 31) and t30["t1l"] == (10, 20)
    assert t30["reset"] == 1667                     # 1667 * 30 ns is the first time above 50 us


def test_model_reports_timing_violations():
    def errors(segments):
        chain = wsm.Ws2812Chain(pin=DOUT, **T)
        Wave(DOUT).play(chain, [(0, 10)] + segments + [(0, T["reset"])])
        return chain

    good = wave([1, 0] * 12)
    assert errors(good).errors == []
    # a high time between the two windows (0.60 us), below the 0 window, above the 1 window
    for high, bit in ((30, 0), (31, 1), (12, 0), (48, 1)):
        chain = errors(good[:4] + [(1, high), (0, 62 - high)] + good[6:])
        assert [e[:1] + e[2:] for e in chain.errors if e[0] == "high time"] == [("high time", high)], high
        assert chain.highs[2][2] == bit             # taken as the nearer code, framing carries on
        assert len(chain.frames[0]) == 3
    # a line stuck high is reported once, when it has been high longer than a 1
    chain = errors(good[:4] + [(1, 500), (0, 22)] + good[6:])
    assert [e for e in chain.errors if e[0] == "high time"] == [("high time", 10 + 124 + 47, 48)]
    # a stretched low (a byte fetched too late): shorter than the reset time, so an error
    for low in (51, 93, 400, T["reset"] - 1):
        chain = errors(good[:3] + [(0, low)] + good[4:])
        kinds = [e[0] for e in chain.errors]
        assert "low time" in kinds, low
        assert ("bit period" in kinds) == (20 + low > 92), low
        assert chain.frames == [[0xAA] * 3]         # still one frame: the low did not latch
    # a low time that is too short for its bit
    chain = errors(good[:1] + [(0, 14)] + good[2:])
    assert chain.errors == [("low time", 10 + 40 + 14, 14, 1)]
    chain = errors(good[:3] + [(0, 34)] + good[4:])
    assert [e[0] for e in chain.errors] == ["low time"] and chain.errors[0][2:] == (34, 0)
    # the period is checked on its own: with the datasheet windows a legal high and a legal low
    # always add up to a legal period, so use windows where they do not
    loose = dict(t0h=(10, 30), t1h=(31, 50), t0l=(10, 60), t1l=(10, 60), period=(60, 64), reset=2501)
    chain = wsm.Ws2812Chain(pin=DOUT, **loose)
    Wave(DOUT).play(chain, good[:2] + [(1, 20), (0, 45), (1, 40), (0, 19)] + good[6:] + [(0, 2501)])
    assert chain.errors == [("bit period", 62 + 65, 65), ("bit period", 62 + 65 + 59, 59)]


def test_model_partial_pixel_and_reset_boundary():
    chain = wsm.Ws2812Chain(pin=DOUT, **T)
    w = Wave(DOUT)
    w.play(chain, [(0, 5)] + wave([1] * 20) + [(0, T["reset"])])
    assert chain.errors == [("partial pixel", chain.latched[0], 20)]
    assert chain.frames == [[0xFF, 0xFF]] and chain.pixels == [[]]
    # exactly the reset time latches; the next bits are a new frame and the gap is recorded
    w.play(chain, wave(bits_of([1, 2, 3])) + [(0, T["reset"])])
    assert chain.frames[1] == [1, 2, 3] and chain.pixels[1] == [(1, 2, 3)]
    assert len(chain.errors) == 1 and len(chain.gaps) == 1
    assert chain.gaps[0][1] == 22 + T["reset"]      # the last bit's own low time is part of the gap
    assert chain.latched[1] - chain.highs[-1][0] == T["reset"] - 1
    # a low of exactly the reset time separates two frames; one cycle less does not
    chain = wsm.Ws2812Chain(pin=DOUT, **T)
    one = wave(bits_of([7, 8, 9]))[:-1]
    Wave(DOUT).play(chain, one + [(0, T["reset"])] + one + [(0, T["reset"] - 1)] + one + [(0, T["reset"])])
    assert chain.frames == [[7, 8, 9], [7, 8, 9, 7, 8, 9]]
    assert [g for _, g in chain.gaps] == [T["reset"]]
    assert [e[0] for e in chain.errors] == ["low time", "bit period"]
    assert chain.errors[0][2:] == (T["reset"] - 1, 1)


# ---------------------------------------------------------------- frames

def test_ws2812_one_led_default_constants():
    """The firmware's defaults are the 50 MHz datasheet values; one LED."""
    m, syms, chain = start(None)
    assert (syms["WS_T0H"], syms["WS_T1H"], syms["WS_BIT"], syms["WS_RESET"]) == (20, 40, 62, 3000)
    assert syms["DOUT"] == DOUT
    data = [0xA5, 0x3C, 0x81]                       # G, R, B
    for b in frame(data):
        assert m.host_inbox_push(0, b)
    run(m, span(syms, 3), [chain])
    assert chain.errors == []
    assert chain.frames == [data] and chain.pixels == [[(0xA5, 0x3C, 0x81)]]
    assert [b for _, _, b in chain.highs] == bits_of(data)
    check_timing(chain, syms)
    assert len(chain.highs) == 24 and len(chain.periods) == 23
    assert chain.highs[0][0] - chain.highs[0][1] >= 3000    # the start-up reset time comes first
    assert outbox(m) == [OK]
    t = m.threads[0]
    assert t.running and t.blocked and t.pc == syms["ws_frame"]     # waiting for the next header
    assert m.uo_out == 0                                            # line idle low


def test_ws2812_several_leds_zero_one_and_mixed_bytes():
    m, syms, chain = start(REAL)
    data = [0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xA5, 0x3C, 0x81, 0x01, 0x80, 0x7E]
    for b in frame(data):
        assert m.host_inbox_push(0, b)
    run(m, span(REAL, len(data)), [chain])
    assert chain.errors == []
    assert chain.frames == [data]
    assert chain.pixels == [[(0, 0, 0), (0xFF, 0xFF, 0xFF), (0xA5, 0x3C, 0x81), (0x01, 0x80, 0x7E)]]
    check_timing(chain, REAL)
    # the byte boundaries (every eighth period) are bit periods like any other,
    # after a 0 (bytes 0x00, 0x80, 0x7E...) and after a 1 (0xFF, 0xA5, 0x81...)
    boundary = chain.periods[7::8]
    assert len(boundary) == len(data) - 1 and {p for _, p in boundary} == {REAL["WS_BIT"]}
    rises = [c - h for c, h, _ in chain.highs]
    assert rises[-1] - rises[0] == (len(data) * 8 - 1) * REAL["WS_BIT"]     # no drift over the frame
    assert outbox(m) == [OK]


def test_ws2812_long_frame_refilled_by_the_host():
    """100 LEDs, 300 bytes: the count needs its high byte, and the frame is
    nineteen times the inbox, which HostFeeder keeps topped up while the
    frame is going out."""
    m, syms, chain = start(REAL)
    data = [(i * 37 + 11) & 0xFF for i in range(300)]
    data[30:36] = [0x00, 0xFF, 0x00, 0xFF, 0x55, 0xAA]
    feeder = pm.HostFeeder(0, frame(data))
    assert frame(data)[:2] == [0x2C, 0x01]

    class Probe:                                    # how much was still to come at the first rise
        left = None

        def on_cycle(self, mm):
            if self.left is None and (mm.pad() >> DOUT) & 1:
                self.left = len(feeder.pending)

    probe = Probe()
    run(m, span(REAL, len(data)), [feeder, chain, probe])
    # the feeder tops the inbox up as the header and the first byte are popped,
    # so all but 16 + 3 of the 302 bytes arrive while the frame is going out
    assert not feeder.pending and probe.left == 302 - 16 - 3
    assert chain.errors == []
    assert chain.frames == [data] and chain.pixels == [grb(data)]
    assert len(chain.highs) == 2400 and len(chain.periods) == 2399
    check_timing(chain, REAL)
    assert outbox(m) == [OK]


def test_ws2812_two_frames_separated_by_the_reset_time():
    m, syms, chain = start(REAL)
    a, b = [0x10, 0x20, 0x30, 0xFF, 0x00, 0xFF], [0x0F, 0xF0, 0x55]
    for x in frame(a) + frame(b):
        assert m.host_inbox_push(0, x)
    run(m, span(REAL, 9, frames=2), [chain])
    assert chain.errors == []
    assert chain.frames == [a, b] and chain.pixels == [grb(a), grb(b)]
    check_timing(chain, REAL)
    assert len(chain.gaps) == 1
    gap = chain.gaps[0][1]
    assert gap >= REAL["WS_RESET"] >= T["reset"]
    assert gap <= REAL["WS_RESET"] + REAL["WS_BIT"] + 60        # and not much more: no dead time
    assert chain.latched[0] < chain.gaps[0][0]                  # frame 1 latched before frame 2 began
    assert outbox(m) == [OK, OK]


def test_ws2812_zero_length_frame_is_ignored():
    m, syms, chain = start(REAL)
    data = [1, 2, 3]
    for x in [0, 0] + frame(data) + [0, 0]:
        assert m.host_inbox_push(0, x)
    run(m, span(REAL, 3), [chain])
    assert chain.frames == [data] and chain.errors == []
    assert outbox(m) == [OK]                         # one status: the empty frames have none
    assert not m.threads[0].inbox and m.threads[0].pc == syms["ws_frame"]


def test_ws2812_status_bytes_must_be_read():
    """One status per frame and none is ever dropped: with 16 unread the
    thread waits in its PUSH (line low, frame already out) until the host
    reads one."""
    m, syms, chain = start(REAL)
    stream = []
    for i in range(17):
        stream += frame([i, 0x80 | i, 0xFF - i])
    feeder = pm.HostFeeder(0, stream)
    run(m, span(REAL, 3 * 17, frames=17), [feeder, chain])
    t = m.threads[0]
    assert len(chain.frames) == 17 and chain.errors == []
    assert len(t.outbox) == 16 and t.blocked and t.pc == syms["ws_end"]
    assert m.host_outbox_pop(0) == OK
    run(m, REAL["WS_RESET"] + 100, [feeder, chain])
    assert outbox(m) == [OK] * 16 and t.pc == syms["ws_frame"] and not feeder.pending


def test_ws2812_first_byte_may_come_late():
    """Only a frame that is on the wire can underrun. After the header the
    thread waits for the first byte for as long as the host takes."""
    data = [0x42, 0x00, 0xFF]
    late = REAL["WS_RESET"] + 20000
    feeder = TimedFeeder(0, [(0, frame(data)[:2]), (late, data)])
    m, syms, chain = start(REAL)
    run(m, late, [feeder, chain])
    t = m.threads[0]
    assert t.blocked and t.pc == syms["ws_frame"] + 5 and t.regs[3] == 3      # at the first byte's POP
    assert chain.highs == [] and outbox(m) == []
    run(m, span(REAL, 3), [feeder, chain])
    assert chain.frames == [data] and chain.errors == []
    assert late < chain.highs[0][0] - chain.highs[0][1] < late + 60           # starts when the byte arrives
    check_timing(chain, REAL)
    assert outbox(m) == [OK]


def test_ws2812_restart_in_mid_frame():
    """The host stops the thread while the line is high, clears the FIFOs,
    points PC0 back at ws_init and runs it again: the line must drop at once
    and the next frame must follow a full reset time and decode whole. The
    model's only complaints are about the frame that was cut off."""
    m, syms, chain = start(REAL)
    for b in frame([0xFF] * 6):
        m.host_inbox_push(0, b)
    for _ in range(REAL["WS_RESET"] + 400):         # into the third bit, line high
        if len(chain.highs) == 2 and (m.pad() >> DOUT) & 1:
            break
        run(m, 1, [chain])
    else:
        raise AssertionError("the frame never reached its third bit")
    m.host_run(0, False)
    run(m, 300, [chain])                            # the host's SPI traffic takes about this long
    assert (m.pad() >> DOUT) & 1                    # stopped with the line high
    m.host_fifo_clear(0b0011)
    assert m.host_set_pc(0, syms["ws_init"])
    m.host_run(0, True)
    restart = m.cycle
    run(m, 3, [chain])
    assert (m.pad() >> DOUT) & 1 == 0               # the first instruction drops the line
    new = [0x12, 0x34, 0x56]
    for b in frame(new):
        assert m.host_inbox_push(0, b)
    run(m, span(REAL, 3), [chain])
    assert chain.frames[-1] == new and chain.pixels[-1] == [(0x12, 0x34, 0x56)]
    assert chain.gaps[-1][1] >= REAL["WS_RESET"]
    assert [e[0] for e in chain.errors] == ["high time", "partial pixel"]
    assert all(e[1] < restart + T["reset"] + 4 for e in chain.errors)
    assert outbox(m) == [OK]


# ---------------------------------------------------------------- underrun

def test_ws2812_underrun_is_reported_and_the_stream_stays_in_step():
    """The host stops after the first LED of a 3-LED frame. The strip must
    see a clean short frame (one LED, no stretched bit), the host a 0xFF
    status once it has delivered the rest (which is dropped), and the next
    frame must go out whole."""
    a = [0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99]
    b = [0xC3, 0x00, 0xFF]
    resume = 4 * REAL["WS_RESET"]
    feeder = TimedFeeder(0, [(0, frame(a)[:5]), (resume, a[3:] + frame(b))])
    m, syms, chain = start(REAL)
    run(m, resume, [feeder, chain])
    # stalled: the short frame has latched, nothing reported yet, thread waits for the rest
    assert chain.frames == [a[:3]] and chain.errors == []
    t = m.threads[0]
    assert t.blocked and t.pc == syms["ws_underrun"] and t.regs[3] == 6
    assert outbox(m) == [] and m.uo_out == 0
    run(m, span(REAL, 3), [feeder, chain])
    assert not feeder.pending
    assert outbox(m) == [UNDERRUN, OK]
    assert chain.frames == [a[:3], b] and chain.pixels == [[(0x11, 0x22, 0x33)], [(0xC3, 0x00, 0xFF)]]
    assert chain.errors == []                        # a short frame of whole pixels is legal
    check_timing(chain, REAL)
    assert chain.gaps[0][1] >= resume - 2 * REAL["WS_RESET"]
    assert t.blocked and t.pc == syms["ws_frame"] and not t.inbox


def test_ws2812_underrun_inside_a_pixel():
    """Starved after 4 of 6 bytes: the strip latches one pixel and 8 stray
    bits. The model reports exactly that; no bit is stretched or malformed."""
    a = [0x80, 0x01, 0xFF, 0x5A, 0xEE, 0xDD]
    feeder = TimedFeeder(0, [(0, frame(a)[:6]), (3 * REAL["WS_RESET"] + 3000, a[4:])])
    m, syms, chain = start(REAL)
    run(m, 3 * REAL["WS_RESET"] + 6000, [feeder, chain])
    assert chain.frames == [a[:4]] and chain.pixels == [[(0x80, 0x01, 0xFF)]]
    assert chain.errors == [("partial pixel", chain.latched[0], 32)]
    check_timing(chain, REAL)
    assert outbox(m) == [UNDERRUN]
    assert m.threads[0].pc == syms["ws_frame"] and not m.threads[0].inbox


def test_ws2812_underrun_deadline_is_the_fetch_slot():
    """The byte must be in the inbox at the slot of the timed POP, which
    sits in the low phase of the last bit of the byte before. One cycle later
    is an underrun, and the late byte is dropped, not sent."""
    data = [0xFF, 0x0F, 0x01]
    m, syms, chain = start(REAL, trace=True)
    for b in frame(data):
        m.host_inbox_push(0, b)
    run(m, span(REAL, 3), [chain])
    fetch = [r.cycle for r in m.trace if r.tid == 0 and r.pc == syms["ws_byte"]]
    assert len(fetch) == 2 and all(r.done for r in m.trace if r.pc == syms["ws_byte"])
    falls = [c for c, _, _ in chain.highs]
    # the fetch of byte 1 comes after the last fall of byte 0 and before the next rise
    assert falls[7] < fetch[0] < falls[8] - chain.highs[8][1]
    for late, status, frames in ((0, OK, [data]), (1, UNDERRUN, [data[:1]])):
        m, syms, chain = start(REAL)
        for b in frame(data)[:3]:
            m.host_inbox_push(0, b)
        feeder = TimedFeeder(0, [(fetch[0] + late, data[1:])])
        run(m, span(REAL, 3), [feeder, chain])
        assert outbox(m) == [status], late
        assert chain.frames == frames, late
        assert not m.threads[0].inbox and m.threads[0].pc == syms["ws_frame"]
        check_timing(chain, REAL)


# ---------------------------------------------------------------- other timings

def test_ws2812_datasheet_timing_at_another_clock():
    """33.3 MHz (30 ns): the datasheet times divided by the clock period."""
    sym = {"WS_T0H": 14, "WS_T1H": 26, "WS_BIT": 42, "WS_RESET": 1700}     # 0.42, 0.78, 1.26 us, 51 us
    t30 = wsm.cycles_from_ns(30)
    m, syms, chain = start(sym, timing=t30)
    data = [0x00, 0xFF, 0x96, 0x69, 0x80, 0x01]
    for b in frame(data):
        m.host_inbox_push(0, b)
    run(m, span(sym, len(data)), [chain])
    assert chain.errors == [] and chain.frames == [data]
    check_timing(chain, sym, t30)
    assert outbox(m) == [OK]


def exact(t0h, t1h, bit, reset):
    """A model that accepts only these exact times (for the limit tests)."""
    return dict(t0h=(t0h, t0h), t1h=(t1h, t1h), t0l=(bit - t0h, bit - t0h), t1l=(bit - t1h, bit - t1h),
                period=(bit, bit), reset=reset)


def play_exact(t0h, t1h, bit, data):
    sym = {"WS_T0H": t0h, "WS_T1H": t1h, "WS_BIT": bit, "WS_RESET": 300}
    m, syms, chain = start(sym, timing=exact(t0h, t1h, bit, 200))
    for b in frame(data):
        m.host_inbox_push(0, b)
    run(m, span(sym, len(data)), [chain])
    return m, sym, chain


def test_ws2812_fastest_timing_and_what_limits_it():
    """The header's limits: T0H >= 6, T1H - T0H >= 4, BIT - T1H >= 16 cycles.
    At the limits every edge is still on the grid, byte boundaries included
    (26 cycles per bit, 1.9 Mbit/s at 50 MHz). One tick below any of them an
    edge is late, which the model (given exact windows) catches."""
    data = [0xFF, 0x00, 0xA5, 0x81, 0x7E, 0x01]
    m, sym, chain = play_exact(6, 10, 26, data)
    assert chain.errors == [] and chain.frames == [data]
    check_timing(chain, sym, exact(6, 10, 26, 200))
    assert outbox(m) == [OK]
    for t0h, t1h, bit in ((4, 10, 26), (6, 8, 24), (6, 10, 24)):
        m, sym, chain = play_exact(t0h, t1h, bit, data)
        assert chain.errors, (t0h, t1h, bit)


def test_ws2812_byte_boundary_needs_16_cycles_of_low_time():
    """The byte boundary on its own (the high phases have slack here). With
    16 cycles between the fall of a 1 and the next rise the six instructions
    of the boundary fit and every period is the same. With 14 the first rise
    of every byte after the first is one slot late: the period before it is
    2 cycles long, that bit's high time and period 2 cycles short, and
    nothing else moves, because the falls stay on the timer grid."""
    data = [0xFF, 0x00, 0xA5, 0x81, 0x7E, 0x01]
    m, sym, chain = play_exact(10, 20, 36, data)
    assert chain.errors == [] and chain.frames == [data]
    check_timing(chain, sym, exact(10, 20, 36, 200))

    m, sym, chain = play_exact(10, 20, 34, data)
    assert chain.frames == [data] and outbox(m) == [OK]
    for i, (c, high, b) in enumerate(chain.highs):
        late = 2 if i % 8 == 0 and i > 0 else 0
        assert high == (20 if b else 10) - late, (i, high)
    for i, (c, per) in enumerate(chain.periods):        # period i runs from bit i to bit i + 1
        assert per == 34 + (2 if i % 8 == 7 else -2 if i % 8 == 0 and i > 0 else 0), (i, per)
    kinds = {e[0] for e in chain.errors}
    assert kinds == {"high time", "low time", "bit period"}
    assert len([e for e in chain.errors if e[0] == "high time"]) == len(data) - 1
