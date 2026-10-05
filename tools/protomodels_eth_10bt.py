"""10BASE-T link partner (receive side only), for checking a transmitter on the ISS and the RTL.

Same contract as tools/protomodels.py: on_cycle(m) is called before each
Machine.step() with the machine in its state for the coming cycle; the model
reads m.pad() and m.cycle and sets the external level of the two pads it
watches in m.ext_uio. It knows IEEE 802.3 (clause 3 frames, clause 7
Manchester coding, clause 14 10BASE-T signalling) and nothing about Keyer:
no instruction, no engine, no firmware symbol. Its parameters are the two
pin numbers and the core clock period in ns; time is measured in ns from
cycle numbers, and the bit clock is recovered from the line's transitions.

The line. TD+ and TD- are two uio pads. The model reads the pair as a
differential signal: (TD+, TD-) = (1, 0) positive, (0, 1) negative, (0, 0)
zero differential (silence), (1, 1) both driven high, which no 10BASE-T
driver produces (reported). An undriven pair reads zero: the model's
termination sets both external levels to 0.

What it checks (the `errors` list, tuples (kind, time_ns, detail); nothing
here asserts). Limits, where they come from, and how sure:
  - "illegal state": both pads high, at any time.
  - Silence is zero differential. An excursion from silence that is one
    single level and returns to zero is a link test pulse; anything with a
    transition inside is a frame.
  - "link pulse": a pulse that is negative, or whose width is outside
    lp_width_ns = (60, 200) ns. 802.3 14.3.1.3.2 (receiver acceptance, the
    UNH MAU suite test 14.2.4) accepts pulses of 0.6 to 2.0 bit times; the
    transmit template (14.3.1.2.1, Figure 14-12) is a nominal 100 ns
    pulse. Sure of the acceptance range; the transmit template's exact
    corners are not reproduced.
  - "link pulse early" / "link pulse late": 802.3 14.2.1.1 sends a pulse
    every 16 +- 8 ms of silence, counted from the previous pulse or from
    the start of TP_IDL of the last frame (the UNH suite test 14.1.1
    measures both). Window lp_interval_ms = (8, 24), multiplied by
    lp_scale so that a test can shorten the firmware's interval and say by
    how much. "late" is also reported, once, while silence runs past the
    upper limit. Sure.
  - "pulse near frame": a frame starting less than pulse_gap_ns (9600)
    after the end of a link pulse. The standard sets no such limit (the
    pulse belongs to the idle, the frame may follow it); 96 bit times is
    this model's conservative choice so that no receiver can see the pulse
    as part of the preamble. A pulse after a frame is held to the 8 ms
    lower limit above.
  - Manchester (802.3 7.3.1.1, used by 14.3.1.2): the first half of a bit
    cell is the complement of the bit, the second half the bit, so a 1 is
    a negative-to-positive transition in mid-cell. Clock recovery: a
    digital PLL locked on mid-cell transitions (accepted 0.75 to 1.25 bit
    periods after the previous one; a transition 0.25 to 0.75 periods after
    is a cell boundary; anything else is "manchester": lost lock); the
    period estimate adapts by 1/8 of each error.
  - "half-bit": every level inside a frame lasts one or two half-bits of
    50 ns, within half_tol_ns (10 ns). 802.3 14.3.1.2.3 allows zero
    crossings at 8.0 and 8.5 bit times +-20 ns after a reference crossing
    without the twisted-pair model (+-11 ns with it, UNH 14.1.10/11); 10 ns
    per level is tighter than both and loose enough for a real driver.
  - "rate": the mean bit period over the frame within rate_ppm (100 ppm,
    10 Mb/s +- 0.01 %, 802.3 7.3.2 / 14.3.1.2) of 100 ns.
  - "bits": the frame is not a whole number of bytes.
  - "preamble": the first 64 bits are not seven 0x55 and the SFD 0xD5
    (802.3 3.2.1, 3.2.2; a transmitter sends all 56 preamble bits).
  - "length": destination to FCS outside 64..1518 bytes (802.3 3.2.7,
    4.4.2; frame_len = (64, 1518), no VLAN tag).
  - "fcs": the CRC-32 (802.3 3.2.9; computed here bit by bit, the tests
    cross-check it with binascii.crc32) does not match.
  - "start of idle": TP_IDL (802.3 14.3.1.2.1, Figure 14-10) must start
    with the last transition to positive and stay positive for at least
    250 ns; the model also requires it to return to zero within 450 ns of
    that transition (soi_ns = (250, 450)). The 250 ns minimum is the
    template's; sure. The 450 ns maximum (4.5 bit times) is from secondary
    sources describing the start of idle as a 2.5 to 4.5 bit-time pulse;
    the template itself lets the voltage decay toward zero over a longer
    time, so this is stricter than the standard: fairly sure it is not
    looser. A frame whose last level is negative, or positive for less than
    250 ns, has no start of idle.
  - "gap": less than ifg_ns (9600 ns, 96 bit times, 802.3 4.4.2) from the
    end of the last bit cell of a frame to the start of the next frame.
  - "stuck": a level held for more than stuck_ns (10 us) inside activity.

Records: `pulses` (start_ns, width_ns, level); `frames` (Frame objects with
dst, src, type, payload, fcs, fcs_ok, bytes, soi_ns, tail_ns, errors and
timing). sample(cycle, p, n) is the core; on_cycle(m) calls it, and the
tests call it directly with synthetic waveforms (wave() below makes them).
"""

import binascii

POS, NEG, ZERO, BOTH = 1, -1, 0, 2

PREAMBLE = [0x55] * 7 + [0xD5]
HALF_NS = 50.0
BIT_NS = 100.0


def crc32(data):
    """IEEE 802.3 CRC-32 (3.2.9), reflected, bit by bit: the FCS as an
    integer; it is sent least significant byte first."""
    r = 0xFFFFFFFF
    for byte in data:
        for i in range(8):
            b = (byte >> i) & 1
            r = (r >> 1) ^ 0xEDB88320 if (r ^ b) & 1 else r >> 1
    return r ^ 0xFFFFFFFF


def frame_bytes(dst, src, etype, payload, fcs=None):
    """Destination to FCS; the payload is not padded here."""
    body = list(dst) + list(src) + [etype >> 8, etype & 0xFF] + list(payload)
    c = crc32(body) if fcs is None else fcs
    return body + list(c.to_bytes(4, "little"))


def wave(frame, half=2, soi=6, pre=PREAMBLE, lead=0, trail=0, half_override=None):
    """Synthetic pair levels, one (P, N) per cycle, for a frame (the bytes
    after the SFD): `lead` cycles of silence, preamble and SFD `pre`, the
    bits LSB first in Manchester at `half` cycles per half-bit, then the
    start of idle held positive for `soi` half-bits (0: none), then
    `trail` cycles of silence. half_override = {half-bit index: cycles}
    changes the length of single half-bits (fault injection)."""
    lv = {POS: (1, 0), NEG: (0, 1), ZERO: (0, 0)}
    out = [lv[ZERO]] * lead
    halves = []
    for byte in list(pre) + list(frame):
        for i in range(8):
            b = (byte >> i) & 1
            halves += [NEG, POS] if b else [POS, NEG]
    for k, h in enumerate(halves):
        n = (half_override or {}).get(k, half)
        out += [lv[h]] * n
    out += [lv[POS]] * (soi * half)
    out += [lv[ZERO]] * trail
    return out


class Frame:
    def __init__(self, start):
        self.start = start          # ns: first transition out of silence
        self.end = None             # ns: return to zero
        self.bits_end = None        # ns: end of the last bit cell
        self.soi_ns = None          # last positive level: from its rising edge to zero
        self.tail_ns = None         # positive time after the last bit cell
        self.bits = []
        self.bytes = []             # after the SFD: destination .. FCS
        self.dst = self.src = self.type = None
        self.payload = []
        self.fcs = None
        self.fcs_ok = False
        self.bit_ns = None          # mean bit period over the frame
        self.errors = []

    @property
    def ok(self):
        return not self.errors


class Eth10BTReceiver:
    """tx_p, tx_n     uio pin numbers of TD+ and TD-
    clk_ns          core clock period (25.0 at 40 MHz)
    lp_scale        factor on the link pulse interval window (1.0: 8..24 ms)
    other           external level of the uio pads the model does not watch"""

    def __init__(self, tx_p=0, tx_n=1, clk_ns=25.0, lp_scale=1.0, lp_interval_ms=(8.0, 24.0),
                 lp_width_ns=(60.0, 200.0), half_tol_ns=10.0, rate_ppm=100.0,
                 soi_ns=(250.0, 450.0), ifg_ns=9600.0, pulse_gap_ns=9600.0,
                 frame_len=(64, 1518), stuck_ns=10000.0, other=0xFF):
        self.p, self.n, self.clk_ns = tx_p, tx_n, float(clk_ns)
        self.lp_min = lp_interval_ms[0] * 1e6 * lp_scale
        self.lp_max = lp_interval_ms[1] * 1e6 * lp_scale
        self.lp_width = lp_width_ns
        self.tol = half_tol_ns
        self.rate_ppm = rate_ppm
        self.soi_min, self.soi_max = soi_ns
        self.ifg = ifg_ns
        self.pulse_gap = pulse_gap_ns
        self.frame_len = frame_len
        self.stuck = stuck_ns
        self.other = other & 0xFF
        self.errors = []
        self.pulses = []            # (start_ns, width_ns, level)
        self.frames = []
        self.cycle = None
        self._state = ZERO
        self._run_start = 0.0
        self._runs = None           # runs of the activity in progress: [level, start, duration]
        self._ref = None            # start of the last pulse or of the last TP_IDL
        self._pulse_end = None      # end of the last pulse
        self._bits_end = None       # end of the last bit cell of the last frame
        self._late_flagged = False
        self._stuck_flagged = False
        self._both_flagged = False

    # ------------------------------------------------------------ the clock

    def on_cycle(self, m):
        mask = (1 << self.p) | (1 << self.n)
        m.ext_uio = self.other & ~mask & 0xFF          # termination: undriven reads zero
        pad = m.pad()
        self.sample(m.cycle, (pad >> self.p) & 1, (pad >> self.n) & 1)

    def _err(self, kind, t, detail=None, frame=None):
        self.errors.append((kind, t, detail))
        if frame is not None and kind not in frame.errors:
            frame.errors.append(kind)

    def sample(self, cycle, p, n):
        """The pair's pad levels during `cycle`."""
        self.cycle = cycle
        t = cycle * self.clk_ns
        s = {(1, 0): POS, (0, 1): NEG, (0, 0): ZERO, (1, 1): BOTH}[(p & 1, n & 1)]
        if s == BOTH:
            if not self._both_flagged:
                self._err("illegal state", t, "both pads high")
            self._both_flagged = True
            s = ZERO                                    # treated as no differential
        else:
            self._both_flagged = False
        if s != self._state:
            self._change(s, t)
        elif s != ZERO and t - self._run_start > self.stuck and not self._stuck_flagged:
            self._err("stuck", t, "level %+d for over %.0f ns" % (self._state, self.stuck))
            self._stuck_flagged = True
        if s == ZERO and self._runs is None and self._ref is not None and not self._late_flagged:
            if t - self._ref > self.lp_max:
                self._err("link pulse late", t, "no link pulse or frame %.3f ms after the last"
                          % ((t - self._ref) / 1e6))
                self._late_flagged = True

    def _change(self, s, t):
        old, start = self._state, self._run_start
        if old != ZERO:
            self._runs.append([old, start, t - start])
        if s == ZERO:
            runs, self._runs = self._runs, None
            self._activity(runs, t)
        elif old == ZERO:
            self._runs = []
        self._state, self._run_start = s, t
        self._stuck_flagged = False

    def finish(self):
        """Close an activity still in progress (end of the simulation)."""
        if self._state != ZERO and self.cycle is not None:
            self._change(ZERO, (self.cycle + 1) * self.clk_ns)

    # ------------------------------------------------------------ analysis

    def _activity(self, runs, end):
        if len(runs) == 1:
            self._pulse(runs[0])
        else:
            self._frame(runs, end)

    def _pulse(self, run):
        level, start, width = run
        self.pulses.append((start, width, level))
        if level != POS:
            self._err("link pulse", start, "negative pulse")
        if not self.lp_width[0] <= width <= self.lp_width[1]:
            self._err("link pulse", start, "width %.1f ns" % width)
        self._interval(start, "link pulse")
        self._ref = start
        self._pulse_end = start + width
        self._late_flagged = False

    def _interval(self, start, what):
        if self._ref is None:
            return
        d = start - self._ref
        if d < self.lp_min:
            self._err("link pulse early", start, "%s %.3f ms after the last" % (what, d / 1e6))
        elif d > self.lp_max and not self._late_flagged:
            self._err("link pulse late", start, "%s %.3f ms after the last" % (what, d / 1e6))

    def _frame(self, runs, end):
        f = Frame(runs[0][1])
        f.end = end
        self.frames.append(f)
        if self._pulse_end is not None and f.start - self._pulse_end < self.pulse_gap:
            self._err("pulse near frame", f.start, "%.0f ns after a link pulse"
                      % (f.start - self._pulse_end), f)
        if self._bits_end is not None and f.start - self._bits_end < self.ifg:
            self._err("gap", f.start, "%.0f ns after the last frame" % (f.start - self._bits_end), f)
        if self._ref is not None and f.start - self._ref > self.lp_max and not self._late_flagged:
            self._err("link pulse late", f.start, "frame %.3f ms after the last activity"
                      % ((f.start - self._ref) / 1e6))
        # every level but the last (which holds the start of idle) is 1 or 2 half-bits
        for level, start, dur in runs[:-1]:
            k = int(round(dur / HALF_NS))
            if k not in (1, 2) or abs(dur - k * HALF_NS) > self.tol:
                self._err("half-bit", start, "level %+d for %.1f ns" % (level, dur), f)
        # clock recovery on the transitions: mid-cell transitions carry the bits
        bit = BIT_NS
        t_mid = f.start - bit / 2                       # the first transition is a cell boundary
        mids = []
        for level, start, dur in runs[1:]:
            dt = start - t_mid
            if 0.75 * bit <= dt <= 1.25 * bit:
                f.bits.append(1 if level == POS else 0)
                if mids:
                    bit += (dt - bit) / 8.0
                mids.append(start)
                t_mid = start
            elif 0.25 * bit <= dt < 0.75 * bit:
                pass                                    # cell boundary
            else:
                self._err("manchester", start, "transition %.1f ns after the last mid-cell one" % dt, f)
                break
        last = runs[-1]
        f.bits_end = t_mid + BIT_NS / 2 if mids else f.start
        self._bits_end = f.bits_end
        # start of idle
        if last[0] != POS:
            self._err("start of idle", last[1], "the frame ends negative", f)
        else:
            f.soi_ns = end - last[1]
            f.tail_ns = end - f.bits_end
            if not self.soi_min <= f.soi_ns <= self.soi_max:
                self._err("start of idle", last[1], "positive for %.1f ns" % f.soi_ns, f)
        tp_idl = f.bits_end
        if len(mids) > 1:
            f.bit_ns = (mids[-1] - mids[0]) / (len(mids) - 1)
            if abs(f.bit_ns / BIT_NS - 1) * 1e6 > self.rate_ppm:
                self._err("rate", f.start, "bit period %.3f ns" % f.bit_ns, f)
        # bytes
        bits = f.bits
        if len(bits) % 8:
            self._err("bits", f.start, "%d bits" % len(bits), f)
        allb = [sum(bits[i + k] << k for k in range(8)) for i in range(0, len(bits) - 7, 8)]
        if allb[:8] != PREAMBLE:
            self._err("preamble", f.start, " ".join("%02X" % b for b in allb[:8]), f)
        else:
            body = allb[8:]
            f.bytes = body
            if not self.frame_len[0] <= len(body) <= self.frame_len[1]:
                self._err("length", f.start, "%d bytes" % len(body), f)
            if len(body) >= 18:
                f.dst, f.src = body[0:6], body[6:12]
                f.type = body[12] << 8 | body[13]
                f.payload = body[14:-4]
                f.fcs = int.from_bytes(bytes(body[-4:]), "little")
                f.fcs_ok = crc32(body[:-4]) == f.fcs
                if not f.fcs_ok:
                    self._err("fcs", f.start, "FCS %08X, computed %08X" % (f.fcs, crc32(body[:-4])), f)
            else:
                self._err("length", f.start, "%d bytes" % len(body), f)
        # the link pulse timer restarts at the start of TP_IDL
        self._ref = tp_idl
        self._late_flagged = False


def binascii_crc32(data):
    """The library CRC-32, for the cross-check in the tests."""
    return binascii.crc32(bytes(data)) & 0xFFFFFFFF
