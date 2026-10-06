"""Protocol models for exercising firmware on the ISS (and, later, the RTL).

Every model has on_cycle(m) which is called before each Machine.step() with
the machine in its state for the coming cycle. Models read m.pad() (the 24-bit
pad vector for this cycle) and drive external levels through m.ext_ui /
m.ext_uio. They are deliberately independent of the firmware: a UART decoder
only knows the baud rate, an SPI slave only knows mode 0, an I2C slave only
knows the bus rules.
"""


def _bit(v, p):
    return (v >> p) & 1


class HostFeeder:
    """Pushes bytes into a thread's inbox as space becomes available, the way
    a real host driver must (the inbox is 16 deep and drops writes when full)."""

    def __init__(self, tid, data):
        self.tid, self.pending = tid, list(data)

    def on_cycle(self, m):
        while self.pending and m.host_inbox_push(self.tid, self.pending[0]):
            self.pending.pop(0)


class UartDecoder:
    """Decodes 8N1 on a pad pin, sampling mid-bit at the given period."""

    def __init__(self, pin, period):
        self.pin, self.period = pin, period
        self.bytes = []
        self.errors = []
        self._state = "idle"
        self._prev = 0           # start only on a real 1 -> 0 transition
        self._t = 0
        self._bits = []
        self.edges = []          # cycles at which the line changed
        self.starts = []         # cycle of each start-bit edge

    def on_cycle(self, m):
        lvl = _bit(m.pad(), self.pin)
        if lvl != self._prev:
            self.edges.append(m.cycle)
        if self._state == "idle":
            if self._prev == 1 and lvl == 0:
                self._state = "data"
                self._t = 0
                self._bits = []
                self.starts.append(m.cycle)
        elif self._state == "data":
            self._t += 1
            n = len(self._bits)
            if self._t == int(self.period * (1.5 + n)):
                self._bits.append(lvl)
                if n == 8:
                    data = sum(b << i for i, b in enumerate(self._bits[:8]))
                    if self._bits[8] != 1:
                        self.errors.append(("framing", m.cycle, data))
                    self.bytes.append(data)
                    self._state = "idle"
        self._prev = lvl

    def bit_lengths(self):
        return [b - a for a, b in zip(self.edges, self.edges[1:])]


class UartStimulus:
    """Drives 8N1 bytes onto an ext_ui bit at the given period (cycles/bit)."""

    def __init__(self, ui_bit, period, data, start=20, gap=3):
        self.ui_bit, self.period = ui_bit, period
        self.wave = [1] * start
        for b in data:
            bits = [0] + [(b >> i) & 1 for i in range(8)] + [1] * (1 + gap)
            for v in bits:
                self.wave += [v] * period
        self.wave += [1] * 50
        self.done_at = len(self.wave)

    def on_cycle(self, m):
        v = self.wave[m.cycle] if m.cycle < len(self.wave) else 1
        m.ext_ui = (m.ext_ui & ~(1 << self.ui_bit)) | (v << self.ui_bit)


class SpiSlaveModel:
    """Mode 0 slave on uo pins (SCK, MOSI, CS_n) answering on a ui pin (MISO).

    Records every received byte per CS frame and shifts out `reply` bytes.
    Checks that SCK is idle low when CS falls and that bits per frame are a
    multiple of 8.
    """

    def __init__(self, sck, mosi, csn, miso_ui_bit, reply):
        self.sck, self.mosi, self.csn, self.miso = sck, mosi, csn, miso_ui_bit
        self.reply = list(reply)
        self.frames = []
        self.errors = []
        self._prev_sck = 0
        self._prev_cs = 0        # outputs are low at reset; a frame starts on a real 1 -> 0
        self._shift = 0
        self._nbits = 0
        self._out = 0
        self._ri = 0
        self.sck_edges = []

    def _present(self, m):
        bit = (self._out >> 7) & 1
        m.ext_ui = (m.ext_ui & ~(1 << self.miso)) | (bit << self.miso)

    def on_cycle(self, m):
        pad = m.pad()
        sck, mosi, cs = _bit(pad, self.sck), _bit(pad, self.mosi), _bit(pad, self.csn)
        if self._prev_cs == 1 and cs == 0:
            if sck != 0:
                self.errors.append(("sck not idle at cs fall", m.cycle))
            self.frames.append([])
            self._nbits = 0
            self._out = self.reply[self._ri % len(self.reply)] if self.reply else 0
            self._present(m)
        if cs == 0:
            if self._prev_sck == 0 and sck == 1:          # rising: sample MOSI
                self.sck_edges.append(m.cycle)
                self._shift = ((self._shift << 1) | mosi) & 0xFF
                self._nbits += 1
                if self._nbits % 8 == 0:
                    self.frames[-1].append(self._shift)
                    self._ri += 1
            elif self._prev_sck == 1 and sck == 0:        # falling: next MISO bit
                self._out = (self._out << 1) & 0xFF
                if self._nbits % 8 == 0 and self.reply:
                    self._out = self.reply[self._ri % len(self.reply)]
                self._present(m)
        if self._prev_cs == 0 and cs == 1:
            if self._nbits % 8:
                self.errors.append(("partial byte", m.cycle, self._nbits))
        self._prev_sck, self._prev_cs = sck, cs


class I2cSlaveModel:
    """A 24Cxx-style EEPROM on uio pins (SCL, SDA), open-drain with pull-ups.

    Write: first byte after the address sets the pointer, following bytes are
    stored. Read: returns memory from the pointer. Optionally stretches the
    clock after each ACK for `stretch` cycles. Records START/STOP events and
    checks minimum high/low times in cycles.
    """

    def __init__(self, scl, sda, address=0x50, stretch=0, min_high=60, min_low=60):
        self.scl, self.sda, self.address = scl, sda, address
        self.stretch, self.min_high, self.min_low = stretch, min_high, min_low
        self.mem = [0xFF] * 256
        self.ptr = 0
        self.events = []
        self.errors = []
        self._prev_scl = self._prev_sda = 1
        self._state = "idle"      # idle, addr, write, read
        self._shift = 0
        self._nbits = 0
        self._drive_low = False   # we are pulling SDA low
        self._scl_low_hold = 0    # cycles left to hold SCL low (stretch)
        self._first_write_byte = False
        self._tx_byte = 0
        self._last_edge = 0
        self.transfers = []       # ("w", addr, [bytes]) / ("r", addr, [bytes])

    def _drive(self, m):
        ext = m.ext_uio | (1 << self.sda) | (1 << self.scl)
        if self._drive_low:
            ext &= ~(1 << self.sda)
        if self._scl_low_hold > 0:
            ext &= ~(1 << self.scl)
        m.ext_uio = ext & 0xFF

    def on_cycle(self, m):
        if self._scl_low_hold > 0:
            self._scl_low_hold -= 1
        pad = m.pad()
        scl, sda = _bit(pad, self.scl), _bit(pad, self.sda)
        # START / STOP: SDA changes while SCL high
        if scl == 1 and self._prev_scl == 1:
            if self._prev_sda == 1 and sda == 0:
                self.events.append(("start", m.cycle))
                self._state, self._nbits, self._shift = "addr", 0, 0
                self._drive_low = False
            elif self._prev_sda == 0 and sda == 1:
                self.events.append(("stop", m.cycle))
                self._state = "idle"
                self._drive_low = False
        if self._prev_scl == 0 and scl == 1:                 # rising edge: sample
            if m.cycle - self._last_edge < self.min_low and self._last_edge:
                self.errors.append(("scl low too short", m.cycle))
            self._last_edge = m.cycle
            if self._state in ("addr", "write"):
                if self._nbits < 8:
                    self._shift = ((self._shift << 1) | sda) & 0xFF
                self._nbits += 1
            elif self._state == "read":
                self._nbits += 1
                if self._nbits == 9:                          # master's ACK/NACK
                    if sda == 1:
                        self.events.append(("nack", m.cycle))
                        self._state = "idle"
        elif self._prev_scl == 1 and scl == 0:               # falling edge: act
            if m.cycle - self._last_edge < self.min_high:
                self.errors.append(("scl high too short", m.cycle))
            self._last_edge = m.cycle
            if self._state == "addr" and self._nbits == 8:
                if (self._shift >> 1) == self.address:
                    self._drive_low = True                    # ACK
                    rw = self._shift & 1
                    self._state = "read" if rw else "write"
                    self._first_write_byte = not rw
                    self.transfers.append(("r" if rw else "w", self.ptr, []))
                    self._scl_low_hold = self.stretch
                else:
                    self._state = "idle"                      # NACK: leave released
            elif self._state == "addr" and self._nbits == 9:
                self._drive_low = False
                self._nbits = 0
                if self._state == "addr":
                    self._state = "idle"
            elif self._state == "write" and self._nbits == 8:
                if self._first_write_byte:
                    self.ptr = self._shift
                    self._first_write_byte = False
                else:
                    self.mem[self.ptr] = self._shift
                    self.transfers[-1][2].append(self._shift)
                    self.ptr = (self.ptr + 1) & 0xFF
                self._drive_low = True
                self._scl_low_hold = self.stretch
            elif self._state == "write" and self._nbits == 9:
                self._drive_low = False
                self._nbits, self._shift = 0, 0
            elif self._state == "read":
                if self._nbits == 9:                          # after ACK: next byte
                    self._nbits = 0
                if self._nbits == 0:
                    self._tx_byte = self.mem[self.ptr]
                    self.transfers[-1][2].append(self._tx_byte)
                    self.ptr = (self.ptr + 1) & 0xFF
                if self._nbits < 8:
                    bit = (self._tx_byte >> (7 - self._nbits)) & 1
                    self._drive_low = bit == 0
                else:
                    self._drive_low = False                   # release for ACK
        # in read state the data bit for bit 0 must be presented right after the ACK
        self._prev_scl, self._prev_sda = scl, sda
        self._drive(m)


class I2cDecoder:
    """Passive I2C bus decoder on two pads (SCL, SDA): drives nothing, reads
    m.pad() and m.cycle only. Unlike I2cSlaveModel it follows every
    transfer, whatever the address, and reports the bytes as they are on
    the wire.

    A START is SDA falling while SCL is high in this cycle and the previous
    one; it is a repeated START ("restart") when no STOP came since the last
    START. A STOP is SDA rising while SCL is high. A data bit is SDA
    sampled at a rising SCL edge, counted when SCL falls again without a
    START or STOP in between (the clock pulse that carries a repeated START
    or a STOP is not a bit); nine bits make a byte and its ACK bit as seen
    on SDA at the ninth clock (0 = ACK, 1 = NACK). The first byte after a
    START is the 7-bit address and the R/W bit (1 = read).

    Records:
      events     (kind, cycle, detail): "start", "restart", "stop", "byte"
                 (detail (byte, ack); the cycle of the ninth rising SCL edge)
      transfers  tuples (kind, address, rw, addr_ack, [(byte, ack), ...], end):
                 kind "start" or "restart", end "stop", "restart" or None
                 (still open); address and rw are None until the address
                 byte is complete. Built in place: the last one may be open.
      errors     (kind, cycle, detail): "stop inside a byte", "start inside
                 a byte" (detail: the bits of the byte seen), "bit outside
                 a transfer", "incomplete byte" (from finish(): the record
                 ends inside a byte).

    The first sample only sets the previous levels; feed the idle bus first
    (both lines high) when the record starts at a START condition.
    """

    def __init__(self, scl, sda):
        self.scl, self.sda = scl, sda
        self.events = []
        self.transfers = []
        self.errors = []
        self._prev = None         # (scl, sda) of the previous cycle
        self._open = False        # inside a transfer (a START seen, no STOP since)
        self._bits = []
        self._pending = None      # (sda, cycle) sampled at a rising SCL edge, not yet a bit
        self._cur = None          # the transfer being built, as a list
        self._outside_flagged = False

    def _close(self, end):
        if self._cur is not None:
            self._cur[5] = end
            self.transfers[-1] = tuple(self._cur)
            self._cur = None

    def on_cycle(self, m):
        pad = m.pad()
        scl, sda = _bit(pad, self.scl), _bit(pad, self.sda)
        prev, self._prev = self._prev, (scl, sda)
        if prev is None:
            return
        pscl, psda = prev
        if scl and pscl and psda and not sda:                  # START / repeated START
            kind = "restart" if self._open else "start"
            if self._bits:
                self.errors.append(("start inside a byte", m.cycle, len(self._bits)))
            self._close("restart" if self._open else None)
            self.events.append((kind, m.cycle, None))
            self._cur = [kind, None, None, None, [], None]
            self.transfers.append(tuple(self._cur))
            self._open, self._bits, self._pending, self._outside_flagged = True, [], None, False
        elif scl and pscl and not psda and sda:                # STOP
            if self._bits:
                self.errors.append(("stop inside a byte", m.cycle, len(self._bits)))
            self.events.append(("stop", m.cycle, None))
            self._close("stop")
            self._open, self._bits, self._pending = False, [], None
        elif scl and not pscl:                                 # rising SCL: sample
            self._pending = (sda, m.cycle)
        elif pscl and not scl and self._pending is not None:   # falling SCL: a bit
            self._bit()

    def _bit(self):
        sda, cycle = self._pending
        self._pending = None
        if not self._open:
            if not self._outside_flagged:
                self.errors.append(("bit outside a transfer", cycle, None))
            self._outside_flagged = True
            return
        self._bits.append(sda)
        if len(self._bits) == 9:
            byte = sum(b << (7 - i) for i, b in enumerate(self._bits[:8]))
            ack = self._bits[8]
            self._bits = []
            self.events.append(("byte", cycle, (byte, ack)))
            cur = self._cur
            if cur[1] is None:
                cur[1], cur[2], cur[3] = byte >> 1, byte & 1, ack
            else:
                cur[4].append((byte, ack))
            self.transfers[-1] = tuple(cur)

    def finish(self):
        """The record ends: a bit sampled with SCL still high counts, and a
        byte in progress is reported."""
        if self._pending is not None:
            self._bit()
        if self._bits:
            self.errors.append(("incomplete byte", None, len(self._bits)))
