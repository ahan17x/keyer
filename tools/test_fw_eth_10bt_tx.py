"""fw/eth_10bt_tx.s on the golden model against a 10BASE-T receiver that
knows only IEEE 802.3 (tools/protomodels_eth_10bt.py).
Run: python3 -m pytest tools/test_fw_eth_10bt_tx.py -q

The link pulse interval is shortened in most tests: LP_PERIOD = 10 us of
idle tick and LP_TICKS = 8, so 80 us instead of 16 ms (lp_scale = 0.005 for
the receiver's 8..24 ms window, which becomes 40..120 us).
test_link_pulses_at_full_interval runs the firmware's own 16 ms. The host
is modelled as an SPI master at clk/8: one byte reaches the inbox every 72
cycles (8 bits of 8 cycles plus a gap), 1.8 us per byte at 40 MHz, slower
than the 0.8 us a byte takes on the wire.
"""

import binascii
import collections
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyer_isa as isa  # noqa: E402
import keyerasm  # noqa: E402
from keyersim import Machine  # noqa: E402
import protomodels_eth_10bt as eth  # noqa: E402
from protomodels_eth_10bt import Eth10BTReceiver  # noqa: E402

FW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fw", "eth_10bt_tx.s")
WORDS = 88                              # the image size the header states
DST = [0xFF] * 6
SRC = [0x02, 0x4B, 0x45, 0x59, 0x45, 0x52]
ETYPE = 0x88B5
QUEUE_OPS = ("SERI", "SERIC", "SERTX", "SERTXC")


def assemble(**symbols):
    words, syms, _ = keyerasm.assemble(open(FW).read(), symbols=symbols or None)
    return keyerasm.to_list(words), syms


def expected_payload(block):
    cmd = block[0]
    n, k = cmd & 15, cmd >> 4
    p = [cmd] + list(block[1:1 + n])
    if k:
        p += [i & 0xFF for i in range(96 * k)]
    else:
        p += [0] * (46 - len(p))
    return p


def block(cmd, data=()):
    data = list(data)
    return [cmd] + data + [0xEE] * (15 - len(data))


class Bench:
    """The firmware on the ISS, the receiver on its pads, and an SPI host
    that delivers one inbox byte every `byte_cycles` cycles."""

    def __init__(self, clk=40000, short=True, byte_cycles=72, **symbols):
        syms = dict(CLK_KHZ=clk)
        if short:
            syms.update(LP_PERIOD=400 * clk // 40000, LP_TICKS=8)
        syms.update(symbols)
        self.words, self.syms = assemble(**syms)
        self.m = Machine()
        self.m.load(self.words)
        self.m.host_run(0, True)
        self.clk_ns = 1e6 / clk
        self.interval_ns = self.syms["LP_PERIOD"] * self.syms["LP_TICKS"] * self.clk_ns
        self.rx = Eth10BTReceiver(clk_ns=self.clk_ns, lp_scale=self.interval_ns / 16e6)
        self.q = collections.deque()
        self.byte_cycles = byte_cycles
        self._next = 0
        self.out = []
        self.queue_at = {a for a, w in enumerate(self.words) if isa.disasm(w).split()[0].upper() in QUEUE_OPS}
        self.waits = []                 # (cycle, pc, slots blocked) of every queueing instruction
        self._blk = 0
        self.fills, self.takes = [], []

    def stage(self, data):
        self.q.extend(data)

    def run(self, cycles, until=None):
        m, rx, t0 = self.m, self.rx, self.m.threads[0]
        for _ in range(cycles):
            if self.q and m.cycle >= self._next:
                if m.host_inbox_push(0, self.q[0]):
                    self.q.popleft()
                    self._next = m.cycle + self.byte_cycles
            rx.on_cycle(m)
            pc = t0.pc if m.cycle % 2 == 0 and t0.running else None
            full = m.ser.tx_full
            m.step()
            if pc in self.queue_at:
                if t0.blocked:
                    self._blk += 1
                else:
                    self.waits.append((m.cycle - 1, pc, self._blk))
                    self._blk = 0
            if m.ser.tx_full != full:
                (self.fills if m.ser.tx_full else self.takes).append(m.cycle - 1)
            b = m.host_outbox_pop(0)
            if b is not None:
                self.out.append(b)
            if until and until(self):
                return
        rx.finish()

    def frames_done(self, n):
        return lambda b: len(b.rx.frames) >= n

    def check_frame(self, f, blk):
        assert f.fcs_ok
        assert f.dst == DST and f.src == SRC and f.type == ETYPE
        assert f.payload == expected_payload(blk)
        body = f.bytes[:-4]
        assert f.fcs == binascii.crc32(bytes(body)) & 0xFFFFFFFF == eth.crc32(body)


def queue_waits(b):
    """Slots each byte-queueing instruction waited for the holding register,
    leaving out the first two of each frame (the engine is idle for them)."""
    pre = b.syms["pre"]
    return [w for _, pc, w in b.waits if pc not in (pre, pre + 1)]


def run_frames(blocks, clk=40000, gap=0, extra=4000, **kw):
    """Stage the blocks one after the other (the next as soon as the host
    has sent the previous one, plus `gap` cycles), run until all are on the
    wire and `extra` cycles more."""
    b = Bench(clk=clk, **kw)
    for blk in blocks:
        b.stage(blk)
    b.run(400000, until=b.frames_done(len(blocks)))
    b.run(extra)
    return b


# ------------------------------------------------------------------ the firmware's facts

def test_image_size_and_declared_header():
    words, syms = assemble()
    assert max(a for a, w in enumerate(words) if w) + 1 == WORDS
    seric = isa.encode("SERIC", n=0)
    hdr = [w & 0xFF for w in words[syms["hdr"]:syms["hdr_end"]] if w & 0xFF00 == seric]
    assert hdr == DST + SRC + [ETYPE >> 8, ETYPE & 0xFF]
    assert SRC[0] & 0x02 and not SRC[0] & 0x01       # locally administered, unicast
    # the documented symbols at 40 MHz and 20 MHz
    assert (syms["SYMT"], syms["LP_PERIOD"] * syms["LP_TICKS"]) == (2, 640000)    # 16 ms at 25 ns
    _, s20 = assemble(CLK_KHZ=20000)
    assert (s20["SYMT"], s20["LP_PERIOD"] * s20["LP_TICKS"], s20["PULSE_HOLD"]) == (1, 320000, 0)


def test_receiver_crc_against_binascii():
    assert eth.crc32(b"123456789") == 0xCBF43926          # the catalogue check value
    rng = random.Random(3)
    for n in (0, 1, 14, 60, 1514):
        data = bytes(rng.randrange(256) for _ in range(n))
        assert eth.crc32(data) == binascii.crc32(data) & 0xFFFFFFFF


# ------------------------------------------------------------------ link pulses

def test_link_pulses_alone():
    """No host: pulses only, 100 ns, positive, 80 us apart (the shortened
    interval), the pair at zero between them, no frame."""
    b = Bench()
    b.run(20 * 3200)
    assert b.rx.errors == []
    assert b.rx.frames == []
    starts = [p[0] for p in b.rx.pulses]
    assert len(starts) >= 18
    assert all(w == 100.0 and lv == eth.POS for _, w, lv in b.rx.pulses)
    gaps = {round(y - x) for x, y in zip(starts, starts[1:])}
    assert gaps == {80300}, gaps            # 8 ticks of 10 us, plus 12 cycles from SET to SETT and of the idle loop
    assert b.out == []


def test_link_pulses_at_full_interval():
    """The firmware's own interval at 40 MHz against the standard's 16 +- 8 ms."""
    b = Bench(short=False)
    assert b.rx.lp_min == 8e6 and b.rx.lp_max == 24e6
    b.run(2 * 640000 + 2000)
    assert b.rx.errors == []
    starts = [p[0] for p in b.rx.pulses]
    assert len(starts) == 2
    assert 16.0e6 <= starts[1] - starts[0] <= 16.001e6
    assert b.rx.pulses[0][1] == 100.0


# ------------------------------------------------------------------ frames

def test_one_frame_byte_for_byte():
    blk = block(0x05, [0xDE, 0xAD, 0xBE, 0xEF, 0x42])
    b = run_frames([blk])
    assert b.rx.errors == []
    (f,) = b.rx.frames
    b.check_frame(f, blk)
    assert len(f.bytes) == 64
    assert f.payload[:6] == [0x05, 0xDE, 0xAD, 0xBE, 0xEF, 0x42] and set(f.payload[6:]) == {0}
    assert b.out == [0x05]
    assert f.bit_ns == 100.0


def test_minimum_size_padding():
    """cmd 0x00: no staged byte is sent; the payload is cmd and 45 zeros."""
    blk = block(0x00, range(1, 16))
    b = run_frames([blk])
    assert b.rx.errors == []
    (f,) = b.rx.frames
    b.check_frame(f, blk)
    assert len(f.bytes) == 64 and f.payload == [0] * 46
    assert b.out == [0x00]


def test_longest_payload_and_underrun_margin():
    """cmd 0xFF: 15 staged bytes and 15 * 96 counting bytes, a 1474-byte
    frame (payload 1456; 802.3 allows 1500). Every byte-queueing instruction
    after the first two waits at least 11 slots for the holding register:
    from one to the next is at most 5 slots of the 16 per byte at 40 MHz."""
    blk = block(0xFF, range(0xA0, 0xAF))
    b = run_frames([blk])
    assert b.rx.errors == []
    (f,) = b.rx.frames
    b.check_frame(f, blk)
    assert len(f.bytes) == 1474 and len(f.payload) == 1456
    assert f.payload[1:16] == list(range(0xA0, 0xAF))
    assert len(b.waits) == 8 + 14 + 1456
    waits = queue_waits(b)
    assert min(waits) == 11, sorted(collections.Counter(waits).items())
    # the holding register is never empty for more than 2 cycles in a frame
    holes = [fl - tk for tk, fl in zip(b.takes, b.fills[1:]) if fl - tk < 1000]
    assert len(holes) == 8 + 14 + 1456 - 1 and max(holes) <= 2


def test_two_frames_back_to_back():
    """The second block is staged while the first frame is on the wire; it
    goes out after the minimum gap."""
    b1 = block(0x03, [1, 2, 3])
    b2 = block(0x1F, range(0x60, 0x6F))
    b = Bench()
    b.stage(b1)
    b.run(100000, until=lambda x: x.m.ser.tx_state != 0)
    b.stage(b2)                                           # during frame 1
    b.run(100000, until=b.frames_done(2))
    b.run(4000)
    assert b.rx.errors == []
    f1, f2 = b.rx.frames
    b.check_frame(f1, b1)
    b.check_frame(f2, b2)
    gap = f2.start - f1.bits_end
    # 96 bit times from the end of the start of idle, + the 300 ns tail, + the
    # firmware's way from the gap to the next frame's first symbol
    assert gap == 11550.0, gap
    assert b.out == [0x03, 0x1F]


def test_frames_interleaved_with_link_pulses():
    """Blocks arrive at pseudo-random times against the pulse schedule; no
    pulse is ever sent during a frame, less than 8 ms (scaled: 40 us) after
    one, or less than the receiver's 9.6 us before one."""
    rng = random.Random(11)
    b = Bench()
    sent = []
    for i in range(10):
        b.run(rng.randrange(500, 9000))
        blk = block(rng.randrange(16), [rng.randrange(256) for _ in range(15)])
        sent.append(blk)
        b.stage(blk)
        b.run(100000, until=b.frames_done(i + 1))
    b.run(8000)
    assert b.rx.errors == []
    assert len(b.rx.frames) == 10 and len(b.rx.pulses) >= 5
    for f, blk in zip(b.rx.frames, sent):
        b.check_frame(f, blk)
        for p, w, _ in b.rx.pulses:
            assert not (f.start - 9600 < p + w and p < f.bits_end + b.rx.lp_min), (p, f.start)
    assert b.out == [blk[0] for blk in sent]


def find_data(last_bit):
    """A one-byte staged payload whose frame ends (the FCS's last bit) in `last_bit`."""
    for d in range(256):
        blk = block(0x01, [d])
        body = DST + SRC + [ETYPE >> 8, ETYPE & 0xFF] + expected_payload(blk)
        if (binascii.crc32(bytes(body)) >> 31) == last_bit:
            return blk
    raise AssertionError


@pytest.mark.parametrize("last_bit, soi", [(1, 350.0), (0, 300.0)])
def test_start_of_idle_after_final_one_and_zero(last_bit, soi):
    """802.3 14.3.1.2.1: positive for at least 250 ns from the last positive
    transition (the model also requires zero within 450 ns). After a final 1
    the last rising edge is mid-cell: 50 ns of half-bit plus 300 ns; after a
    final 0 it is the cell boundary: 300 ns. Either way the line is positive
    for 300 ns after the last bit cell, then both pads are low."""
    blk = find_data(last_bit)
    b = run_frames([blk])
    assert b.rx.errors == []
    (f,) = b.rx.frames
    b.check_frame(f, blk)
    assert f.fcs >> 31 == last_bit
    assert f.soi_ns == soi and f.tail_ns == 300.0
    assert 250.0 <= f.soi_ns <= 450.0


def test_host_that_does_not_stage():
    """Nothing staged: link pulses only. 15 bytes: still nothing (the block is
    incomplete). The 16th byte, much later: the frame goes out."""
    b = Bench()
    b.run(8 * 3200)
    assert b.rx.frames == [] and len(b.rx.pulses) >= 7
    blk = block(0x02, [0x11, 0x22])
    b.stage(blk[:15])
    b.run(10 * 3200)
    assert b.rx.frames == [] and b.out == []
    n = len(b.rx.pulses)
    assert n >= 15
    b.stage(blk[15:])
    b.run(100000, until=b.frames_done(1))
    b.run(4000)
    assert b.rx.errors == []
    (f,) = b.rx.frames
    b.check_frame(f, blk)
    assert b.out == [0x02]


def test_20mhz():
    """CLK_KHZ = 20000: period 1, 2-cycle pulses; from one byte to the next
    is at most 5 of the 8 slots per byte, so each waits at least 3."""
    blocks = [block(0x07, range(7)), block(0xF3, [9, 8, 7])]
    b = run_frames(blocks, clk=20000)
    assert b.rx.errors == []
    assert [len(f.bytes) for f in b.rx.frames] == [64, 14 + 1 + 3 + 1440 + 4]
    for f, blk in zip(b.rx.frames, blocks):
        b.check_frame(f, blk)
    assert all(w == 100.0 for _, w, _ in b.rx.pulses) and b.rx.pulses
    assert min(queue_waits(b)) == 3
    assert b.out == [0x07, 0xF3]


# ------------------------------------------------------------------ the receiver catches faults

def feed(rx, levels, start=0):
    for i, (p, n) in enumerate(levels):
        rx.sample(start + i, p, n)
    return start + len(levels)


def good_body(payload_len=46):
    return eth.frame_bytes(DST, SRC, ETYPE, [(7 * i) & 0xFF for i in range(payload_len)])


def kinds(rx):
    return {e[0] for e in rx.errors}


def test_receiver_accepts_a_clean_synthetic_frame():
    rx = Eth10BTReceiver()
    feed(rx, eth.wave(good_body(), lead=40, trail=40))
    rx.finish()
    assert rx.errors == []
    (f,) = rx.frames
    assert f.fcs_ok and f.payload == [(7 * i) & 0xFF for i in range(46)]


@pytest.mark.parametrize("fault, kind", [
    ("fcs", "fcs"),
    ("short", "length"),
    ("long", "length"),
    ("half", "half-bit"),
    ("violation", "manchester"),
    ("no_soi", "start of idle"),
    ("long_soi", "start of idle"),
    ("preamble", "preamble"),
    ("both_high", "illegal state"),
])
def test_receiver_catches_frame_faults(fault, kind):
    body = good_body()
    kw = dict(lead=40, trail=40)
    if fault == "fcs":
        body = eth.frame_bytes(DST, SRC, ETYPE, [(7 * i) & 0xFF for i in range(46)], fcs=0x12345678)
    elif fault == "short":
        body = good_body(40)                    # 60 bytes with a good FCS
    elif fault == "long":
        body = good_body(1501)
    elif fault == "half":
        kw["half_override"] = {300: 3}          # one half-bit of 75 ns
    elif fault == "violation":
        kw["half_override"] = {300: 4, 301: 0}  # a bit cell with no mid-cell transition
    elif fault == "no_soi":
        kw["soi"] = 0
    elif fault == "long_soi":
        kw["soi"] = 12                          # 600 ns
    elif fault == "preamble":
        kw["pre"] = [0x55] * 6 + [0xD5]         # one preamble byte short
    w = eth.wave(body, **kw)
    if fault == "both_high":
        w[20] = (1, 1)
    rx = Eth10BTReceiver()
    feed(rx, w)
    rx.finish()
    assert kind in kinds(rx), rx.errors
    if fault in ("short", "long"):
        assert rx.frames[0].fcs_ok and kinds(rx) == {"length"}
    if fault == "fcs":
        assert kinds(rx) == {"fcs"}


@pytest.mark.parametrize("last_bit", [0, 1])
def test_receiver_missing_start_of_idle_either_last_bit(last_bit):
    """With no start of idle the frame ends negative (final 0) or positive
    for one 50 ns half-bit (final 1): both are reported."""
    for d in range(256):
        body = eth.frame_bytes(DST, SRC, ETYPE, [d] * 46)
        if body[-1] >> 7 == last_bit:
            break
    rx = Eth10BTReceiver()
    feed(rx, eth.wave(body, soi=0, lead=40, trail=40))
    rx.finish()
    assert kinds(rx) == {"start of idle"}, rx.errors


def pulse(width_cycles, level=(1, 0)):
    return [level] * width_cycles


def test_receiver_catches_pulse_faults():
    # too wide: 300 ns
    rx = Eth10BTReceiver()
    feed(rx, [(0, 0)] * 10 + pulse(12) + [(0, 0)] * 10)
    assert kinds(rx) == {"link pulse"}
    # too narrow: 50 ns; negative
    rx = Eth10BTReceiver()
    feed(rx, [(0, 0)] * 10 + pulse(2) + [(0, 0)] * 10)
    t = feed(rx, [(0, 0)] * 10 + pulse(4, (0, 1)) + [(0, 0)] * 10, 400_000)
    assert [e[0] for e in rx.errors] == ["link pulse", "link pulse"]
    # spacing, on a window scaled to 8..24 us: 4 us is early, 30 us is late
    rx = Eth10BTReceiver(lp_scale=0.001)
    t = feed(rx, pulse(4) + [(0, 0)] * 156)             # pulse at 0, then 4 us of silence
    t = feed(rx, pulse(4) + [(0, 0)] * 1196, t)         # pulse at 4 us, then 30 us
    feed(rx, pulse(4) + [(0, 0)] * 10, t)
    assert [e[0] for e in rx.errors] == ["link pulse early", "link pulse late"], rx.errors
    # a good pulse train on the same window
    rx = Eth10BTReceiver(lp_scale=0.001)
    t = 0
    for _ in range(4):
        t = feed(rx, pulse(4) + [(0, 0)] * 636, t)      # 16 us apart
    assert rx.errors == [] and len(rx.pulses) == 4


def test_receiver_catches_a_stuck_pair():
    rx = Eth10BTReceiver()
    feed(rx, [(0, 0)] * 10 + [(1, 0)] * 500)        # positive for 12.5 us
    rx.finish()
    assert kinds(rx) == {"stuck", "link pulse"}


def test_receiver_catches_gap_and_pulse_spacing_around_frames():
    body = good_body()
    # two frames 5 us apart
    rx = Eth10BTReceiver()
    t = feed(rx, eth.wave(body, lead=40, trail=200))
    feed(rx, eth.wave(body, trail=40), t)
    assert kinds(rx) == {"gap"}, rx.errors
    # a frame 1 us after a link pulse; a pulse 2 us after the frame (scale 1: 8 ms)
    rx = Eth10BTReceiver()
    t = feed(rx, [(0, 0)] * 10 + pulse(4) + [(0, 0)] * 40)
    t = feed(rx, eth.wave(body, trail=80), t)
    feed(rx, pulse(4) + [(0, 0)] * 10, t)
    assert [e[0] for e in rx.errors] == ["pulse near frame", "link pulse early"], rx.errors
