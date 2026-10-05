"""JTAG (IEEE 1149.1) target model for exercising a JTAG master on the ISS
and, through the lockstep harness, on the RTL.

One TAP, as the standard defines it, and nothing about the firmware that
drives it. Like the models in protomodels.py it has on_cycle(m), called
before each Machine.step(): it reads m.pad() (TCK, TMS and TDI are pad
pins), drives TDO through m.ext_ui, and keeps `errors`, a list of protocol
violations with their cycle numbers.

What the standard fixes and this model implements:
  - the 16-state TAP controller, advancing on TMS at every rising TCK edge;
  - TMS and TDI are sampled at the rising edge of TCK (the model takes
    their level in the cycle before the one in which TCK reads high);
  - the instruction register captures a value whose two least significant
    bits are 01 in Capture-IR, shifts in Shift-IR, and the new instruction
    takes effect on the falling edge of TCK in Update-IR;
  - Test-Logic-Reset selects IDCODE; the BYPASS instruction is all ones and
    every instruction this device does not know selects BYPASS as well;
  - IDCODE is a 32-bit register whose bit 0 is 1, BYPASS a one-bit register
    that captures 0; the selected register is loaded in Capture-DR and
    shifts towards TDO, LSB first, on each rising edge in Shift-DR;
  - TDO changes on the falling edge of TCK and is driven only in Shift-DR
    and Shift-IR; otherwise it is released (the pull-up shows 1).
"""

from collections import namedtuple

TAP_STATES = (
    "Test-Logic-Reset", "Run-Test/Idle",
    "Select-DR-Scan", "Capture-DR", "Shift-DR", "Exit1-DR", "Pause-DR", "Exit2-DR", "Update-DR",
    "Select-IR-Scan", "Capture-IR", "Shift-IR", "Exit1-IR", "Pause-IR", "Exit2-IR", "Update-IR",
)

# state -> (next state if TMS = 0, next state if TMS = 1), IEEE 1149.1 figure 6-1
TAP_NEXT = {
    "Test-Logic-Reset": ("Run-Test/Idle", "Test-Logic-Reset"),
    "Run-Test/Idle": ("Run-Test/Idle", "Select-DR-Scan"),
    "Select-DR-Scan": ("Capture-DR", "Select-IR-Scan"),
    "Capture-DR": ("Shift-DR", "Exit1-DR"),
    "Shift-DR": ("Shift-DR", "Exit1-DR"),
    "Exit1-DR": ("Pause-DR", "Update-DR"),
    "Pause-DR": ("Pause-DR", "Exit2-DR"),
    "Exit2-DR": ("Shift-DR", "Update-DR"),
    "Update-DR": ("Run-Test/Idle", "Select-DR-Scan"),
    "Select-IR-Scan": ("Capture-IR", "Test-Logic-Reset"),
    "Capture-IR": ("Shift-IR", "Exit1-IR"),
    "Shift-IR": ("Shift-IR", "Exit1-IR"),
    "Exit1-IR": ("Pause-IR", "Update-IR"),
    "Pause-IR": ("Pause-IR", "Exit2-IR"),
    "Exit2-IR": ("Shift-IR", "Update-IR"),
    "Update-IR": ("Run-Test/Idle", "Select-DR-Scan"),
}

# One pass through Update-DR or Update-IR. tdi and tdo are the nbits bits that
# went in and came out since the last Capture (or since power-up, if the TAP
# woke up in the middle of a scan), first bit in bit 0; ir is the instruction
# that was in effect during the scan; cycle is the falling TCK edge in Update.
Scan = namedtuple("Scan", "kind nbits tdi tdo ir cycle")


def _bit(v, p):
    return (v >> p) & 1


class JtagTapModel:
    """A JTAG TAP on three pad pins (TCK, TMS, TDI) answering on a ui pin (TDO).

    Pins: tck, tms, tdi are pad indices (0-23), tdo_ui_bit is the bit of
    ext_ui the model drives.
    Timing, all in core cycles:
      setup, hold      TMS and TDI must not change in the `setup` cycles
                       before a rising TCK edge nor in the `hold` cycles
                       after it;
      min_high/min_low the shortest TCK high and low phases accepted;
      tdo_delay        cycles from the falling TCK edge on the pad to the new
                       TDO level (0: the same cycle).
    Device: idcode (bit 0 must be 1), ir_len, idcode_ir (the instruction that
    selects IDCODE). start_state and start_ir give the power-up condition: a
    TAP without a reset pin can wake up anywhere, so nothing is assumed; by
    default it wakes in Shift-IR with BYPASS selected.

    Observed: state (the current one), state_log [(cycle, state)] after every
    rising edge (entry 0 is the start state), sampled [(cycle, tms, tdi)] at
    every rising edge, scans [Scan], ir (the instruction in effect),
    tck_rises and tck_falls (cycles), tms_changes and tdi_changes (cycles),
    errors [(what, cycle, cycles measured)].
    """

    def __init__(self, tck, tms, tdi, tdo_ui_bit, setup=1, hold=1, min_high=1, min_low=1,
                 tdo_delay=0, idcode=0x14B592AB, ir_len=4, idcode_ir=0b0010,
                 start_state="Shift-IR", start_ir=None):
        if start_state not in TAP_NEXT:
            raise ValueError("unknown TAP state %r" % start_state)
        if not idcode & 1:
            raise ValueError("bit 0 of an IDCODE is 1")
        if ir_len < 2:
            raise ValueError("an instruction register has at least two bits")
        self.tck, self.tms, self.tdi, self.tdo = tck, tms, tdi, tdo_ui_bit
        self.setup, self.hold = setup, hold
        self.min_high, self.min_low, self.tdo_delay = min_high, min_low, tdo_delay
        self.idcode, self.ir_len = idcode & 0xFFFFFFFF, ir_len
        self.bypass_ir = (1 << ir_len) - 1
        self.idcode_ir = idcode_ir & self.bypass_ir
        if self.idcode_ir == self.bypass_ir:
            raise ValueError("the all-ones instruction is BYPASS")
        self.state = start_state
        if start_ir is None:                    # Test-Logic-Reset means IDCODE is selected
            start_ir = self.idcode_ir if start_state == "Test-Logic-Reset" else self.bypass_ir
        self.ir = start_ir & self.bypass_ir
        self.state_log = [(0, start_state)]
        self.sampled = []
        self.scans = []
        self.errors = []
        self.tck_rises, self.tck_falls = [], []
        self.tms_changes, self.tdi_changes = [], []
        self._sr, self._sr_len = 0, 1           # the shift register between TDI and TDO
        self._n = self._in = self._out = 0      # bits shifted since the last Capture
        self._prev = None                       # (tck, tms, tdi) of the previous cycle
        self._last_rise = self._last_fall = None
        self._tms_at = self._tdi_at = None      # cycle of the last change
        self._tdo = 1                           # level on the pad now (released: pull-up)
        self._tdo_queue = []                    # (cycle, level) changes on their way out

    # ---------------------------------------------------------------- the TAP

    def _selected_dr(self):
        """(capture value, length) of the data register the instruction selects."""
        if self.ir == self.idcode_ir:
            return self.idcode, 32
        return 0, 1                              # BYPASS, also for unknown instructions

    def _rising(self, cycle, tms, tdi):
        s = self.state
        self.sampled.append((cycle, tms, tdi))
        if s == "Capture-DR":
            self._sr, self._sr_len = self._selected_dr()
            self._n = self._in = self._out = 0
        elif s == "Capture-IR":
            self._sr, self._sr_len = 0b01, self.ir_len
            self._n = self._in = self._out = 0
        elif s in ("Shift-DR", "Shift-IR"):
            self._out |= (self._sr & 1) << self._n
            self._in |= tdi << self._n
            self._n += 1
            self._sr = (self._sr >> 1) | (tdi << (self._sr_len - 1))
        self.state = TAP_NEXT[s][tms]
        self.state_log.append((cycle, self.state))

    def _falling(self, cycle):
        s = self.state
        if s == "Test-Logic-Reset":
            self.ir = self.idcode_ir
        elif s == "Update-IR":
            self.scans.append(Scan("IR", self._n, self._in, self._out, self.ir, cycle))
            self.ir = self._sr & self.bypass_ir
        elif s == "Update-DR":
            self.scans.append(Scan("DR", self._n, self._in, self._out, self.ir, cycle))
        tdo = (self._sr & 1) if s in ("Shift-DR", "Shift-IR") else 1
        self._tdo_queue.append((cycle + self.tdo_delay, tdo))

    # ---------------------------------------------------------------- per cycle

    def on_cycle(self, m):
        c = m.cycle
        pad = m.pad()
        now = (_bit(pad, self.tck), _bit(pad, self.tms), _bit(pad, self.tdi))
        if self._prev is not None:
            tck0, tms0, tdi0 = self._prev
            tck, tms, tdi = now
            if tms != tms0:
                self.tms_changes.append(c)
                self._tms_at = c
                if self._last_rise is not None and 0 < c - self._last_rise < self.hold:
                    self.errors.append(("tms hold", c, c - self._last_rise))
            if tdi != tdi0:
                self.tdi_changes.append(c)
                self._tdi_at = c
                if self._last_rise is not None and 0 < c - self._last_rise < self.hold:
                    self.errors.append(("tdi hold", c, c - self._last_rise))
            if tck0 == 0 and tck == 1:
                self.tck_rises.append(c)
                if self._last_fall is not None and c - self._last_fall < self.min_low:
                    self.errors.append(("tck low too short", c, c - self._last_fall))
                if self._tms_at is not None and c - self._tms_at < self.setup:
                    self.errors.append(("tms setup", c, c - self._tms_at))
                if self._tdi_at is not None and c - self._tdi_at < self.setup:
                    self.errors.append(("tdi setup", c, c - self._tdi_at))
                self._rising(c, tms0, tdi0)      # the levels just before the edge
                self._last_rise = c
            elif tck0 == 1 and tck == 0:
                self.tck_falls.append(c)
                if self._last_rise is not None and c - self._last_rise < self.min_high:
                    self.errors.append(("tck high too short", c, c - self._last_rise))
                self._falling(c)
                self._last_fall = c
        self._prev = now
        while self._tdo_queue and self._tdo_queue[0][0] <= c:
            self._tdo = self._tdo_queue.pop(0)[1]
        m.ext_ui = (m.ext_ui & ~(1 << self.tdo)) | (self._tdo << self.tdo)
