"""I2C master model for exercising an I2C slave on the ISS (and, in lockstep,
on the RTL).

Like the models in protomodels.py it has on_cycle(m), called before each
Machine.step(): it reads m.pad(), drives m.ext_uio and stamps what it sees
with m.cycle. It knows only the bus rules of the I2C specification (NXP
UM10204): START, repeated START, STOP, nine clock pulses per byte, data
changing only while SCL is low, the receiver's ACK, clock stretching. It
knows nothing about the device it talks to. Its parameters are the two pin
numbers and its timing in core cycles: the quarter period, the data hold
time and the longest clock stretch it tolerates.

Both lines are open-drain with pull-ups: the model only ever pulls a line
low or lets it go, and reads the level back from the pad.

Timing, with Q = quarter cycles and H = data_hold cycles (default Q; a real
master may change SDA as soon as SCL is low, H = 1 here). Every interval
below is exact on the pads unless the slave stretches the clock.

  bit        SCL falls; H later SDA takes the bit's value (or is released for
             a bit the slave sends); 2 Q after the fall SCL is released; the
             model waits until the pad really is high (clock stretching); Q
             after that it samples SDA; Q later SCL falls again. SCL low =
             SCL high = 2 Q.
  START      SDA falls while SCL is high; SCL falls 2 Q later.
  repeated   from SCL low: H later SDA is released, 2 Q after the fall SCL
  START      is released; 2 Q after SCL is high, SDA falls; SCL falls 2 Q
             later.
  STOP       from SCL low: H later SDA is pulled low, 2 Q after the fall SCL
             is released; 2 Q after SCL is high, SDA is released; the bus is
             then left free for 2 Q.

Script. queue(ops) appends operations; they run in order, one clock cycle of
the model per on_cycle call. `idle` is true when everything queued has run.
The script is played as written: a NACK is recorded, it does not end the
transfer.

  ("start",)               START, or a repeated START if SCL is low
  ("stop",)                STOP
  ("write", byte)          eight bits and the ACK bit. Optional third element:
  ("write", byte, expect)  True (default) = an ACK is required, False = a
                           NACK is required, None = either
  ("read", ack)            eight bits from the slave, then ACK (ack true) or
                           NACK from the master
  ("wbits", value, n)      the n most significant bits of an 8-bit value and
                           no ACK bit: a byte cut short
  ("rbits", n)             n clock pulses with SDA released; the levels go to
                           `bits`
  ("wait", cycles)         do nothing, lines as they are (SCL stays low in
                           the middle of a transfer, the bus stays free
                           after a STOP)
  ("high", cycles)         the next clock pulse stays high this much longer

Records
  events     ("start" | "stop", cycle) for every START and STOP on the bus
  acks       (byte, acked) for every "write"
  read       the bytes of every "read"
  bits       (level, cycle) for every "rbits" pulse
  stretches  (cycle SCL was released, cycles the pad stayed low after that)
  setup      (cycle of an SCL rising edge, cycles since SDA last changed)
  hold       (cycle of an SDA change while SCL is low, cycles since SCL
             fell, who) with who = "m" for the model's own change, "s" for
             the slave's
  scl_low,   lengths of every completed SCL low and high phase on the pad,
  scl_high   as (cycle the phase ended, cycles)
  errors     protocol violations, each a tuple (text, cycle, ...):
    "sda changed while scl high"   an SDA edge during SCL high that is not
                                   this master's START or STOP
    "sda changed at scl edge"      SDA and SCL changed in the same cycle
    "scl pulled low while high"    SCL fell while the master was not pulling
                                   it: a stretch must begin while SCL is low
    "missing ack" / "unexpected ack"   against the script's expectation
    "stretch limit"                SCL stayed low longer than stretch_limit
                                   after the master released it
    "arbitration lost"             the master sent a 1 and read back a 0
    "sda low before start"         SDA was not free when a START was due
    "sda low after stop"           SDA did not rise at the STOP
"""


def _bit(v, p):
    return (v >> p) & 1


class I2cMasterModel:
    """A scripted I2C master on two uio pins. See the module docstring."""

    def __init__(self, scl, sda, quarter, data_hold=None, stretch_limit=None):
        assert quarter >= 1
        self.scl_pin, self.sda_pin, self.q = scl, sda, quarter
        self.h = quarter if data_hold is None else data_hold
        assert 1 <= self.h < 2 * quarter
        self.stretch_limit = 100 * quarter if stretch_limit is None else stretch_limit
        self.events = []
        self.acks = []
        self.read = []
        self.bits = []
        self.stretches = []
        self.setup = []
        self.hold = []
        self.scl_low = []
        self.scl_high = []
        self.errors = []
        self.cycle = 0
        self.scl = self.sda = 1          # pad levels of the previous cycle
        self._scl_low = False            # this master is pulling SCL low
        self._sda_low = False            # this master is pulling SDA low
        self._sda_low_prev = False
        self._own_edge = False           # the SDA change of this cycle is our START/STOP
        self._extra_high = 0
        self._last_sda = None            # cycle of the last SDA change
        self._last_rise = None
        self._last_fall = None
        self._queue = []
        self._busy = False
        self._gen = self._run()

    # ------------------------------------------------------------ script

    def queue(self, ops):
        self._queue.extend(tuple(op) for op in ops)

    @property
    def idle(self):
        return not self._queue and not self._busy

    def _run(self):
        while True:
            if not self._queue:
                self._busy = False
                yield
                continue
            self._busy = True
            op = self._queue.pop(0)
            kind = op[0]
            if kind == "start":
                yield from self._start()
            elif kind == "stop":
                yield from self._stop()
            elif kind == "write":
                yield from self._write(op[1], op[2] if len(op) > 2 else True)
            elif kind == "read":
                yield from self._read(op[1])
            elif kind == "wbits":
                for i in range(op[2]):
                    yield from self._clock((op[1] >> (7 - i)) & 1)
            elif kind == "rbits":
                for _ in range(op[1]):
                    level = yield from self._clock(1, check=False)
                    self.bits.append((level, self.cycle))
            elif kind == "wait":
                yield from self._wait(op[1])
            elif kind == "high":
                self._extra_high = op[1]
            else:
                raise ValueError("unknown script operation %r" % (op,))

    def _wait(self, n):
        for _ in range(n):
            yield

    def _release_scl(self):
        """Let SCL go and wait until the pad is high. Returns in the cycle
        after the first one in which it was."""
        self._scl_low = False
        released = self.cycle
        yield
        n = 0
        while not self.scl:
            n += 1
            if n > self.stretch_limit:
                self.errors.append(("stretch limit", self.cycle))
                break
            yield
        self.stretches.append((released, n))

    def _clock(self, bit, check=True):
        """One clock pulse from SCL low to SCL low. bit = 0 pulls SDA low,
        1 releases it. Returns the level sampled in the middle of SCL high."""
        q = self.q
        yield from self._wait(self.h)
        self._sda_low = not bit
        yield from self._wait(2 * q - self.h)
        yield from self._release_scl()
        yield from self._wait(q)
        level = self.sda
        if check and bit and not level:
            self.errors.append(("arbitration lost", self.cycle))
        yield from self._wait(q - 1 + self._extra_high)
        self._extra_high = 0
        self._scl_low = True
        return level

    def _start(self):
        q = self.q
        if self._scl_low:                       # repeated START
            yield from self._wait(self.h)
            self._sda_low = False
            yield from self._wait(2 * q - self.h)
            yield from self._release_scl()
            yield from self._wait(2 * q - 1)
        if not self.sda:
            self.errors.append(("sda low before start", self.cycle))
        self._sda_low = True
        self._own_edge = True
        yield from self._wait(2 * q)
        self._scl_low = True

    def _stop(self):
        q = self.q
        yield from self._wait(self.h)
        self._sda_low = True
        yield from self._wait(2 * q - self.h)
        yield from self._release_scl()
        yield from self._wait(2 * q - 1)
        self._sda_low = False
        self._own_edge = True
        yield
        if not self.sda:
            self.errors.append(("sda low after stop", self.cycle))
        yield from self._wait(2 * q - 1)

    def _write(self, byte, expect):
        for i in range(7, -1, -1):
            yield from self._clock((byte >> i) & 1)
        acked = (yield from self._clock(1, check=False)) == 0
        self.acks.append((byte, acked))
        if expect is True and not acked:
            self.errors.append(("missing ack", self.cycle, byte))
        elif expect is False and acked:
            self.errors.append(("unexpected ack", self.cycle, byte))

    def _read(self, ack):
        value = 0
        for _ in range(8):
            value = (value << 1) | (yield from self._clock(1, check=False))
        yield from self._clock(0 if ack else 1, check=False)
        self.read.append(value)

    # ------------------------------------------------------------ the clock

    def on_cycle(self, m):
        c = self.cycle = m.cycle
        next(self._gen)                         # the script's move for this cycle
        ext = m.ext_uio | (1 << self.scl_pin) | (1 << self.sda_pin)
        if self._scl_low:
            ext &= ~(1 << self.scl_pin)
        if self._sda_low:
            ext &= ~(1 << self.sda_pin)
        m.ext_uio = ext & 0xFF
        pad = m.pad()                           # the bus during this cycle
        scl, sda = _bit(pad, self.scl_pin), _bit(pad, self.sda_pin)
        if sda != self.sda:
            if scl != self.scl:
                self.errors.append(("sda changed at scl edge", c))
            elif scl:
                if self._own_edge:
                    self.events.append(("stop" if sda else "start", c))
                else:
                    self.errors.append(("sda changed while scl high", c))
            elif self._last_fall is not None:
                who = "m" if self._sda_low != self._sda_low_prev else "s"
                self.hold.append((c, c - self._last_fall, who))
            self._last_sda = c
        if scl != self.scl:
            if scl:
                if self._last_sda is not None:
                    self.setup.append((c, c - self._last_sda))
                if self._last_fall is not None:
                    self.scl_low.append((c, c - self._last_fall))
                self._last_rise = c
            else:
                if not self._scl_low:
                    self.errors.append(("scl pulled low while high", c))
                if self._last_rise is not None:
                    self.scl_high.append((c, c - self._last_rise))
                self._last_fall = c
        self._own_edge = False
        self._sda_low_prev = self._sda_low
        self.scl, self.sda = scl, sda
