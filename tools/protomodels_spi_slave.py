"""SPI master model, mode 0, for exercising an SPI slave on the ISS and the RTL.

Same contract as tools/protomodels.py: on_cycle(m) is called before each
Machine.step() with the machine in its state for the coming cycle; the model
reads m.pad() (24-bit pad vector: bits 0-7 uio, 8-15 ui, 16-23 uo), drives
m.ext_ui / m.ext_uio and reads m.cycle. It knows only the protocol: SPI mode 0
(CPOL = 0, CPHA = 0), MSB first, one slave select. Nothing in here comes from
the firmware; the only parameters are pin numbers and timings in core cycles.

Mode 0 as the master sees it:
  - SCK idles low. CS_n falls and the master puts the first MOSI bit on the
    wire; `cs_setup` cycles later SCK rises for the first time.
  - Both sides sample on the rising edge. The master changes MOSI on the
    falling edge; the slave changes MISO after the falling edge and must have
    it stable `setup` cycles before the next rising edge and keep it for
    `hold` cycles after it.
  - After the last falling edge SCK stays low; CS_n rises `cs_hold` cycles
    later and stays high for at least `cs_idle` cycles.
  - MISO is a shared line: the slave may drive it only while CS_n is low. It
    gets `t_dis` cycles after CS_n rises to let go.

How a driven pin is told from a released one through the pad alone: the model
is the only other party on MISO, and it pulls the line weakly to the opposite
of the level it saw in the previous cycle. A released pad follows the pull
(so it toggles every cycle); a pad that differs from the pull is driven. A
released MISO therefore also fails the setup check, as it should: the master
would be sampling a floating line.
"""


def _bit(v, p):
    return (v >> p) & 1


def _bytes(bits):
    out = []
    for i in range(0, len(bits) - len(bits) % 8, 8):
        v = 0
        for b in bits[i:i + 8]:
            v = (v << 1) | b
        out.append(v)
    return out


class SpiFrame:
    """What the master did and saw in one frame."""

    def __init__(self, data, nbits, selected):
        self.data = list(data)      # bytes the master was asked to send
        self.nbits = nbits          # SCK pulses to generate
        self.selected = selected    # False: traffic for another slave, CS_n stays high
        self.start = None           # first cycle of the frame (CS_n low on the pad if selected)
        self.end = None             # first cycle after the frame (CS_n high again, SCK low)
        self.cs_fall = None         # = start, for a selected frame
        self.cs_rise = None         # cycle in which CS_n is high again (selected frames)
        self.rising = []            # cycle of every SCK rising edge
        self.falling = []           # cycle of every SCK falling edge
        self.mosi_bits = []         # bit on MOSI at each rising edge
        self.miso_bits = []         # bit sampled from MISO at each rising edge (selected frames)
        self.miso_changes = []      # cycles in which MISO changed, from the first rising edge to CS_n rising
        self.miso_on = None         # first cycle after CS_n fell in which MISO was seen driven
        self.miso_off = None        # first cycle after CS_n rose from which MISO was not seen driven

    @property
    def sent(self):
        """Complete bytes clocked out on MOSI."""
        return _bytes(self.mosi_bits)

    @property
    def received(self):
        """Complete bytes sampled from MISO."""
        return _bytes(self.miso_bits)

    @property
    def partial(self):
        """Bits clocked after the last complete byte (0 unless the frame was aborted mid-byte)."""
        return len(self.mosi_bits) % 8

    @property
    def done(self):
        return self.end is not None


class SpiMasterModel:
    """SPI mode 0 master on three ui pins (SCK, MOSI, CS_n) sampling MISO on a pad.

    half      SCK half period
    cs_setup  CS_n falling to the first SCK rising edge
    cs_hold   last SCK falling edge to CS_n rising
    cs_idle   minimum CS_n high time between frames
    setup     MISO must not change in the last `setup` cycles up to and
              including the cycle of a rising edge (it is sampled in that cycle)
    hold      nor in the `hold` cycles after it
    t_dis     cycles after CS_n rises in which MISO may still be driven
    (all in core clock cycles; defaults: half a bit for the CS times, one bit
    for cs_idle and t_dis, one cycle for setup and hold)

    send() queues a frame; frames are played one after another and recorded
    in `frames` (SpiFrame). Protocol violations by the slave go to `errors`:
      ("miso setup", cycle of the rising edge, cycle of the change)
      ("miso hold", cycle of the rising edge, cycle of the change)
      ("miso driven while cs high", cycle)     reported once per CS_n high period
    """

    def __init__(self, sck_ui_bit, mosi_ui_bit, csn_ui_bit, miso_pin, half,
                 cs_setup=None, cs_hold=None, cs_idle=None, setup=1, hold=1, t_dis=None):
        assert half >= 1 and setup >= 0 and hold >= 0
        self.sck, self.mosi, self.csn, self.miso = sck_ui_bit, mosi_ui_bit, csn_ui_bit, miso_pin
        self.half = half
        self.cs_setup = half if cs_setup is None else cs_setup
        self.cs_hold = half if cs_hold is None else cs_hold
        self.cs_idle = 2 * half if cs_idle is None else cs_idle
        self.setup, self.hold = setup, hold
        self.t_dis = 2 * half if t_dis is None else t_dis
        assert self.cs_setup >= 1 and self.cs_hold >= 1 and self.cs_idle >= 1 and self.t_dis >= 0
        self.frames = []            # frames played or being played, in order
        self.errors = []
        self._queue = []            # (SpiFrame, gap, align, cs_high_abort)
        self._cur = None            # frame being played
        self._wave = []             # (sck, mosi, csn) for each cycle of it
        self._t = 0
        self._out = (0, 0, 1)       # what was on the wires in the previous cycle
        self._idle_since = 0        # cycle from which the bus has been idle
        self._cs_rose = None        # cycle in which CS_n last rose (None before the first frame)
        self._last_sel = None       # last selected frame
        self._prev_miso = 1         # MISO pad in the previous cycle
        self._last_change = None    # cycle of the last MISO change
        self._last_rise = None      # last SCK rising edge of the running selected frame
        self._flagged = False       # "driven while cs high" already reported in this high period

    # ------------------------------------------------------------ stimulus

    def send(self, data, bits=None, cs_high_abort=False, select=True, gap=0, align=None):
        """Queue a frame and return its SpiFrame (filled in as it is played).

        data           bytes for MOSI, MSB first
        bits           number of SCK pulses (default 8 per byte); fewer aborts
                       the frame, mid-byte if it is not a multiple of 8
        cs_high_abort  a broken master: CS_n rises in the middle of the high
                       phase of the last pulse instead of after its falling
                       edge (not mode 0; for robustness tests)
        select         False: the same waveform with CS_n left high (traffic
                       for another slave on the bus)
        gap            extra idle cycles before the frame, on top of cs_idle
        align          (r, n): the frame starts in a cycle c with c % n == r
        """
        data = list(data)
        nbits = 8 * len(data) if bits is None else bits
        assert 0 <= nbits <= 8 * len(data)
        assert not (cs_high_abort and (nbits == 0 or not select))
        fr = SpiFrame(data, nbits, select)
        self._queue.append((fr, gap, align, cs_high_abort))
        return fr

    @property
    def busy(self):
        """True while a frame is queued or running."""
        return bool(self._queue) or self._cur is not None

    def _build(self, fr, cs_high_abort):
        H = self.half
        cs = 0 if fr.selected else 1
        bits = [(byte >> (7 - i)) & 1 for byte in fr.data for i in range(8)][:fr.nbits]
        wave = [(0, bits[0] if bits else 0, cs)] * self.cs_setup
        for k, b in enumerate(bits):
            last = k == len(bits) - 1
            if last and cs_high_abort:
                a = max(1, H // 2)
                return wave + [(1, b, cs)] * a + [(1, b, 1)] * max(1, H - a)
            wave += [(1, b, cs)] * H
            if last:
                wave += [(0, b, cs)] * self.cs_hold
            else:
                wave += [(0, bits[k + 1], cs)] * H      # MOSI changes on the falling edge
        if not bits:
            wave += [(0, 0, cs)] * self.cs_hold
        return wave

    # ------------------------------------------------------------ the clock

    def on_cycle(self, m):
        c = m.cycle
        # 1. what the master puts on the wires in this cycle
        if self._cur is None and self._queue:
            fr, gap, align, cha = self._queue[0]
            if c - self._idle_since >= self.cs_idle + gap and \
                    (align is None or c % align[1] == align[0]):
                self._queue.pop(0)
                self._cur, self._wave, self._t = fr, self._build(fr, cha), 0
                self.frames.append(fr)
                fr.start = c
                if fr.selected:
                    fr.cs_fall = c
        fr = self._cur
        ending = False
        if fr is not None and self._t < len(self._wave):
            out = self._wave[self._t]
            self._t += 1
        else:
            out = (0, 0, 1)
            ending = fr is not None
        sck, mosi, csn = out
        p_sck, _, p_csn = self._out
        pull = 1 - self._prev_miso
        m.ext_ui = ((m.ext_ui & ~((1 << self.sck) | (1 << self.mosi) | (1 << self.csn)))
                    | (sck << self.sck) | (mosi << self.mosi) | (csn << self.csn))
        m.ext_uio = (m.ext_uio & ~(1 << self.miso) & 0xFF) | (pull << self.miso)

        # 2. what is on MISO in this cycle
        miso = _bit(m.pad(), self.miso)
        driven = miso != pull                   # a released pad follows the pull
        changed = miso != self._prev_miso
        self._prev_miso = miso
        if changed:
            if self._last_rise is not None and 0 < c - self._last_rise <= self.hold:
                self.errors.append(("miso hold", self._last_rise, c))
            self._last_change = c

        # 3. the running frame
        if fr is not None:
            sel = fr.selected
            if sel and csn == 0:
                if driven and fr.miso_on is None:
                    fr.miso_on = c
                if changed and fr.rising and c > fr.rising[0]:
                    fr.miso_changes.append(c)
            if p_sck == 0 and sck == 1:
                fr.rising.append(c)
                fr.mosi_bits.append(mosi)
                if sel:
                    fr.miso_bits.append(miso)   # sampled on the rising edge
                    self._last_rise = c
                    if self._last_change is not None and c - self._last_change < self.setup:
                        self.errors.append(("miso setup", c, self._last_change))
            elif p_sck == 1 and sck == 0:
                fr.falling.append(c)
            if sel and p_csn == 0 and csn == 1:
                fr.cs_rise = fr.miso_off = c
                self._cs_rose = c
                self._last_sel = fr
                self._last_rise = None
                self._flagged = False
            if ending:
                fr.end = c
                self._idle_since = c
                self._cur = None

        # 4. MISO belongs to someone else while CS_n is high
        if csn == 1 and driven:
            if self._last_sel is not None:
                self._last_sel.miso_off = c + 1
            if (self._cs_rose is None or c - self._cs_rose >= self.t_dis) and not self._flagged:
                self.errors.append(("miso driven while cs high", c))
                self._flagged = True
        self._out = out
