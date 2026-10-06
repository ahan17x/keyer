"""Keyer host driver: one API, two transports.

The Keyer chip is driven over its SPI slave (mode 0, MSB first; SCK on ui[0],
MOSI on ui[1], CS_n on ui[2], MISO on uo[0]; register map in docs/isa.md
section 6). This module is the host side: load a program, set PCs, run and
stop threads, push and pop the FIFOs without overrunning them, read status
and pins, read the capture buffer, load and start a replay; and, on the PC
side, list a capture's timing and decode it with the protocol models of
tools/protomodels*.py (docs/CAPTURE.md section 5).

    host = KeyerHost(transport)
    await host.load_program(words)
    await host.run(0b01)
    await host.push(0, b"hello")
    data = await host.pop(0, 5, wait=True)

Two transports implement the same three calls, `xfer(cmd, data)`,
`idle(cycles)` and `reset()`:

- `SimTransport(dut, pads)`: drives the pads of the cocotb testbench
  (test/tb.v). Used by test/test_host.py.
- `BoardTransport(...)`: the Tiny Tapeout demo board. Runs under MicroPython
  on the board's RP2350 and bit-bangs the four pins.

Both use the same `BitBangSPI`, so the code that wiggles SCK, MOSI and CS_n on
the board is the code the simulation exercises; only the four pin functions
and the delay differ. No transaction may be in progress when the chip's
reset is released: CS_n must be high from two core clocks before `rst_n`
rises (SEMANTICS 10.1, DECISIONS D-038). Every transport enforces it:
`reset()` parks the SPI pins (CS_n high, SCK and MOSI low) before it asserts
reset and keeps them parked until after the release, refuses to run inside a
transaction, and `xfer()` refuses to start while reset is asserted. Every call is a coroutine so that one test body runs on
both: under cocotb it is awaited, on the board `run_sync()` drives it to
completion (nothing there really suspends). The `selftest_*` functions at the
bottom are such bodies; `test/test_host.py` runs them in simulation and
`python3 tools/keyerhost.py selftest` (or `keyerhost.main(["selftest"])` at
the MicroPython prompt) runs them on the board.

MicroPython-compatible: no imports beyond the standard ones at module level.
The board transport has NOT been run on hardware yet (no chip exists); the
pin numbers follow the demo board firmware's GPIO map (docs in the class).

SPDX-License-Identifier: Apache-2.0
"""

# ---- register map (docs/isa.md section 6) ---------------------------------
R_CTRL, R_STAT, R_PC0, R_PC1, R_IMEM_ADDR, R_IMEM_DATA = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
R_INBOX0, R_OUTBOX0, R_INBOX1, R_OUTBOX1, R_LEVELS, R_PINMODE = 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B
R_IRQEN, R_PINS, R_FIFOCLR, R_ID, R_PINOUT = 0x0C, 0x0D, 0x0E, 0x0F, 0x10
R_CR_CTRL, R_CAP_CFG, R_CAP_BUF, R_REP_CFG, R_REP_BUF, R_CR_COUNT = 0x11, 0x12, 0x13, 0x14, 0x15, 0x16

FIFO_DEPTH = 16
RESET_GUARD = 4         # core clocks CS_n is held high around a reset edge (the chip needs 2 before release)
ID_BYTE = 0x4B          # 'K'
CR_ARM, CR_DISARM, CR_START, CR_STOP = 1, 2, 4, 8


class KeyerError(Exception):
    pass


class KeyerTimeout(KeyerError):
    pass


def run_sync(coro):
    """Drive a coroutine that never really suspends (the board transport) to
    completion and return its result. Works on CPython and MicroPython."""
    try:
        while True:
            coro.send(None)
    except StopIteration as e:
        return e.value if hasattr(e, "value") else None


# ---- SPI ---------------------------------------------------------------------

class BitBangSPI:
    """Mode 0 SPI master over four pin functions.

    set_sck(v), set_mosi(v), set_csn(v) drive a pin, get_miso() reads one;
    half() is a coroutine that waits half an SCK period (it may return at
    once on a slow host). Timing per bit: MOSI and SCK low, half, SCK high,
    sample MISO, half. A transaction is CS_n low, half, command byte, data
    bytes, SCK low, half, CS_n high, two halves. The chip needs each half to
    be at least 4 core clocks and CS_n high for at least 4 (SEMANTICS 10.1).

    `busy` is true from CS_n low to CS_n high; `in_reset` is set by the
    transport while it holds the chip in reset. xfer() raises KeyerError
    while `in_reset`, and park() (the idle pin state: CS_n high, SCK and MOSI
    low) raises inside a transaction, so a reset can never be released over
    a transaction (SEMANTICS 10.1, DECISIONS D-038).
    """

    def __init__(self, set_sck, set_mosi, set_csn, get_miso, half):
        self.set_sck, self.set_mosi, self.set_csn, self.get_miso, self.half = \
            set_sck, set_mosi, set_csn, get_miso, half
        self.busy = False
        self.in_reset = False

    def park(self):
        """Drive the idle state. Called by a transport before it asserts
        reset; the pins then stay parked until the reset is released."""
        if self.busy:
            raise KeyerError("reset inside a host transaction: CS_n must be high when reset is released")
        self.set_sck(0)
        self.set_mosi(0)
        self.set_csn(1)

    async def _byte(self, b):
        got = 0
        for i in range(7, -1, -1):
            self.set_sck(0)
            self.set_mosi((b >> i) & 1)
            await self.half()
            self.set_sck(1)
            got = (got << 1) | (self.get_miso() & 1)
            await self.half()
        return got

    async def xfer(self, cmd, data):
        """Send cmd, then the data bytes; returns the bytes clocked in during
        the data phase (meaningful for reads)."""
        if self.in_reset:
            raise KeyerError("host transaction while reset is asserted")
        if self.busy:
            raise KeyerError("host transaction inside another")
        self.set_sck(0)
        self.set_mosi(0)
        self.busy = True
        self.set_csn(0)
        await self.half()
        await self._byte(cmd)
        rx = []
        for b in data:
            rx.append(await self._byte(b))
        self.set_sck(0)
        self.set_mosi(0)
        await self.half()
        self.set_csn(1)
        self.busy = False
        await self.half()
        await self.half()
        return rx


class SimTransport:
    """The cocotb testbench (test/tb.v): SCK, MOSI, CS_n are ui_in[0..2],
    MISO is uo_out[0]. `pads` is test/keyer_tb.py's Pads, which owns ui_in so
    that the firmware inputs ui[3..7] are preserved. half = half an SCK
    period in core clocks (4 is the chip's limit)."""

    def __init__(self, dut, pads, half=4):
        from cocotb.triggers import ClockCycles
        self.dut, self.pads, self._cc = dut, pads, ClockCycles
        self._sck, self._mosi, self._csn = 0, 0, 1
        self.half_cycles = half
        self.spi = BitBangSPI(self._set_sck, self._set_mosi, self._set_csn, self._get_miso, self._half)

    def _flush(self):
        self.pads.set_spi(self._sck, self._mosi, self._csn)

    def _set_sck(self, v):
        self._sck = v
        self._flush()

    def _set_mosi(self, v):
        self._mosi = v
        self._flush()

    def _set_csn(self, v):
        self._csn = v
        self._flush()

    def _get_miso(self):
        return int(self.dut.uo_out.value[0])      # only MISO; an X here at a sampling edge is an error

    async def _half(self):
        await self._cc(self.dut.clk, self.half_cycles)

    async def xfer(self, cmd, data):
        if int(self.dut.rst_n.value) == 0:        # a reset this transport did not make
            await self._cc(self.dut.clk, 1)       # (a release written in this very time step shows a cycle later)
            if int(self.dut.rst_n.value) == 0:
                raise KeyerError("host transaction while reset is asserted")
        return await self.spi.xfer(cmd, data)

    async def idle(self, cycles):
        await self._cc(self.dut.clk, max(1, cycles))

    async def reset(self, cycles=5):
        """Hard reset with the SPI pins parked: CS_n is high from at least
        four core clocks before rst_n falls until four after it rises."""
        self.spi.park()
        self.spi.in_reset = True
        await self._cc(self.dut.clk, RESET_GUARD)
        self.dut.rst_n.value = 0
        await self._cc(self.dut.clk, max(RESET_GUARD, cycles))
        self.dut.rst_n.value = 1
        await self._cc(self.dut.clk, RESET_GUARD)
        self.spi.in_reset = False


class BoardTransport:
    """The Tiny Tapeout demo board, under MicroPython on its RP2350.

    Pins (firmware GPIO map of the v3 demo board, `GPIOMapTTDBv3`): ui_in[0..7]
    are GP17..GP24 and uo_out[0..7] are GP33..GP40, so SCK = GP17, MOSI = GP18,
    CS_n = GP19, MISO = GP33. The older RP2040 board (`GPIOMapTT04`) has
    ui_in[0..2] on GP9..GP11 and uo_out[0] on GP5: pass board="tt04", or
    explicit GPIO numbers in `pins`. MicroPython bit-banging runs at tens of
    kHz, far below the chip's SCK limit of clk/8, so `half_us` can stay 0.

    `setup()` selects and clocks the project through the board's SDK
    (`ttboard`) when it is present: enable the design, hold reset, start the
    project clock, release reset, with the SPI pins parked (CS_n high) all
    the while. Without the SDK it only configures the four pins; select and
    clock the project by other means first, and reset it only while no
    transaction is in progress (the constructor leaves CS_n high).

    NOT yet run on hardware.
    """

    BOARDS = {"dbv3": {"sck": 17, "mosi": 18, "csn": 19, "miso": 33},
              "tt04": {"sck": 9, "mosi": 10, "csn": 11, "miso": 5}}

    def __init__(self, board="dbv3", pins=None, half_us=0, clock_hz=50000000):
        import machine
        import time
        p = dict(self.BOARDS[board])
        if pins:
            p.update(pins)
        self._time, self.half_us, self.clock_hz = time, half_us, clock_hz
        self._sck = machine.Pin(p["sck"], machine.Pin.OUT, value=0)
        self._mosi = machine.Pin(p["mosi"], machine.Pin.OUT, value=0)
        self._csn = machine.Pin(p["csn"], machine.Pin.OUT, value=1)
        self._miso = machine.Pin(p["miso"], machine.Pin.IN)
        self.spi = BitBangSPI(self._sck.value, self._mosi.value, self._csn.value, self._miso.value, self._half)
        self._tt = None

    def setup(self, project="tt_um_ahan17x_keyer"):
        """Select, clock and reset the project via the demo board SDK."""
        from ttboard.demoboard import DemoBoard
        tt = DemoBoard.get()
        self.spi.park()
        self.spi.in_reset = True
        getattr(tt.shuttle, project).enable()
        tt.reset_project(True)
        tt.clock_project_PWM(self.clock_hz)
        self._guard()
        tt.reset_project(False)
        self._guard()
        self.spi.in_reset = False
        self._tt = tt
        return tt

    def _guard(self):
        self._time.sleep_us(max(1, (RESET_GUARD * 1000000) // self.clock_hz))

    async def reset(self):
        """Hard reset through the SDK with the SPI pins parked."""
        if self._tt is None:
            raise KeyerError("no board SDK: call setup() first, or reset the project with CS_n high")
        self.spi.park()
        self.spi.in_reset = True
        self._guard()
        self._tt.reset_project(True)
        self._guard()
        self._tt.reset_project(False)
        self._guard()
        self.spi.in_reset = False

    async def _half(self):
        if self.half_us:
            self._time.sleep_us(self.half_us)

    async def xfer(self, cmd, data):
        return await self.spi.xfer(cmd, data)

    async def idle(self, cycles):
        self._time.sleep_us(max(1, (cycles * 1000000) // self.clock_hz))


# ---- the driver ----------------------------------------------------------------

class KeyerHost:
    """Everything a host does with the chip. `transport` provides
    `xfer(cmd, data)`, `idle(cycles)` and `reset()`. All methods are coroutines."""

    def __init__(self, transport, poll_idle=200, poll_limit=2000):
        self.t = transport
        self.poll_idle = poll_idle        # core clocks between polls while waiting
        self.poll_limit = poll_limit      # polls before a wait gives up

    # -- raw register access
    async def write(self, reg, data):
        await self.t.xfer(0x80 | (reg & 0x7F), [b & 0xFF for b in data])

    async def read(self, reg, n):
        return await self.t.xfer(reg & 0x7F, [0] * n)

    async def reset(self):
        """Hard reset of the chip. The transport keeps CS_n high across it
        (SEMANTICS 10.1) and raises KeyerError inside a transaction."""
        await self.t.reset()

    # -- identity, control, status
    async def id(self):
        """(id byte, ISA version); the id byte is 0x4B ('K')."""
        b = await self.read(R_ID, 2)
        return b[0], b[1]

    async def run(self, mask):
        """Set the RUN bits: bit 0 thread 0, bit 1 thread 1 (a 0 stops)."""
        await self.write(R_CTRL, [mask & 3])

    async def stop(self):
        await self.run(0)

    async def soft_reset(self, mask):
        """Soft-reset the threads in mask (timer, flags, LR, FIFOs; PC and
        registers stay), leaving the RUN bits as they are."""
        run = (await self.read(R_CTRL, 1))[0] & 3
        await self.write(R_CTRL, [((mask & 3) << 2) | run])

    async def status(self):
        s = (await self.read(R_STAT, 1))[0]
        return {"running": [s & 1, (s >> 1) & 1], "halted": [(s >> 2) & 1, (s >> 3) & 1],
                "blocked": [(s >> 4) & 1, (s >> 5) & 1], "raw": s}

    async def wait_halted(self, tid):
        """Poll until thread tid has halted."""
        for _ in range(self.poll_limit):
            if (await self.status())["halted"][tid]:
                return
            await self.t.idle(self.poll_idle)
        raise KeyerTimeout("thread %d did not halt" % tid)

    # -- program memory and PCs (both threads must be stopped for memory access)
    async def load_program(self, words, base=0):
        await self.write(R_IMEM_ADDR, [base & 0xFF, 0])
        data = []
        for w in words:
            data.append(w & 0xFF)
            data.append((w >> 8) & 0xFF)
        await self.write(R_IMEM_DATA, data)

    async def read_program(self, base, n):
        await self.write(R_IMEM_ADDR, [base & 0xFF, 0])
        rb = await self.read(R_IMEM_DATA, 2 * n)
        return [rb[2 * i] | (rb[2 * i + 1] << 8) for i in range(n)]

    async def set_pc(self, tid, pc):
        """Accepted only while thread tid is stopped."""
        await self.write(R_PC0 + tid, [pc & 0xFF, 0])

    async def get_pc(self, tid):
        return (await self.read(R_PC0 + tid, 2))[0]

    # -- FIFOs, with level checks
    async def levels(self):
        """[inbox0, outbox0, inbox1, outbox1] occupancies (0..16)."""
        return await self.read(R_LEVELS, 4)

    async def push(self, tid, data):
        """Push bytes into thread tid's inbox. The inbox is 16 deep and the
        chip drops writes to a full inbox, so each transaction carries no
        more than the free space; waits (with polling) for the thread to make
        room. Returns the number of bytes pushed."""
        data = [b & 0xFF for b in data]
        i, polls = 0, 0
        while i < len(data):
            free = FIFO_DEPTH - (await self.levels())[2 * tid]
            if free <= 0:
                polls += 1
                if polls > self.poll_limit:
                    raise KeyerTimeout("inbox %d stayed full (%d of %d bytes pushed)" % (tid, i, len(data)))
                await self.t.idle(self.poll_idle)
                continue
            n = min(free, len(data) - i)
            await self.write(R_INBOX0 + 2 * tid, data[i:i + n])
            i += n
            polls = 0
        return i

    async def pop(self, tid, n=None, wait=False):
        """Pop bytes from thread tid's outbox, never more than it holds (a
        read of an empty outbox returns a stale byte). n=None: whatever is
        there now. wait=True: poll until n bytes have arrived."""
        out, polls = [], 0
        while True:
            avail = (await self.levels())[1 + 2 * tid]
            want = avail if n is None else min(avail, n - len(out))
            if want:
                out += await self.read(R_OUTBOX0 + 2 * tid, want)
                polls = 0
            if n is None or len(out) >= n or not wait:
                return out
            polls += 1
            if polls > self.poll_limit:
                raise KeyerTimeout("outbox %d: %d of %d bytes arrived" % (tid, len(out), n))
            await self.t.idle(self.poll_idle)

    async def fifo_clear(self, mask):
        """bit 0 inbox 0, bit 1 outbox 0, bit 2 inbox 1, bit 3 outbox 1."""
        await self.write(R_FIFOCLR, [mask & 0xF])

    # -- pins
    async def pins(self):
        """(uio levels, ui levels, firmware uo outputs), synchronised."""
        b = await self.read(R_PINS, 3)
        return b[0], b[1], b[2]

    async def pinout(self):
        """(uio_out, uio_oe): what the chip drives on the bidirectional pins."""
        b = await self.read(R_PINOUT, 2)
        return b[0], b[1]

    async def pinmode(self, od_mask=None):
        """Set (or just read) the open-drain mask of uio[7:0]."""
        if od_mask is not None:
            await self.write(R_PINMODE, [od_mask & 0xFF])
        return (await self.read(R_PINMODE, 1))[0]

    async def irq_enable(self, mask):
        await self.write(R_IRQEN, [mask & 0xFF])

    # -- capture and replay (docs/CAPTURE.md)
    async def capture_config(self, group, mask, trigger_pattern=0, trigger_mask=0, base=0x80, length=64):
        """Watch pins 4*group .. 4*group+3 under `mask`; trigger when the
        group matches `trigger_pattern` under `trigger_mask` after not
        matching (mask 0: at once); record into program memory words
        base .. base+length-1."""
        await self.write(R_CAP_CFG, [(group & 7) | ((mask & 0xF) << 4),
                                     (trigger_pattern & 0xF) | ((trigger_mask & 0xF) << 4)])
        await self.write(R_CAP_BUF, [base & 0xFF, length & 0xFF])

    async def capture_arm(self):
        await self.write(R_CR_CTRL, [CR_ARM])

    async def capture_disarm(self):
        await self.write(R_CR_CTRL, [CR_DISARM])

    async def cr_status(self):
        s = (await self.read(R_CR_CTRL, 1))[0]
        return {"capture_active": s & 1, "triggered": (s >> 1) & 1, "capture_done": (s >> 2) & 1,
                "overflow": (s >> 3) & 1, "replay_active": (s >> 4) & 1, "replay_done": (s >> 5) & 1,
                "underrun": (s >> 6) & 1, "raw": s}

    async def cr_counts(self):
        """(entries recorded, entries applied)."""
        b = await self.read(R_CR_COUNT, 2)
        return b[0], b[1]

    async def wait_capture_done(self):
        for _ in range(self.poll_limit):
            if (await self.cr_status())["capture_done"]:
                return
            await self.t.idle(self.poll_idle)
        raise KeyerTimeout("capture did not finish")

    async def wait_replay_done(self):
        for _ in range(self.poll_limit):
            if (await self.cr_status())["replay_done"]:
                return
            await self.t.idle(self.poll_idle)
        raise KeyerTimeout("replay did not finish")

    async def capture_read(self, base=None):
        """Read the recorded entries as (delta, pins) pairs (SEMANTICS 14.4:
        the first delta is 0; an idle entry repeats the previous pins with
        delta 4095). Both threads must be stopped (the program memory port).
        `base` defaults to the configured capture base; the number of
        entries is CR_COUNT's count of entries written."""
        if (await self.read(R_CTRL, 1))[0] & 3:
            raise KeyerError("stop both threads before reading the capture buffer")
        n = (await self.cr_counts())[0]
        if base is None:
            base = (await self.read(R_CAP_BUF, 2))[0]
        return [(w >> 4, w & 0xF) for w in await self.read_program(base, n)]

    capture_drain = capture_read            # the original name

    async def capture_settings(self):
        """(group, watch mask, trigger pattern, trigger mask, base, length)
        as configured (CAP_CFG and CAP_BUF read back)."""
        c = await self.read(R_CAP_CFG, 2)
        b = await self.read(R_CAP_BUF, 2)
        return c[0] & 7, c[0] >> 4, c[1] & 0xF, c[1] >> 4, b[0], b[1]

    async def replay_config(self, group, mask, base=0x80, length=0):
        """Drive pins 4*group .. 4*group+3 under `mask` from the entries at
        base; length 0 replays as many entries as the last capture recorded."""
        await self.write(R_REP_CFG, [(group & 7) | ((mask & 0xF) << 4)])
        await self.write(R_REP_BUF, [base & 0xFF, length & 0xFF])

    async def replay_load(self, entries, base=0x80):
        """Write a waveform: entries are (delta, pins), delta 0..4095 cycles
        since the previous entry (the first entry's delta is ignored)."""
        words = []
        for d, p in entries:
            if not 0 <= d <= 4095:
                raise KeyerError("delta %d out of range 0..4095 (use idle entries for longer gaps)" % d)
            words.append((d << 4) | (p & 0xF))
        await self.load_program(words, base=base)

    async def replay_start(self):
        await self.write(R_CR_CTRL, [CR_START])

    async def replay_stop(self):
        await self.write(R_CR_CTRL, [CR_STOP])


# ---- reading a capture: listing and protocol decoders (docs/CAPTURE.md section 5) ----
# Pure functions of the entries as KeyerHost.capture_read() returns them; no
# chip needed. The decoders import tools/protomodels*.py inside the functions,
# so the board can import this file without them (it forwards the entries to
# the PC, which decodes; see _pc_main).

IDLE_DELTA = 4095


def entries_from_wave(levels):
    """The entries the capture engine records for a waveform (SEMANTICS 14.3,
    14.4) with a buffer that never fills: `levels` are the masked group
    nibbles of consecutive cycles, levels[0] being the trigger cycle. The
    inverse of expand_capture()."""
    entries = []
    if not levels:
        return entries
    last = levels[0] & 0xF
    entries.append((0, last))
    d = 1                                   # cap_dt: cycles since the last entry
    for v in levels[1:]:
        v &= 0xF
        if v != last:
            entries.append((d, v))
            last, d = v, 1
        elif d == IDLE_DELTA:
            entries.append((IDLE_DELTA, last))
            d = 1
        else:
            d += 1
    return entries


def capture_cycles(entries):
    """The cycle of every entry, counted from the trigger (entry 0)."""
    out, c = [], 0
    for k, (d, _) in enumerate(entries):
        if k:
            c += max(d, 1)
        out.append(c)
    return out


def expand_capture(entries, group, tail=0):
    """Per-cycle 24-bit pad vectors from the trigger cycle (index 0) to the
    last entry, then `tail` more cycles of the last level: entry k holds
    from delta_k cycles after entry k-1 (a delta of 0 after the first entry,
    which the engine never records, counts as 1, as in replay). The nibble
    sits at pins 4*group .. 4*group+3, so decoders take real pin numbers."""
    shift = 4 * group
    out = []
    for k, (d, p) in enumerate(entries):
        if k:
            out.extend([out[-1]] * (max(d, 1) - 1))
        out.append((p & 0xF) << shift)
    if out and tail > 0:
        out.extend([out[-1]] * tail)
    return out


def _pin_label(group, bit, names):
    lab = "p%d" % (4 * group + bit)
    if names and bit in names:
        lab += "=" + names[bit]
    return lab


def capture_listing(entries, group, clock_hz=None, names=None, mask=0xF):
    """A timing listing: a header (group, entries, duration), then one line
    per entry: index, cycle since the trigger, time in us (with clock_hz),
    delta, the four bits under their pin numbers (and `names`, a dict
    bit -> label; bits outside the watch `mask` print as -), and what
    changed from the previous entry. Idle entries say "idle"."""
    cyc = capture_cycles(entries)
    total = cyc[-1] if cyc else 0
    head = "capture: group %d (pins %d-%d), %d entries, %d cycles" % (
        group, 4 * group, 4 * group + 3, len(entries), total)
    if clock_hz:
        head += " = %.3f us at %.6g MHz" % (total * 1e6 / clock_hz, clock_hz / 1e6)
    labels = [_pin_label(group, b, names) for b in (3, 2, 1, 0)]
    cols = "  idx    cycle " + ("        us " if clock_hz else "") + "delta  " + " ".join(labels) + "  change"
    lines = [head, cols]
    prev = None
    for k, (d, p) in enumerate(entries):
        s = "%5d %8d " % (k, cyc[k])
        if clock_hz:
            s += "%10.3f " % (cyc[k] * 1e6 / clock_hz)
        s += "%5d  " % d
        bits = []
        for lab, b in zip(labels, (3, 2, 1, 0)):
            v = str((p >> b) & 1) if (mask >> b) & 1 else "-"
            bits.append(" " * (len(lab) - len(v)) + v)       # (MicroPython has no rjust)
        s += " ".join(bits) + "  "
        if prev is None:
            s += "trigger"
        elif d == IDLE_DELTA and p == prev:
            s += "idle"
        else:
            ch = []
            for b in (3, 2, 1, 0):
                if ((p ^ prev) >> b) & 1:
                    nm = names[b] if names and b in names else "p%d" % (4 * group + b)
                    ch.append("%s %s" % (nm, "rise" if (p >> b) & 1 else "fall"))
            s += ", ".join(ch)
        lines.append(s.rstrip())
        prev = p
    return "\n".join(lines)


class _Probe:
    """What an on_cycle() decoder needs from a machine: pad(), cycle, and
    ext_ui / ext_uio (writes are ignored: nothing is driven back)."""

    def __init__(self):
        self.cycle, self.v, self.ext_ui, self.ext_uio = 0, 0, 0, 0xFF

    def pad(self):
        return self.v


def _feed(models, wave, initial):
    m = _Probe()
    if initial is not None:
        m.cycle, m.v = -1, initial
        for mod in models:
            mod.on_cycle(m)
    for c, v in enumerate(wave):
        m.cycle, m.v = c, v
        for mod in models:
            mod.on_cycle(m)


# per protocol: the bit parameters (bit inside the nibble) with their defaults,
# the other parameters, and the level of the group before the trigger that the
# decoder assumes unless `initial` is given (a function of the bit parameters)
DECODERS = {
    "uart": ({"bit": 2}, ("period", "baud"), lambda b: 1 << b["bit"]),
    "spi": ({"sck": 0, "mosi": 1, "csn": 2, "miso": None}, (), lambda b: 1 << b["csn"]),
    "i2c": ({"scl": 2, "sda": 3}, (), lambda b: (1 << b["scl"]) | (1 << b["sda"])),
    "usb": ({"dp": 0, "dm": 1}, ("bit_cycles",), lambda b: 1 << b["dm"]),
    "manchester": ({"p": 0, "n": 1}, ("clk_ns",), lambda b: 0),
}


def _pad(text, width):
    return text + " " * (width - len(text))


def _at(c, clock_hz):
    if clock_hz:
        return "@%d (%.3f us)" % (c, c * 1e6 / clock_hz)
    return "@%d" % c


def decode_capture(entries, group, proto, clock_hz=None, initial=None, tail=None, **params):
    """Decode a capture of one pin group. Returns (result, text): result is a
    dict ("proto", "errors" and per-protocol records), text a rendering.

    Bit parameters are positions inside the group's nibble (0..3); periods
    are in core cycles. `initial` is the nibble in the cycle before the
    trigger (the capture starts at the trigger, so a decoder that needs an
    edge to start, such as a START condition or a CS_n fall, would miss the
    first one); by default the protocol's idle level. `tail` is how many
    cycles the last level is held after the last entry: the entries do not
    say how long it lasted (the capture ended some time later), so the
    default is per protocol, enough to finish a final UART character (11
    bit periods) or USB packet (2 bit times), 1 cycle otherwise.

      uart        bit (2), period or baud (needs clock_hz): 8N1 bytes
      spi         sck (0), mosi (1), csn (2), miso (none): mode 0 frames
      i2c         scl (2), sda (3): transfers with ACK bits
      usb         dp (0), dm (1), bit_cycles (32): low-speed packets
      manchester  p (0), n (1), clk_ns (25.0): 10BASE-T link pulses, frames
    """
    if proto not in DECODERS:
        raise KeyerError("unknown protocol %r (one of %s)" % (proto, ", ".join(sorted(DECODERS))))
    bitdefs, others, idle = DECODERS[proto]
    bits = {}
    for k, v in bitdefs.items():
        bits[k] = params.pop(k, v)
    for k in params:
        if k not in others:
            raise KeyerError("%s: unknown parameter %r (bits %s; other %s)" % (
                proto, k, ", ".join(sorted(bitdefs)), ", ".join(others) or "none"))
    for k, v in bits.items():
        if v is not None and not (isinstance(v, int) and 0 <= v <= 3):
            raise KeyerError("%s: %s = %r is not a bit of the nibble (0..3)" % (proto, k, v))
    pin = dict((k, None if v is None else 4 * group + v) for k, v in bits.items())
    if initial is None:
        initial = idle(bits)
    init_pad = (initial & 0xF) << (4 * group)
    where = ", ".join("%s bit %d = pin %d" % ("line" if k == "bit" else k, bits[k], pin[k])
                      for k in bitdefs if bits[k] is not None)
    if proto == "uart":
        return _decode_uart(entries, group, pin, where, init_pad, tail, clock_hz, params)
    if proto == "spi":
        return _decode_spi(entries, group, pin, where, init_pad, tail, clock_hz)
    if proto == "i2c":
        return _decode_i2c(entries, group, pin, where, init_pad, tail, clock_hz)
    if proto == "usb":
        return _decode_usb(entries, group, bits, where, tail, clock_hz, params)
    return _decode_manchester(entries, group, pin, where, init_pad, tail, clock_hz, params)


def _errors_text(errors, clock_hz):
    out = []
    for e in errors:
        kind, cyc, detail = (tuple(e) + (None, None))[:3]
        s = "  error: %s" % kind
        if isinstance(cyc, (int, float)):
            s += " " + _at(int(cyc), clock_hz)
        if detail is not None:
            s += " (%s)" % (detail,)
        out.append(s)
    return out


def _decode_uart(entries, group, pin, where, init_pad, tail, clock_hz, params):
    import protomodels as pm
    if "period" in params:
        period = params["period"]
    elif "baud" in params and clock_hz:
        period = float(clock_hz) / params["baud"]
    else:
        raise KeyerError("uart: give period (cycles per bit), or baud and the clock")
    dec = pm.UartDecoder(pin["bit"], period)
    # the last level held for a whole character: a byte whose last edge is
    # inside it (0xF0: bit 4 rises, then nothing) still reaches its stop bit
    wave = expand_capture(entries, group, int(11 * period) + 2 if tail is None else tail)
    _feed([dec], wave, init_pad)
    res = {"proto": "uart", "bytes": list(dec.bytes), "starts": list(dec.starts),
           "errors": list(dec.errors), "period": period}
    lines = ["uart (%s; %s cycles per bit, 8N1): %d bytes, %d errors" % (
        where, ("%.2f" % period).rstrip("0").rstrip("."), len(dec.bytes), len(dec.errors))]
    for c, b in zip(dec.starts, dec.bytes):
        lines.append("  %s %02X %s" % (_at(c, clock_hz), b, repr(chr(b)) if 32 <= b < 127 else ""))
    lines += _errors_text(dec.errors, clock_hz)
    return res, "\n".join(s.rstrip() for s in lines)


def _decode_spi(entries, group, pin, where, init_pad, tail, clock_hz):
    import protomodels as pm
    wave = expand_capture(entries, group, 1 if tail is None else tail)
    mosi = pm.SpiSlaveModel(pin["sck"], pin["mosi"], pin["csn"], 0, [])
    models = [mosi]
    miso = None
    if pin["miso"] is not None:
        miso = pm.SpiSlaveModel(pin["sck"], pin["miso"], pin["csn"], 0, [])   # mode 0: sampled alike
        models.append(miso)
    _feed(models, wave, init_pad)
    starts, prev = [], (init_pad >> pin["csn"]) & 1
    for c, v in enumerate(wave):
        cs = (v >> pin["csn"]) & 1
        if prev == 1 and cs == 0:
            starts.append(c)
        prev = cs
    res = {"proto": "spi", "mosi": [list(f) for f in mosi.frames],
           "miso": [list(f) for f in miso.frames] if miso else None,
           "starts": starts, "errors": list(mosi.errors)}
    lines = ["spi (%s; mode 0): %d frames, %d errors" % (where, len(mosi.frames), len(mosi.errors))]
    for i, f in enumerate(mosi.frames):
        s = "  %s MOSI %s" % (_at(starts[i], clock_hz) if i < len(starts) else "", " ".join("%02X" % b for b in f))
        if miso:
            s += " | MISO " + " ".join("%02X" % b for b in miso.frames[i])
        lines.append(s)
    lines += _errors_text(mosi.errors, clock_hz)
    return res, "\n".join(s.rstrip() for s in lines)


def _decode_i2c(entries, group, pin, where, init_pad, tail, clock_hz):
    import protomodels as pm
    dec = pm.I2cDecoder(pin["scl"], pin["sda"])
    _feed([dec], expand_capture(entries, group, 1 if tail is None else tail), init_pad)
    dec.finish()
    res = {"proto": "i2c", "transfers": list(dec.transfers), "events": list(dec.events),
           "errors": list(dec.errors)}
    lines = ["i2c (%s): %d transfers, %d errors" % (where, len(dec.transfers), len(dec.errors))]
    w = 22 if clock_hz else 8
    k = 0
    for kind, c, _ in dec.events:                # in bus order: a line per START and per STOP
        if kind == "stop":
            lines.append("  %s STOP" % _pad(_at(c, clock_hz), w))
        elif kind in ("start", "restart"):
            _, addr, rw, ack, data, _ = dec.transfers[k]
            k += 1
            s = "  %s %s" % (_pad(_at(c, clock_hz), w), _pad(kind.upper(), 7))
            if addr is not None:
                s += " 0x%02X %s %s" % (addr, "R" if rw else "W", "NACK" if ack else "ACK")
                if data:
                    s += " : " + ", ".join("%02X %s" % (b, "NACK" if a else "ACK") for b, a in data)
            lines.append(s)
    lines += _errors_text(dec.errors, clock_hz)
    return res, "\n".join(s.rstrip() for s in lines)


def _decode_usb(entries, group, bits, where, tail, clock_hz, params):
    import protomodels_usb_host as uh
    T = params.get("bit_cycles", 32)
    names = {(0, 1): uh.J, (1, 0): uh.K, (0, 0): uh.SE0, (1, 1): uh.SE1}

    def state(nib):
        return names[((nib >> bits["dp"]) & 1, (nib >> bits["dm"]) & 1)]

    # runs [state, first cycle, length] of the line state; the last level is
    # held for `tail` cycles (default two bit times, so a final EOP ends in J)
    cyc = capture_cycles(entries)
    end = (cyc[-1] if cyc else 0) + (2 * T if tail is None else tail)
    runs = []
    for (d, p), c in zip(entries, cyc):
        s = state(p)
        if runs and runs[-1][0] == s:
            continue
        if runs:
            runs[-1][2] = c - runs[-1][1]
        runs.append([s, c, 0])
    if runs:
        runs[-1][2] = end - runs[-1][1] + 1
    host = uh.UsbLsHost(bit_cycles=T, dp=0, dm=1, seed=None, keepalive_bits=None)
    host_pids = ("OUT", "IN", "SOF", "SETUP", "PRE")
    packets, events, errors = [], [], []
    i = 0
    while i < len(runs):
        s, c, n = runs[i]
        if s == uh.SE0:                         # not the end of a packet (those are taken below)
            events.append(("keep-alive" if n <= 3 * T else "reset", c, n))
        if s != uh.K:
            i += 1
            continue
        j = i
        while j < len(runs) and runs[j][0] != uh.SE0:
            j += 1
        prun = [list(r) for r in runs[i:j + 1]]
        after = runs[j + 1][0] if j + 1 < len(runs) else None     # None: the capture ends inside it
        pkt = uh.Packet(c)
        host.cycle = c                          # errors are reported at the packet's start
        n0 = len(host.errors)
        # Offline there is no request to answer: the reference is placed four
        # bit times before the packet, inside the 2..6.5 window, so the
        # "response early/late" check cannot fire; any such error and the
        # delay are dropped from the result. Host packets (tokens, PRE) are
        # legal on a bus capture: _analyse's "a device does not send" error
        # is dropped for them and their CRC-5 checked here instead.
        host._analyse(pkt, prun, after, c - 4 * T)
        name = pkt.name
        perr = []
        for kind, ec, detail in host.errors[n0:]:
            if kind in ("response early", "response late"):
                continue
            if kind == "pid" and name in host_pids and str(detail).startswith("a device does not send"):
                continue
            perr.append((kind, ec, detail))
        rec = {"cycle": c, "pid": name, "bytes": list(pkt.bytes), "crc": None, "errors": perr}
        if name in ("OUT", "IN", "SETUP", "SOF"):
            if len(pkt.bytes) != 3:
                perr.append(("length", c, "token of %d bytes" % len(pkt.bytes)))
            else:
                v = pkt.bytes[1] | pkt.bytes[2] << 8
                ok = uh.crc5([(v >> k) & 1 for k in range(11)]) == v >> 11
                rec["crc"] = "ok" if ok else "bad"
                if not ok:
                    perr.append(("crc5", c, "CRC-5 0x%02X" % (v >> 11)))
                if name == "SOF":
                    rec["frame"] = v & 0x7FF
                else:
                    rec["addr"], rec["ep"] = v & 0x7F, (v >> 7) & 0xF
        elif name in ("DATA0", "DATA1") and len(pkt.bytes) >= 3:
            rec["payload"] = list(pkt.payload)
            rec["crc"] = "bad" if any(e[0] == "crc16" for e in perr) else "ok"
        packets.append(rec)
        errors += perr
        i = j + 1
    res = {"proto": "usb", "packets": packets, "events": events, "errors": errors}
    lines = ["usb low speed (%s; %d cycles per bit; J = D+ 0, D- 1): %d packets, %d errors" % (
        where, T, len(packets), len(errors))]
    w = 22 if clock_hz else 8
    for kind, c, n in events:
        lines.append("  %s %s (SE0 for %d cycles)" % (_pad(_at(c, clock_hz), w), kind, n))
    for p in packets:
        s = "  %s %s" % (_pad(_at(p["cycle"], clock_hz), w), _pad(p["pid"], 6))
        if "addr" in p:
            s += " addr %d ep %d" % (p["addr"], p["ep"])
        if "frame" in p:
            s += " frame %d" % p["frame"]
        if "payload" in p:
            s += " " + (" ".join("%02X" % b for b in p["payload"]) or "(empty)")
        if p["crc"]:
            s += "  crc %s" % p["crc"]
        lines.append(s)
        lines += _errors_text(p["errors"], clock_hz)
    return res, "\n".join(s.rstrip() for s in lines)


def _decode_manchester(entries, group, pin, where, init_pad, tail, clock_hz, params):
    import protomodels_eth_10bt as eth
    clk_ns = float(params.get("clk_ns", 25.0))
    rx = eth.Eth10BTReceiver(tx_p=pin["p"], tx_n=pin["n"], clk_ns=clk_ns)
    wave = expand_capture(entries, group, 1 if tail is None else tail)
    pp, pn = pin["p"], pin["n"]
    rx.sample(-1, (init_pad >> pp) & 1, (init_pad >> pn) & 1)
    for c, v in enumerate(wave):
        rx.sample(c, (v >> pp) & 1, (v >> pn) & 1)
    rx.finish()
    frames = []
    for f in rx.frames:
        frames.append({"start_ns": f.start, "bits": len(f.bits), "bytes": len(f.bytes),
                       "dst": f.dst, "src": f.src, "type": f.type,
                       "payload_len": len(f.payload) if f.dst is not None else None,
                       "fcs_ok": f.fcs_ok, "errors": list(f.errors)})
    res = {"proto": "manchester", "pulses": list(rx.pulses), "frames": frames, "errors": list(rx.errors)}
    lines = ["10base-t (%s; %.6g ns per cycle): %d link pulses, %d frames, %d errors" % (
        where, clk_ns, len(rx.pulses), len(frames), len(rx.errors))]
    for start, width, level in rx.pulses:
        lines.append("  @%.3f us  link pulse %s, %.0f ns" % (start / 1000.0, "+" if level > 0 else "-", width))
    for f in frames:
        s = "  @%.3f us  frame, %d bits" % (f["start_ns"] / 1000.0, f["bits"])
        if f["dst"] is not None:
            s += ": dst %s src %s type 0x%04X, %d payload bytes, FCS %s" % (
                ":".join("%02X" % b for b in f["dst"]), ":".join("%02X" % b for b in f["src"]),
                f["type"], f["payload_len"], "ok" if f["fcs_ok"] else "bad")
        lines.append(s)
    for kind, t, detail in rx.errors:
        lines.append("  error: %s @%.3f us%s" % (kind, t / 1000.0, " (%s)" % detail if detail else ""))
    return res, "\n".join(s.rstrip() for s in lines)


# ---- self-test bodies: run unchanged on the board and in simulation --------------
# Each takes a KeyerHost and needs nothing connected to the chip's pins.

# loop: pop r0 / inc r0 / push r0 / bra loop   (tools/test_keyerhost.py checks
# these words against the assembler)
ECHO_PROGRAM = [0xE020, 0x10C0, 0xE000, 0x90FC]


async def selftest_id(host):
    ident, version = await host.id()
    assert ident == ID_BYTE, "ID byte 0x%02X, expected 0x4B" % ident
    await host.stop()
    st = await host.status()
    assert st["running"] == [0, 0], st
    return version


async def selftest_program_memory(host):
    await host.stop()
    words = [(i * 0x1357 + 0x2468) & 0xFFFF for i in range(256)]
    await host.load_program(words)
    got = await host.read_program(0, 256)
    assert got == words, "program memory read-back differs at %s" % [i for i in range(256) if got[i] != words[i]][:8]
    await host.set_pc(0, 0x12)
    await host.set_pc(1, 0x34)
    assert await host.get_pc(0) == 0x12 and await host.get_pc(1) == 0x34


async def selftest_fifo_echo(host, count=40):
    """Thread 0 echoes inbox bytes + 1 to its outbox. Sends more than the
    FIFO depth so that the level checks do the work."""
    await host.stop()
    await host.soft_reset(0b11)
    await host.load_program(ECHO_PROGRAM + [0] * 4)
    await host.set_pc(0, 0)
    await host.run(0b01)
    data = [(7 * i + 3) & 0xFF for i in range(count)]
    got = []
    for i in range(0, count, 12):
        chunk = data[i:i + 12]
        assert await host.push(0, chunk) == len(chunk)
        got += await host.pop(0, len(chunk), wait=True)
    assert got == [(b + 1) & 0xFF for b in data], got
    st = await host.status()
    assert st["running"][0] and st["blocked"][0], st       # waiting in POP
    assert await host.levels() == [0, 0, 0, 0]
    await host.stop()


async def selftest_capture_replay(host):
    """Replay a host-written waveform on uo2/uo3 (pin group 4) while
    capturing the same group: the uo pins read back what is driven, so no
    wiring is needed. The capture must reproduce the deltas."""
    await host.stop()
    wave = [(0, 0x4), (5, 0xC), (3, 0x8), (9, 0x0), (40, 0x4), (2, 0xC), (7, 0x0)]
    await host.replay_load(wave, base=0x40)
    await host.replay_config(group=4, mask=0xC, base=0x40, length=len(wave))
    await host.capture_config(group=4, mask=0xC, trigger_pattern=0x4, trigger_mask=0xC, base=0x80, length=len(wave))
    await host.capture_arm()
    st = await host.cr_status()
    assert st["capture_active"] and not st["triggered"], st
    await host.replay_start()
    await host.wait_replay_done()
    await host.wait_capture_done()
    st = await host.cr_status()
    assert not st["overflow"] and not st["underrun"], st
    assert await host.cr_counts() == (len(wave), len(wave))
    got = await host.capture_drain()
    assert got == [(0, 0x4)] + wave[1:], got
    assert (await host.pins())[2] == 0x00                 # the waveform ends with both pins low


async def selftest_all(host):
    version = await selftest_id(host)
    await selftest_program_memory(host)
    await selftest_fifo_echo(host)
    await selftest_capture_replay(host)
    return version


# ---- command line -------------------------------------------------------------------

USAGE = """keyerhost commands (run on the demo board under MicroPython, or from a
PC with --port to forward them to the board through mpremote). Words, bytes,
addresses and masks are hex; thread numbers, counts and every --option value
are decimal:
  id                         identity byte and ISA version
  status                     running / halted / blocked per thread, FIFO levels
  reset                      hard reset of the chip (SPI pins parked)
  load WORD WORD ...         load program words (hex) at address 0
                             [PC: load FILE.s [-D NAME=VALUE ...] | load FILE.hex]
  pc TID ADDR                set a thread's PC (thread stopped)
  run MASK | stop            start threads (bit 0, bit 1) / stop both
  push TID BYTE ...          push bytes (hex) into a thread's inbox
  pop TID [N]                pop N bytes (default: all available) from its outbox
  pins                       uio, ui and uo levels, and the uio drive state
  capture GROUP MASK TPAT TMASK BASE LEN    configure and arm a capture
  capture status             capture/replay status bits and counts
  capture disarm             stop recording (the entries so far stay)
  capture read [BASE]        print the recorded entries, one "delta pins" line
                             each, after a "# capture group G mask M" line
                             (threads stopped)   [PC: --save F also writes them to F]
  capture listing [BASE] [--clock HZ] [--names 2=SCL,3=SDA]
                             timing listing of the recorded entries
  capture decode PROTO [BASE] [--clock HZ] [--KEY VALUE ...]
                             decode the entries; PROTO and its keys (bit
                             numbers 0-3 inside the group, periods in cycles):
                               uart --bit 2 --period N (or --baud B with --clock)
                               spi  --sck 0 --mosi 1 --csn 2 [--miso 3]
                               i2c  --scl 2 --sda 3
                               usb  --dp 0 --dm 1 --bit_cycles 32
                               manchester --p 0 --n 1 --clk_ns 25.0
                             also --initial V (group level before the trigger)
                             and --tail N (cycles the last level is held)
                             [listing and decode, PC: run on the PC; --file F
                             decodes a saved "capture read" instead of the board]
  capture ... --group G      the pin group, if not the one in CAP_CFG
  drain                      print the recorded entries (delta pins)
  replay GROUP MASK BASE LEN start a replay (LEN 0: the last capture's length)
  selftest                   run the built-in self-tests
"""


def _options(args):
    """Split [positional ...] [--key value ...]; values are decimal numbers
    (an int if possible, else a float) or kept as text."""
    pos, opts, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            if i + 1 >= len(args):
                raise KeyerError("option %s needs a value" % a)
            v = args[i + 1]
            try:
                v = int(v)
            except ValueError:
                try:
                    v = float(v)
                except ValueError:
                    pass
            opts[a[2:]] = v
            i += 2
        else:
            pos.append(a)
            i += 1
    return pos, opts


def format_capture(entries, group, mask):
    """The text of `capture read`: a header line, then one "delta pins" line
    per entry (parse_capture() reads it back)."""
    lines = ["# capture group %d mask %X entries %d" % (group, mask, len(entries))]
    return "\n".join(lines + ["%4d %X" % e for e in entries])


def parse_capture(text):
    """(entries, group, mask) from the text of `capture read` (or of
    `drain`, which has no header: group and mask are then None)."""
    entries, group, mask = [], None, None
    for line in text.splitlines():
        f = line.split()
        if not f:
            continue
        if f[0] == "#":
            if "group" in f:
                group = int(f[f.index("group") + 1])
            if "mask" in f:
                mask = int(f[f.index("mask") + 1], 16)
            continue
        if len(f) != 2:
            raise KeyerError("not a capture entry: %r" % line)
        entries.append((int(f[0]), int(f[1], 16)))
    return entries, group, mask


def capture_report(sub, args, entries, group, mask=0xF):
    """`capture listing ...` or `capture decode PROTO ...` on entries already
    read (the chip is not touched): returns the text. `args` are the words
    after the subcommand; positional words (BASE) are ignored here."""
    pos, opts = _options(args)
    if "group" in opts:
        group = opts.pop("group")
    clock = opts.pop("clock", None)
    if sub == "listing":
        names = None
        if "names" in opts:
            names = {}
            for item in str(opts.pop("names")).split(","):
                b, _, n = item.partition("=")
                names[int(b)] = n
        if opts:
            raise KeyerError("capture listing: unknown option(s) %s" % ", ".join("--" + k for k in opts))
        return capture_listing(entries, group, clock_hz=clock, names=names, mask=mask)
    if not pos:
        raise KeyerError("capture decode PROTO: one of %s" % ", ".join(sorted(DECODERS)))
    proto = pos[0]
    warn = []
    if proto in DECODERS:
        for k, v in DECODERS[proto][0].items():
            b = opts.get(k, v)
            if isinstance(b, int) and 0 <= b <= 3 and not (mask >> b) & 1:
                warn.append("warning: %s (bit %d) is outside the watch mask %X: it reads 0" % (k, b, mask))
    _, text = decode_capture(entries, group, proto, clock_hz=clock, **opts)
    return "\n".join(warn + [text])


async def _capture_command(host, args):
    sub = args[0]
    if sub == "status":
        st = await host.cr_status()
        n, k = await host.cr_counts()
        return "capture %s%s%s%s | replay %s%s%s | recorded %d applied %d" % (
            "active" if st["capture_active"] else "idle", " triggered" if st["triggered"] else "",
            " done" if st["capture_done"] else "", " OVERFLOW" if st["overflow"] else "",
            "active" if st["replay_active"] else "idle", " done" if st["replay_done"] else "",
            " UNDERRUN" if st["underrun"] else "", n, k)
    if sub == "disarm":
        await host.capture_disarm()
        return "disarmed"
    pos, opts = _options(args[1:])
    if sub == "decode":
        pos = pos[1:]
    base = int(pos[0], 16) if pos else None
    group, mask = (await host.capture_settings())[:2]
    if "group" in opts:
        group = opts["group"]
    entries = await host.capture_read(base)
    if sub == "read":
        return format_capture(entries, group, mask)
    return capture_report(sub, args[1:], entries, group, mask)


async def command(host, argv):
    """Run one command; returns the text to print."""
    def num(s):
        return int(s, 16)
    cmd, args = argv[0], argv[1:]
    if cmd == "id":
        return "id 0x%02X version %d" % await host.id()
    if cmd == "status":
        st = await host.status()
        return "running %s halted %s blocked %s levels %s" % (st["running"], st["halted"], st["blocked"], await host.levels())
    if cmd == "reset":
        await host.reset()
        return "reset"
    if cmd == "load":
        await host.stop()
        await host.load_program([num(a) for a in args])
        return "loaded %d words" % len(args)
    if cmd == "pc":
        await host.set_pc(int(args[0]), num(args[1]))
        return "pc%d = 0x%02X" % (int(args[0]), await host.get_pc(int(args[0])))
    if cmd == "run":
        await host.run(num(args[0]))
        return "running %s" % (await host.status())["running"]
    if cmd == "stop":
        await host.stop()
        return "stopped"
    if cmd == "push":
        return "pushed %d" % await host.push(int(args[0]), [num(a) for a in args[1:]])
    if cmd == "pop":
        got = await host.pop(int(args[0]), int(args[1]) if len(args) > 1 else None)
        return " ".join("%02X" % b for b in got)
    if cmd == "pins":
        return "uio %02X ui %02X uo %02X | uio_out %02X uio_oe %02X" % (await host.pins() + await host.pinout())
    if cmd == "capture" and args and args[0] in ("read", "listing", "decode", "status", "disarm"):
        return await _capture_command(host, args)
    if cmd == "capture":
        g, m, tp, tm, base, ln = [num(a) for a in args]
        await host.capture_config(g, m, tp, tm, base, ln)
        await host.capture_arm()
        return "armed"
    if cmd == "drain":
        return "\n".join("%4d %X" % e for e in await host.capture_drain())
    if cmd == "replay":
        g, m, base, ln = [num(a) for a in args]
        await host.replay_config(g, m, base, ln)
        await host.replay_start()
        return "replay started"
    if cmd == "selftest":
        return "selftest passed, ISA version %d" % await selftest_all(host)
    raise KeyerError("unknown command %r\n%s" % (cmd, USAGE))


_board = None       # the board transport of this interpreter session


def main(argv):
    """On the board: keyerhost.main(["status"]). The first call of an
    interpreter session creates the board transport and, if the SDK is
    there, selects, clocks and resets the project; later calls (and later
    mpremote invocations, which keep the interpreter) reuse it, so the chip
    keeps its state between commands. `reset` resets it on purpose."""
    global _board
    if _board is None:
        transport = BoardTransport()
        try:
            transport.setup()
        except ImportError:
            pass
        _board = transport
    print(run_sync(command(KeyerHost(_board), argv)))


def _pc_assemble(path, symbols):
    import keyerasm
    with open(path) as f:
        words, _, _ = keyerasm.assemble(f.read(), symbols=symbols)
    return keyerasm.to_list(words, size=max(words) + 1)


def _pc_main(argv, call=None):
    """On a PC: assemble if needed and forward the command to the board with
    mpremote (pip install mpremote), which mounts tools/ so that the board
    imports this very file. `capture listing` and `capture decode` run here:
    the entries come from the board's `capture read` (or from --file F) and
    are decoded on the PC. `call(argv, capture)` runs one board command
    (tests replace it); it returns (exit status, output if capture)."""
    import os
    import subprocess
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    port = None
    if argv and argv[0] == "--port":
        port, argv = argv[1], argv[2:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    def board(av, capture=False):
        # "resume": no soft reset of the board's interpreter, so the transport
        # made by the first command (which reset the chip) is reused
        cmd = ["mpremote"] + (["connect", port] if port else []) + \
              ["resume", "mount", here, "exec", "import keyerhost; keyerhost.main(%r)" % (av,)]
        try:
            if capture:
                p = subprocess.run(cmd, stdout=subprocess.PIPE, universal_newlines=True)
                return p.returncode, p.stdout
            return subprocess.call(cmd), None
        except FileNotFoundError:
            print("mpremote not found: pip install mpremote, or copy tools/keyerhost.py to the board and "
                  "call keyerhost.main(%r) at its prompt" % (av,))
            return 1, None

    call = call or board
    if argv[0] == "load" and len(argv) >= 2 and not all(c in "0123456789abcdefABCDEF" for c in argv[1]):
        path, symbols, rest = argv[1], {}, argv[2:]
        while rest:
            d = rest.pop(0)
            if d == "-D" and rest:
                d = "-D" + rest.pop(0)
            if not d.startswith("-D") or "=" not in d:
                print("load FILE.s [-D NAME=VALUE ...]: unexpected %r" % d)
                return 2
            k, _, v = d[2:].partition("=")
            symbols[k.strip()] = int(v, 0)
        if path.endswith(".s"):
            words = _pc_assemble(path, symbols)
        else:
            with open(path) as f:
                words = [int(x, 16) for x in f.read().split()]
        argv = ["load"] + ["%04X" % w for w in words]
    if argv[0] == "capture" and len(argv) > 1 and argv[1] in ("read", "listing", "decode"):
        sub, args = argv[1], list(argv[2:])
        save = path = None
        if "--save" in args:
            k = args.index("--save")
            save = args[k + 1]
            del args[k:k + 2]
        if "--file" in args:
            k = args.index("--file")
            path = args[k + 1]
            del args[k:k + 2]
        if sub == "read" and save is None:
            return call(["capture", "read"] + args, False)[0]
        if path is not None:
            with open(path) as f:
                text = f.read()
        else:
            pos, opts = _options(args)
            base = pos[1:2] if sub == "decode" else pos[:1]
            st, text = call(["capture", "read"] + base + (["--group", str(opts["group"])] if "group" in opts else []), True)
            if st:
                if text:
                    print(text)
                return st
        if save is not None:
            with open(save, "w") as f:
                f.write(text if text.endswith("\n") else text + "\n")
        if sub == "read":
            print(text.rstrip("\n"))
            return 0
        entries, group, mask = parse_capture(text)
        if group is None and "--group" not in args:
            print("the capture has no group: give --group G")
            return 2
        try:
            print(capture_report(sub, args, entries, 0 if group is None else group, 0xF if mask is None else mask))
        except KeyerError as e:
            print(e)
            return 2
        return 0
    return call(argv, False)[0]


if __name__ == "__main__":
    import sys
    if sys.implementation.name == "micropython":
        main(sys.argv[1:] or ["selftest"])
    else:
        sys.exit(_pc_main(sys.argv[1:]))
