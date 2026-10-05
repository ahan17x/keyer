"""Keyer host driver: one API, two transports.

The Keyer chip is driven over its SPI slave (mode 0, MSB first; SCK on ui[0],
MOSI on ui[1], CS_n on ui[2], MISO on uo[0]; register map in docs/isa.md
section 6). This module is the host side: load a program, set PCs, run and
stop threads, push and pop the FIFOs without overrunning them, read status
and pins, drain the capture buffer, load and start a replay.

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

    async def capture_drain(self, base=None):
        """Read the recorded entries as (delta, pins) pairs. Both threads
        must be stopped (the program memory port). `base` defaults to the
        configured capture base."""
        if (await self.read(R_CTRL, 1))[0] & 3:
            raise KeyerError("stop both threads before draining the capture buffer")
        n = (await self.cr_counts())[0]
        if base is None:
            base = (await self.read(R_CAP_BUF, 2))[0]
        return [(w >> 4, w & 0xF) for w in await self.read_program(base, n)]

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
PC with --port to forward them to the board through mpremote):
  id                         identity byte and ISA version
  status                     running / halted / blocked per thread, FIFO levels
  load WORD WORD ...         load program words (hex) at address 0   [PC: load FILE.s|FILE.hex]
  pc TID ADDR                set a thread's PC (thread stopped)
  run MASK | stop            start threads (bit 0, bit 1) / stop both
  push TID BYTE ...          push bytes (hex) into a thread's inbox
  pop TID [N]                pop N bytes (default: all available) from its outbox
  pins                       uio, ui and uo levels, and the uio drive state
  capture GROUP MASK TPAT TMASK BASE LEN    configure and arm a capture
  drain                      print the recorded entries (delta pins)
  replay GROUP MASK BASE LEN start a replay (LEN 0: the last capture's length)
  selftest                   run the built-in self-tests
"""


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


def main(argv):
    """On the board: keyerhost.main(["status"]). Creates the board transport,
    selects and clocks the project if the SDK is there, runs the command."""
    transport = BoardTransport()
    try:
        transport.setup()
    except ImportError:
        pass
    print(run_sync(command(KeyerHost(transport), argv)))


def _pc_main(argv):
    """On a PC: assemble if needed and forward the command to the board with
    mpremote (pip install mpremote), which mounts tools/ so that the board
    imports this very file."""
    import os
    import subprocess
    import sys
    port = None
    if argv and argv[0] == "--port":
        port, argv = argv[1], argv[2:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] == "load" and len(argv) == 2 and not all(c in "0123456789abcdefABCDEF" for c in argv[1]):
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, here)
        path = argv[1]
        if path.endswith(".s"):
            import keyerasm
            with open(path) as f:
                words, _, _ = keyerasm.assemble(f.read())
            words = keyerasm.to_list(words, size=max(words) + 1)
        else:
            with open(path) as f:
                words = [int(x, 16) for x in f.read().split()]
        argv = ["load"] + ["%04X" % w for w in words]
    here = os.path.dirname(os.path.abspath(__file__))
    cmd = ["mpremote"] + (["connect", port] if port else []) + \
          ["mount", here, "exec", "import keyerhost; keyerhost.main(%r)" % (argv,)]
    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        print("mpremote not found: pip install mpremote, or copy tools/keyerhost.py to the board and "
              "call keyerhost.main(%r) at its prompt" % (argv,))
        return 1


if __name__ == "__main__":
    import sys
    if sys.implementation.name == "micropython":
        main(sys.argv[1:] or ["selftest"])
    else:
        sys.exit(_pc_main(sys.argv[1:]))
