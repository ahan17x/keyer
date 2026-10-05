"""SW-DP target model (ARM Serial Wire Debug, ADIv5) for exercising an SWD
host on the ISS and the RTL.

Same contract as tools/protomodels.py: on_cycle(m) is called before each
Machine.step(); the model reads m.pad() (bits 0-7 uio, 8-15 ui, 16-23 uo),
drives m.ext_uio and reads m.cycle. It knows only the protocol; its
parameters are the two pin numbers and timing limits in core cycles.

The protocol as the target sees it. Everything happens on rising SWCLK
edges: the target samples SWDIO there (the level just before the edge) and
changes its own output there. Clock periods are numbered by the rising edge
that ends them, the start bit being period 1:
  1..8    request, host drives: start (1), APnDP, RnW, A[2], A[3], parity
          (of the four bits before it), stop (0), park (1)
  9       turnaround; from edge 9 the target drives
  10..12  ACK[0..2]: OK = 1,0,0  WAIT = 0,1,0  FAULT = 0,0,1
  read, ACK OK:   13..44 data bit 0..31, 45 parity (even over the data),
                  46 turnaround (the target lets go at edge 45)
  write, ACK OK:  13 turnaround (the target lets go at edge 12),
                  14..45 data bit 0..31 from the host, 46 parity
  otherwise:      13 turnaround, no data phase
A request with wrong parity, stop or park gets no response at all.

Getting there: the target powers up in JTAG (dormant here: it answers
nothing). At least 50 clocks with SWDIO high are a line reset; the 16-bit
sequence 0xE79E, LSB first, selects SWD; after the next line reset the
target is in the reset state, in which the first request must be a read of
DPIDR (anything else gets no response).

Registers: DPIDR (read DP 0x0), CTRL/STAT (DP 0x4, read-write), SELECT
(write DP 0x8), RDBUFF (read DP 0xC, 0); ABORT (write DP 0x0) is accepted;
AP accesses are acknowledged and read 0.
"""

ACK_OK, ACK_WAIT, ACK_FAULT = 1, 2, 4


def _bit(v, p):
    return (v >> p) & 1


class SwdTargetModel:
    """SW-DP on an output pin (SWCLK) and a uio pin (SWDIO, pulled up).

    dpidr        the value of DPIDR
    setup, hold  the host's SWDIO must not change in the `setup` cycles
                 before a rising edge at which the target samples it, nor in
                 the `hold` cycles after it
    min_high, min_low   shortest SWCLK phases accepted
    delay        cycles from a rising edge to the target's output change
    selected     start with SWD already selected and reset (skip the
                 connect sequence)

    `next_ack` (ACK_WAIT or ACK_FAULT) answers the next valid request with
    that ACK, once; `corrupt_parity` inverts the parity bit of the next read,
    once. `transactions` records every answered request as a dict (kind
    "r"/"w", ap, addr, ack, data, parity_ok, cycle); `ignored` every request
    that got no response, with the reason; `events` line resets and the
    select sequence. `errors` holds violations by the host:
      ("swdio setup", edge cycle, change cycle), ("swdio hold", ...),
      ("swclk high too short", cycle, length), ("swclk low too short", ...),
      ("contention", cycle)   both sides driving SWDIO in the same cycle
    """

    def __init__(self, swclk, swdio, dpidr=0x2BA01477, setup=1, hold=1, min_high=1, min_low=1,
                 delay=0, selected=False):
        self.swclk, self.swdio, self.dpidr = swclk, swdio, dpidr
        self.setup, self.hold, self.min_high, self.min_low, self.delay = setup, hold, min_high, min_low, delay
        self.selected = selected
        self.state = "active" if selected else "dormant"      # dormant, reset, active
        self.ctrlstat, self.select = 0, 0
        self.next_ack, self.corrupt_parity = None, False
        self.transactions, self.ignored, self.events, self.errors = [], [], [], []
        self.rises, self.falls = [], []
        self._prev_clk, self._prev_io = None, 1
        self._last_change = None        # cycle of the last SWDIO change
        self._last_sample = None        # last rising edge at which the host's level was sampled
        self._edge = 0                  # cycle of the last SWCLK edge
        self._ones = 0                  # consecutive 1 bits sampled
        self._seq = 0                   # the last 16 bits sampled, first one in bit 0
        self._n = 0                     # period of the running request (0 = none)
        self._req = 0
        self._tr = None                 # the running transaction
        self._bits = 0
        self._drive = None              # level the target drives, None = released
        self._pending = []              # (cycle, level or None): output changes on their way

    # ------------------------------------------------------------ registers

    def _read(self, ap, addr):
        if ap:
            return 0
        return {0x0: self.dpidr, 0x4: self.ctrlstat, 0x8: self.select}.get(addr, 0)

    def _write(self, ap, addr, value):
        if not ap and addr == 0x4:
            self.ctrlstat = value
        elif not ap and addr == 0x8:
            self.select = value

    # ------------------------------------------------------------ the clock

    def _out(self, c, level):
        self._pending.append((c + self.delay, level))

    def on_cycle(self, m):
        c = m.cycle
        while self._pending and self._pending[0][0] <= c:
            self._drive = self._pending.pop(0)[1]
        ext = m.ext_uio | (1 << self.swdio)                   # the pull-up
        if self._drive == 0:
            ext &= ~(1 << self.swdio)
        m.ext_uio = ext & 0xFF
        pad = m.pad()
        clk, io = _bit(pad, self.swclk), _bit(pad, self.swdio)
        host_drives = bool(getattr(m, "uio_oe", 0) & (1 << self.swdio))
        if self._drive is not None and host_drives:
            self.errors.append(("contention", c))
        if io != self._prev_io:
            self._last_change = c
            if self._last_sample is not None and c - self._last_sample <= self.hold - 1 and self._drive is None:
                self.errors.append(("swdio hold", self._last_sample, c))
        if self._prev_clk is None:
            self._prev_clk = clk                              # the level at power-up is not an edge
        if clk != self._prev_clk:
            length = c - self._edge
            if self.rises or self.falls:
                if clk == 1 and length < self.min_low:
                    self.errors.append(("swclk low too short", c, length))
                if clk == 0 and length < self.min_high:
                    self.errors.append(("swclk high too short", c, length))
            self._edge = c
            (self.rises if clk else self.falls).append(c)
            if clk == 1:
                self._rising(c, self._prev_io)
        self._prev_clk, self._prev_io = clk, io

    def _sample_host(self, c):
        """The target takes the host's level at this edge: check setup, arm hold."""
        if self._last_change is not None and c - self._last_change < self.setup:
            self.errors.append(("swdio setup", c, self._last_change))
        self._last_sample = c

    def _rising(self, c, b):
        n = self._n
        if n == 0 or n < 8:
            self._sample_host(c)
            self._ones = self._ones + 1 if b else 0
            if self._ones == 50:
                self.events.append(("line reset", c))
                if self.selected:
                    self.state = "reset"
                self._n = 0
                n = -1                                        # a reset is never part of a request
            self._seq = (self._seq >> 1) | (b << 15)
            if self._seq == 0xE79E and not self.selected:
                self.selected = True
                self.events.append(("swd selected", c))
        else:
            self._last_sample = None
        if n == -1:
            return
        if n == 0:
            if b == 1 and self._ones < 50 and self.state != "dormant" and self._ones == 1:
                self._n, self._req = 1, 1                     # a start bit after a 0
            return
        n += 1
        self._n = n
        if n <= 8:
            self._req |= b << (n - 1)
            if n == 8:
                self._decode(c)
            return
        tr = self._tr
        if n in (9, 10, 11):
            self._out(c, _bit(tr["ack"], n - 9))
        elif tr["ack"] != ACK_OK:
            if n == 12:
                self._out(c, None)
            else:
                self._finish()
        elif tr["kind"] == "r":
            if n <= 43:
                self._out(c, _bit(tr["data"], n - 12))
            elif n == 44:
                par = bin(tr["data"]).count("1") & 1
                if self.corrupt_parity:
                    par, self.corrupt_parity, tr["parity_ok"] = par ^ 1, False, False
                self._out(c, par)
            elif n == 45:
                self._out(c, None)
            else:
                self._finish()
        else:
            if n == 12:
                self._out(c, None)
            elif 14 <= n <= 45:
                self._sample_host(c)
                tr["data"] |= b << (n - 14)
            elif n == 46:
                self._sample_host(c)
                tr["parity_ok"] = (bin(tr["data"]).count("1") & 1) == b
                if tr["parity_ok"]:
                    self._write(tr["ap"], tr["addr"], tr["data"])
                self._finish()

    def _decode(self, c):
        r = self._req
        ap, rnw, addr = _bit(r, 1), _bit(r, 2), (_bit(r, 3) << 2) | (_bit(r, 4) << 3)
        parity = (_bit(r, 1) + _bit(r, 2) + _bit(r, 3) + _bit(r, 4)) & 1
        why = None
        if _bit(r, 5) != parity:
            why = "parity"
        elif _bit(r, 6) != 0 or _bit(r, 7) != 1:
            why = "framing"
        elif self.state == "reset" and not (rnw and not ap and addr == 0):
            why = "not DPIDR after a line reset"
        if why:
            self.ignored.append((r, why, c))
            self._n = 0
            return
        self.state = "active"
        ack, self.next_ack = self.next_ack or ACK_OK, None
        self._tr = dict(kind="r" if rnw else "w", ap=ap, addr=addr, ack=ack, parity_ok=True, cycle=c,
                        data=self._read(ap, addr) if rnw and ack == ACK_OK else 0)

    def _finish(self):
        self.transactions.append(self._tr)
        self._tr, self._n, self._ones = None, 0, 0
