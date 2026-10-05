"""WS2812B pixel-chain model for the firmware tests (ISS and lockstep).

It knows the WS2812B datasheet and nothing else: the line idles low, every
bit is one high pulse followed by a low time, the bit's value is the width
of the high pulse, 24 bits make one pixel (green, red, blue, each MSB first)
and a low time longer than the reset time latches the frame. Same interface
as tools/protomodels.py: on_cycle(m) before every Machine.step(), reading
m.pad() and m.cycle. The model only listens; it drives no pin.
"""

import math

# Data transfer times from the WS2812B datasheet, in nanoseconds:
# T0H 0.40 us, T1H 0.80 us, T0L 0.85 us, T1L 0.45 us, each +/- 150 ns;
# TH + TL = 1.25 us +/- 600 ns; RES: low for more than 50 us.
WS2812B_NS = {
    "t0h": (250, 550),
    "t1h": (650, 950),
    "t0l": (700, 1000),
    "t1l": (300, 600),
    "period": (650, 1850),
    "reset": 50000,
}


def cycles_from_ns(clk_ns, spec=None):
    """The datasheet windows as whole core cycles for a clock period in ns:
    the keyword arguments of Ws2812Chain. A window (lo, hi) holds every cycle
    count n with lo <= n * clk_ns <= hi; reset is the first count whose time
    is above the datasheet's reset time."""
    spec = WS2812B_NS if spec is None else spec
    out = {}
    for name, v in spec.items():
        if name == "reset":
            out[name] = int(math.floor(v / clk_ns + 1e-9)) + 1
        else:
            out[name] = (int(math.ceil(v[0] / clk_ns - 1e-9)), int(math.floor(v[1] / clk_ns + 1e-9)))
    return out


class Ws2812Chain:
    """A chain of WS2812B pixels listening on one pad pin.

    Timing parameters are in core cycles: t0h, t1h, t0l, t1l and period are
    (min, max) windows, reset is the low time that latches. Measures every
    high time, low time and rise-to-rise period; a bit is 0 or 1 by the
    window its high time falls in. When the line has been low for `reset`
    cycles the bits received since the last latch become one frame.

    Recorded: frames (a list of bytes per frame), pixels (the same frames as
    (g, r, b) tuples), latched (the cycle each frame latched), highs
    [(cycle of the fall, high time, bit)], lows [(cycle of the rise, low time,
    bit before it)] and periods [(cycle of the rise, period)] for the edges
    inside a frame, gaps [(cycle of the rise, low time)] for the low between
    two frames.

    errors: ("high time", cycle, n) for a high time in neither window (the
    bit is then taken as the nearer code so the framing carries on);
    ("low time", cycle, n, bit) and ("bit period", cycle, n) for a low time or
    period outside its window but shorter than the reset time;
    ("partial pixel", cycle, nbits) for a frame that is not a multiple of 24
    bits (frames and pixels keep its whole bytes and whole pixels).
    """

    def __init__(self, pin, t0h, t1h, t0l, t1l, period, reset):
        self.pin = pin
        self.t0h, self.t1h, self.t0l, self.t1l = tuple(t0h), tuple(t1h), tuple(t0l), tuple(t1l)
        self.period, self.reset = tuple(period), reset
        self.frames = []
        self.pixels = []
        self.latched = []
        self.highs = []
        self.lows = []
        self.periods = []
        self.gaps = []
        self.errors = []
        self._prev = 0            # the line idles low
        self._rise = None         # cycle of the last rising edge
        self._fall = None         # cycle of the last falling edge
        self._bits = []           # bits since the last latch
        self._long_high = False   # the current high already exceeded the 1 window

    @staticmethod
    def _in(window, n):
        return window[0] <= n <= window[1]

    def _latch(self, cycle):
        bits, self._bits = self._bits, []
        if len(bits) % 24:
            self.errors.append(("partial pixel", cycle, len(bits)))
        data = []
        for i in range(0, len(bits) - 7, 8):
            v = 0
            for b in bits[i:i + 8]:
                v = (v << 1) | b
            data.append(v)
        self.frames.append(data)
        self.pixels.append([tuple(data[i:i + 3]) for i in range(0, len(data) - 2, 3)])
        self.latched.append(cycle)

    def on_cycle(self, m):
        lvl = (m.pad() >> self.pin) & 1
        c = m.cycle
        if lvl and not self._prev:                          # rising edge
            if self._fall is not None:
                low = c - self._fall
                if low >= self.reset:                       # the frame before it has latched
                    self.gaps.append((c, low))
                else:
                    bit = self._bits[-1]
                    self.lows.append((c, low, bit))
                    if not self._in(self.t1l if bit else self.t0l, low):
                        self.errors.append(("low time", c, low, bit))
                    per = c - self._rise
                    self.periods.append((c, per))
                    if not self._in(self.period, per):
                        self.errors.append(("bit period", c, per))
            self._rise = c
            self._long_high = False
        elif self._prev and not lvl:                        # falling edge
            high = c - self._rise
            if self._in(self.t0h, high):
                bit = 0
            elif self._in(self.t1h, high):
                bit = 1
            else:
                bit = 1 if 2 * high > self.t0h[1] + self.t1h[0] else 0
                if not self._long_high:
                    self.errors.append(("high time", c, high))
            self.highs.append((c, high, bit))
            self._bits.append(bit)
            self._fall = c
        elif lvl:                                           # still high: stuck?
            if not self._long_high and c - self._rise + 1 > self.t1h[1]:
                self._long_high = True                      # reported once, here
                self.errors.append(("high time", c, c - self._rise + 1))
        elif self._bits and c - self._fall + 1 == self.reset:
            self._latch(c)                                  # low for the reset time
        self._prev = lvl
