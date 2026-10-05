"""PS/2 device model: the keyboard (or mouse) end of a PS/2 port.

Used like the models in protomodels.py: on_cycle(m) is called before each
Machine.step(); the model reads m.pad() and drives m.ext_uio. It knows only
the PS/2 protocol, nothing about the firmware that plays the host.

The bus. CLK and DATA are open-drain with pull-ups; either side can pull a
line low. The device generates the clock in both directions. All times are
in core cycles.

Device to host (a frame the device sends): 11 bits, start (0), eight data
bits LSB first, odd parity, stop (1). The device puts each bit on DATA in the
middle of the CLK high phase, pulls CLK low `half` cycles, releases it `half`
cycles; the host samples on the falling edge. The device sends only when the
bus has been idle (CLK and DATA high) for `idle` cycles. If it finds CLK low
during a high phase of its own clock (the host inhibits), before the falling
edge of the eleventh pulse, it aborts, releases DATA, and sends the whole
frame again once the bus is idle.

Host to device (a command): the host holds CLK low for at least
`min_inhibit` cycles, pulls DATA low (request to send, which is also the
start bit) and releases CLK. `rts_delay` cycles later the device clocks
eleven pulses. The host changes DATA only while CLK is low; the device
samples on each rising edge: eight data bits, parity, stop. If the parity is
odd and the stop bit is 1 it pulls DATA low in the middle of the high phase
before the eleventh pulse (the ACK) and releases DATA and CLK at the end of
that pulse.

What the model reports in `errors` (protocol violations by the host), each
with the cycle number:
  ("inhibit too short", cycle, length)   the host held CLK low for less than
                                         min_inhibit cycles
  ("data changed while clk high", cycle) the host changed DATA while CLK was
                                         high during the bits of a command
  ("request to send without inhibit", cycle)
  ("host parity", cycle, byte)           a command with even parity
  ("host stop bit", cycle, byte)         DATA still low at the stop bit
  ("host drives clk high", cycle)        open-drain violation: the pad is
  ("host drives data high", cycle)       high while the host's output enable
                                         is on (needs m.uio_oe)

The host's own drive is taken from m.uio_oe when the machine has it (the
golden model does), which is what a bus monitor on the host's pins would
see; otherwise it is inferred from the pad whenever the device itself is not
pulling that line. The device's behaviour uses only the pad.
"""


def _bit(v, p):
    return (v >> p) & 1


def odd_parity(byte):
    """The parity bit that makes the number of ones in byte + parity odd."""
    return 1 ^ (bin(byte & 0xFF).count("1") & 1)


class _Frame:
    __slots__ = ("byte", "bits", "stop_after", "delay")

    def __init__(self, byte, bits, stop_after, delay):
        self.byte, self.bits, self.stop_after, self.delay = byte, bits, stop_after, delay


class Ps2DeviceModel:
    """A PS/2 device on two uio pins.

    clk, data     pin numbers (uio)
    half          CLK low time and CLK high time, in cycles (clock period 2 * half)
    min_inhibit   the shortest legal host inhibit (CLK held low), in cycles
    idle          cycles the bus must be idle before the device sends (default 2 * half)
    rts_delay     cycles from the host's release of CLK with DATA low to the
                  device's first falling edge (default 2 * half)

    Injection (the test's handles on the device, not protocol parameters):
      send(byte, parity_error=, start_bit=, stop_bit=, stop_after=, delay=)
      acks          list of booleans consumed one per command: False withholds
                    the ACK bit (the eleventh pulse is still clocked)
      respond       False: the device ignores requests to send (never clocks)
      stall_after   n: the device clocks only n pulses (1..10) of the next
                    command, then stops and forgets it
      replies       {command: [bytes]} queued after an acknowledged command
      default_reply bytes queued after any other acknowledged command

    Observations: sent / sent_log (frames completed), aborted (frames cut off
    by a host inhibit, sent again later), abandoned (frames the device was
    told to stop in the middle of), commands / command_log, stalled,
    requests, inhibits [(start, length)], clk_falls, clk_rises,
    host_data_edges.
    """

    def __init__(self, clk, data, half, min_inhibit, idle=None, rts_delay=None):
        if half < 4:
            raise ValueError("half must be at least 4 cycles")
        self.clk, self.data, self.half = clk, data, half
        self.min_inhibit = min_inhibit
        self.idle = 2 * half if idle is None else idle
        self.rts_delay = 2 * half if rts_delay is None else rts_delay
        self._q = half // 2                 # DATA changes this long after a rising edge
        # injection
        self.acks = []
        self.respond = True
        self.stall_after = None
        self.replies = {}
        self.default_reply = []
        # observations
        self.errors = []
        self.sent, self.sent_log = [], []           # bytes; (start, end, byte)
        self.aborted, self.abandoned = [], []       # (cycle, byte)
        self.commands, self.command_log = [], []    # bytes; (first fall, end, byte, acked)
        self.bad_commands = []                      # (cycle, byte, parity bit, stop bit)
        self.stalled = []                           # (cycle, pulses clocked) of a command left unfinished
        self.requests = []                          # cycle each request to send was recognised
        self.host_aborts = []                       # cycle the host inhibited during a command
        self.inhibits = []                          # (start cycle, length) of each host CLK low
        self.clk_falls, self.clk_rises = [], []     # the device's own clock edges
        self.host_data_edges = []                   # (cycle, level) of the host's DATA drive
        # state
        self._queue = []
        self._state = "idle"              # idle, inhibit, rts, send, recv
        self._clk_low = False             # the device is pulling CLK low
        self._data_low = False            # the device is pulling DATA low
        self._idle_n = 0
        self._t = 0
        self._t0 = 0
        self._bits = []
        self._ack = False
        self._good = False
        self._deaf = False                # after a stall: wait for the host to withdraw its request
        self._high_prev = (False, False)  # host driving (CLK, DATA) high in the previous cycle
        self._hc_prev = False             # host pulling CLK low in the previous cycle
        self._hd_prev = False
        self._hc_start = 0

    # ------------------------------------------------------------ injection

    def send(self, byte, parity_error=False, start_bit=0, stop_bit=1, stop_after=None, delay=0):
        """Queue a frame. parity_error inverts the parity bit; start_bit=1 or
        stop_bit=0 make a framing error; stop_after=n abandons the frame
        after n clock pulses (1..10) and does not send it again; delay asks
        for that many more idle cycles before the frame starts."""
        if stop_after is not None and not 1 <= stop_after <= 10:
            raise ValueError("stop_after must be 1..10")
        byte &= 0xFF
        par = odd_parity(byte) ^ (1 if parity_error else 0)
        bits = [start_bit & 1] + [(byte >> i) & 1 for i in range(8)] + [par, stop_bit & 1]
        self._queue.append(_Frame(byte, bits, stop_after, delay))

    @property
    def pending(self):
        """Frames queued and not yet completed or abandoned."""
        return len(self._queue)

    @property
    def state(self):
        return self._state

    # ------------------------------------------------------------ the bus

    def _drive(self, m):
        ext = m.ext_uio | (1 << self.clk) | (1 << self.data)
        if self._clk_low:
            ext &= ~(1 << self.clk)
        if self._data_low:
            ext &= ~(1 << self.data)
        m.ext_uio = ext & 0xFF

    def _set_clk(self, low, cycle):
        if low != self._clk_low:
            (self.clk_falls if low else self.clk_rises).append(cycle)
            self._clk_low = low

    def on_cycle(self, m):
        cyc = m.cycle
        pad = m.pad()                       # the host's drive of this cycle, ours of the last
        c, d = _bit(pad, self.clk), _bit(pad, self.data)
        oe = getattr(m, "uio_oe", None)
        if oe is not None:
            hc_oe, hd_oe = _bit(oe, self.clk), _bit(oe, self.data)
            high = (bool(hc_oe and c), bool(hd_oe and d))
            if high[0] and not self._high_prev[0]:  # reported once, when it starts
                self.errors.append(("host drives clk high", cyc))
            if high[1] and not self._high_prev[1]:
                self.errors.append(("host drives data high", cyc))
            self._high_prev = high
            hc, hd = bool(hc_oe and not c), bool(hd_oe and not d)
        else:
            hc, hd = (c == 0 and not self._clk_low), (d == 0 and not self._data_low)
        state_before = self._state

        # ---- the device (uses the pad only)
        st = self._state
        if st == "idle":
            if c == 0:                              # we are not driving: the host inhibits
                self._state = "inhibit"
            elif d == 0:                            # DATA low, CLK high, no inhibit before it
                self.errors.append(("request to send without inhibit", cyc))
                self.requests.append(cyc)
                self._state, self._t = "rts", 0
            else:
                self._idle_n += 1
                if self._queue and self._idle_n >= self.idle + self._queue[0].delay:
                    self._state, self._t, self._t0 = "send", 0, cyc
                    self._send_step(cyc)
        elif st == "inhibit":
            if c == 1:
                if d == 0:                          # CLK released with DATA low: request to send
                    self.requests.append(cyc)
                    self._state, self._t = "rts", 0
                else:
                    self._state, self._idle_n = "idle", 0
        elif st == "rts":
            if c == 0:
                self._state, self._deaf = "inhibit", False
            elif d == 1:                            # the host withdrew the request
                self._state, self._idle_n, self._deaf = "idle", 0, False
            elif self.respond and not self._deaf:
                self._t += 1
                if self._t >= self.rts_delay:
                    self._state, self._t, self._t0, self._bits = "recv", 0, cyc, []
                    self._ack = False
                    self._recv_step(cyc, d)
        elif st == "send":
            if not self._clk_low and c == 0:        # inhibited in a high phase: abort, send again later
                self._data_low = False
                self.aborted.append((cyc, self._queue[0].byte))
                self._state = "inhibit"
            else:
                self._t += 1
                self._send_step(cyc)
        elif st == "recv":
            if not self._clk_low and c == 0:        # the host aborts its own command
                self._data_low = False
                self.host_aborts.append(cyc)
                self._state = "inhibit"
            else:
                self._t += 1
                self._recv_step(cyc, d)
        self._drive(m)

        # ---- the bus monitor (the host's side of the rules)
        if hc and not self._hc_prev:
            self._hc_start = cyc
        elif self._hc_prev and not hc:
            length = cyc - self._hc_start
            self.inhibits.append((self._hc_start, length))
            if length < self.min_inhibit:
                self.errors.append(("inhibit too short", cyc, length))
        if hd != self._hd_prev:
            self.host_data_edges.append((cyc, 0 if hd else 1))
            clk_now = not self._clk_low and not hc  # the CLK pad during this cycle
            if state_before == "recv" and self._state == "recv" and clk_now:
                self.errors.append(("data changed while clk high", cyc))
        self._hc_prev, self._hd_prev = hc, hd

    # ------------------------------------------------------------ device to host

    def _send_step(self, cyc):
        """One cycle of a frame the device sends. Bit k occupies the cycles
        k * 2 * half onwards: DATA at 0, CLK falls half - q later, rises
        half after that (so DATA changes q cycles into the high phase)."""
        fr = self._queue[0]
        k, p = divmod(self._t, 2 * self.half)
        fall = self.half - self._q
        if p == 0:
            self._data_low = fr.bits[k] == 0
        elif p == fall:
            self._set_clk(True, cyc)
        elif p == fall + self.half:
            self._set_clk(False, cyc)
            if fr.stop_after == k + 1:              # told to stop here: give up for good
                self._queue.pop(0)
                self._data_low = False
                self.abandoned.append((cyc, fr.byte))
                self._state, self._idle_n = "idle", 0
            elif k == 10:                           # the eleventh pulse is over: frame complete
                self._queue.pop(0)
                self._data_low = False
                self.sent.append(fr.byte)
                self.sent_log.append((self._t0, cyc, fr.byte))
                self._state, self._idle_n = "idle", 0

    # ------------------------------------------------------------ host to device

    def _recv_step(self, cyc, d):
        """One cycle of a command: pulse k falls at k * 2 * half and rises
        half later; DATA is sampled on rises 0..9 (data, parity, stop); the
        ACK goes on DATA q cycles after rise 9 and off at rise 10."""
        k, p = divmod(self._t, 2 * self.half)
        if p == 0:
            self._set_clk(True, cyc)
        elif p == self.half:
            self._set_clk(False, cyc)
            if self.stall_after == k + 1:           # told to stop here: drop the command
                self.stall_after = None
                self._data_low = False
                self.stalled.append((cyc, k + 1))
                self._state, self._t, self._deaf = "rts", 0, True
            elif k <= 9:
                self._bits.append(d)
            else:
                self._data_low = False
                self._recv_done(cyc)
        elif k == 9 and p == self.half + self._q:
            byte = sum(b << i for i, b in enumerate(self._bits[:8]))
            par, stop = self._bits[8], self._bits[9]
            good = True
            if par != odd_parity(byte):
                self.errors.append(("host parity", cyc, byte))
                good = False
            if stop != 1:
                self.errors.append(("host stop bit", cyc, byte))
                good = False
            if not good:
                self.bad_commands.append((cyc, byte, par, stop))
            want = self.acks.pop(0) if self.acks else True
            self._ack = good and want
            self._good = good
            self._data_low = self._ack

    def _recv_done(self, cyc):
        byte = sum(b << i for i, b in enumerate(self._bits[:8]))
        if self._good:
            self.commands.append(byte)
            self.command_log.append((self._t0, cyc, byte, self._ack))
            if self._ack:
                for b in self.replies.get(byte, self.default_reply):
                    self.send(b)
        self._state, self._idle_n = "idle", 0
