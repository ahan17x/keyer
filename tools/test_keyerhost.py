"""pytest for tools/keyerhost.py against a pin-level fake chip.

The fake decodes the SPI waveform that BitBangSPI produces (so the bit order
and CS framing are checked) and implements just enough registers: ID, CTRL,
STAT, PCs, program memory, LEVELS and the FIFOs with a thread that echoes
inbox + 1 while it runs. The real chip is covered by test/test_host.py,
which runs the same self-test bodies through the simulation transport.

The second half tests the capture tools (docs/CAPTURE.md section 5): the
listing and the protocol decoders on entries made from synthetic waveforms,
the passive I2C decoder, and the `capture` commands on the board and PC side.
"""

import os
import sys
from collections import deque

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyerasm  # noqa: E402
import keyerhost as kh  # noqa: E402


class FakeChip:
    def __init__(self, echo_per_idle=4):
        self.sck = self.mosi = 0
        self.csn = 1
        self.bits = self.shift = 0
        self.nbyte = 0
        self.cmd = None
        self.mem = [0] * 256
        self.addr = 0
        self.lo = 0
        self.pc = [0, 0]
        self.run = 0
        self.inbox, self.outbox = [deque(), deque()], [deque(), deque()]
        self.dropped = 0            # host bytes written to a full inbox
        self.stale_reads = 0        # host reads of an empty outbox
        self.max_tx_bytes = 0       # longest data phase seen
        self.echo_per_idle = echo_per_idle
        self.tx = 0
        self.resets = []            # (edge, CS_n level at that edge)
        self.cap_cfg, self.cap_buf, self.cr_count = [0, 0], [0, 0], [0, 0]   # read back as written
        self.cr_writes = []         # CR_CTRL control bytes

    # -- the "thread": echo inbox + 1 to outbox while RUN0 is set
    def work(self, n):
        for _ in range(n):
            if self.run & 1 and self.inbox[0] and len(self.outbox[0]) < 16:
                self.outbox[0].append((self.inbox[0].popleft() + 1) & 0xFF)

    # -- register side
    def _read_byte(self, reg, k):
        if reg == kh.R_ID:
            return [kh.ID_BYTE, 2][k % 2]
        if reg == kh.R_CTRL:
            return self.run
        if reg == kh.R_STAT:
            blocked = int(bool(self.run & 1) and not self.inbox[0])
            return self.run | (blocked << 4)
        if reg in (kh.R_PC0, kh.R_PC1):
            return self.pc[reg - kh.R_PC0] if k == 0 else 0
        if reg == kh.R_LEVELS:
            return [len(self.inbox[0]), len(self.outbox[0]), len(self.inbox[1]), len(self.outbox[1])][k % 4]
        if reg in (kh.R_CAP_CFG, kh.R_CAP_BUF, kh.R_CR_COUNT):
            return {kh.R_CAP_CFG: self.cap_cfg, kh.R_CAP_BUF: self.cap_buf, kh.R_CR_COUNT: self.cr_count}[reg][k % 2]
        if reg == kh.R_IMEM_DATA:
            w = self.mem[(self.addr + k // 2) & 0xFF]
            return (w >> 8) if k % 2 else (w & 0xFF)
        if reg in (kh.R_OUTBOX0, kh.R_OUTBOX1):
            q = self.outbox[(reg - kh.R_OUTBOX0) // 2]
            if not q:
                self.stale_reads += 1
                return 0xEE
            return q[0]
        return 0

    def _byte_done(self, b):
        if self.cmd is None:
            self.cmd = b
            return
        k = self.nbyte - 2          # nbyte counts completed bytes, the command included
        reg, write = self.cmd & 0x7F, bool(self.cmd & 0x80)
        self.max_tx_bytes = max(self.max_tx_bytes, k + 1)
        if not write:
            if reg in (kh.R_OUTBOX0, kh.R_OUTBOX1):
                q = self.outbox[(reg - kh.R_OUTBOX0) // 2]
                if q:
                    q.popleft()
            return
        if reg == kh.R_CTRL and k == 0:
            self.run = b & 3
        elif reg in (kh.R_PC0, kh.R_PC1) and k == 0:
            if not (self.run >> (reg - kh.R_PC0)) & 1:
                self.pc[reg - kh.R_PC0] = b
        elif reg == kh.R_IMEM_ADDR and k == 0:
            self.addr = b
        elif reg in (kh.R_CAP_CFG, kh.R_CAP_BUF) and k < 2:
            (self.cap_cfg if reg == kh.R_CAP_CFG else self.cap_buf)[k] = b
        elif reg == kh.R_CR_CTRL and k == 0:
            self.cr_writes.append(b)
        elif reg == kh.R_IMEM_DATA:
            if k % 2 == 0:
                self.lo = b
            else:
                self.mem[self.addr] = (b << 8) | self.lo
                self.addr = (self.addr + 1) & 0xFF
        elif reg in (kh.R_INBOX0, kh.R_INBOX1):
            q = self.inbox[(reg - kh.R_INBOX0) // 2]
            if len(q) >= 16:
                self.dropped += 1
            else:
                q.append(b)

    # -- pin side (what BitBangSPI calls)
    def set_sck(self, v):
        if self.csn == 0 and self.sck == 0 and v == 1:
            if self.bits == 0 and self.cmd is not None and not (self.cmd & 0x80):
                self.tx = self._read_byte(self.cmd & 0x7F, self.nbyte - 1)
            self.shift = ((self.shift << 1) | self.mosi) & 0xFF
            self.bits += 1
        self.sck = v

    def set_mosi(self, v):
        self.mosi = v

    def set_csn(self, v):
        if v == 0 and self.csn == 1:
            assert self.sck == 0, "SCK must be low when CS_n falls (mode 0)"
            self.bits = self.nbyte = 0
            self.cmd = None
        if v == 1 and self.csn == 0:
            assert self.bits == 0, "CS_n rose in the middle of a byte"
        self.csn = v

    def get_miso(self):
        bit = (self.tx >> (8 - self.bits)) & 1 if self.cmd is not None and self.bits else 0
        if self.bits == 8:
            self.bits = 0
            self.nbyte += 1
            self._byte_done(self.shift)
        return bit


class FakeTransport:
    def __init__(self, chip):
        self.chip = chip

        async def half():
            return None
        self.spi = kh.BitBangSPI(chip.set_sck, chip.set_mosi, chip.set_csn, chip.get_miso, half)

    async def xfer(self, cmd, data):
        return await self.spi.xfer(cmd, data)

    async def idle(self, cycles):
        self.chip.work(self.chip.echo_per_idle)

    async def reset(self):
        self.spi.park()
        self.spi.in_reset = True
        self.chip.resets.append(("assert", self.chip.csn))
        self.chip.run = 0
        self.chip.resets.append(("release", self.chip.csn))
        self.spi.in_reset = False


def make(echo_per_idle=4, **kw):
    chip = FakeChip(echo_per_idle)
    return chip, kh.KeyerHost(FakeTransport(chip), **kw)


def test_echo_program_matches_the_assembler():
    words, _, _ = keyerasm.assemble("loop: pop r0\n inc r0\n push r0\n bra loop")
    assert keyerasm.to_list(words, size=4) == kh.ECHO_PROGRAM


def test_bitbang_byte_order_and_framing():
    chip, host = make()
    assert kh.run_sync(host.id()) == (0x4B, 2)
    kh.run_sync(host.write(kh.R_IMEM_ADDR, [0x20, 0]))
    kh.run_sync(host.write(kh.R_IMEM_DATA, [0x34, 0x12, 0xCD, 0xAB]))
    assert chip.mem[0x20:0x22] == [0x1234, 0xABCD] and chip.csn == 1 and chip.sck == 0


def test_program_load_readback_and_pcs():
    chip, host = make()
    words = [(i * 257 + 5) & 0xFFFF for i in range(256)]
    kh.run_sync(host.load_program(words))
    assert chip.mem == words
    assert kh.run_sync(host.read_program(0x10, 5)) == words[0x10:0x15]
    kh.run_sync(host.set_pc(1, 0x90))
    assert kh.run_sync(host.get_pc(1)) == 0x90
    kh.run_sync(host.run(0b10))
    kh.run_sync(host.set_pc(1, 0x11))                 # ignored while running
    assert chip.pc[1] == 0x90


def test_push_never_overruns_and_pop_never_reads_stale():
    chip, host = make(echo_per_idle=3)
    kh.run_sync(host.run(0b01))
    data = list(range(100))

    async def body():
        got = []
        for i in range(0, 100, 25):                   # chunks larger than the FIFO
            await host.push(0, data[i:i + 25])
            got += await host.pop(0, 25, wait=True)
        return got

    assert kh.run_sync(body()) == [(b + 1) & 0xFF for b in data]
    assert chip.dropped == 0 and chip.stale_reads == 0
    assert chip.max_tx_bytes <= 16                    # no transaction longer than the FIFO


def test_pop_without_wait_returns_what_is_there():
    chip, host = make()
    chip.outbox[1].extend([9, 8, 7])
    assert kh.run_sync(host.pop(1)) == [9, 8, 7]
    assert kh.run_sync(host.pop(1)) == [] and chip.stale_reads == 0
    chip.outbox[0].extend([1, 2, 3])
    assert kh.run_sync(host.pop(0, 2)) == [1, 2] and list(chip.outbox[0]) == [3]


def test_push_times_out_when_nobody_drains():
    chip, host = make(poll_limit=5)
    with pytest.raises(kh.KeyerTimeout):
        kh.run_sync(host.push(0, list(range(20))))     # thread not running: 16 fit, 4 never do
    assert len(chip.inbox[0]) == 16 and chip.dropped == 0


def test_pop_wait_times_out():
    chip, host = make(poll_limit=3)
    with pytest.raises(kh.KeyerTimeout):
        kh.run_sync(host.pop(0, 4, wait=True))


def test_replay_load_checks_the_delta_range():
    chip, host = make()
    with pytest.raises(kh.KeyerError):
        kh.run_sync(host.replay_load([(0, 1), (5000, 0)]))
    kh.run_sync(host.replay_load([(0, 0x4), (4095, 0xC)], base=0x40))
    assert chip.mem[0x40:0x42] == [0x0004, 0xFFFC]


def test_command_line_layer():
    chip, host = make()
    assert kh.run_sync(kh.command(host, ["id"])) == "id 0x4B version 2"
    assert kh.run_sync(kh.command(host, ["load", "E020", "10C0", "E000", "90FC"])) == "loaded 4 words"
    assert chip.mem[:4] == kh.ECHO_PROGRAM
    kh.run_sync(kh.command(host, ["run", "1"]))
    assert kh.run_sync(kh.command(host, ["push", "0", "41", "42"])) == "pushed 2"
    chip.work(4)
    assert kh.run_sync(kh.command(host, ["pop", "0"])) == "42 43"
    assert "running [1, 0]" in kh.run_sync(kh.command(host, ["status"]))
    with pytest.raises(kh.KeyerError):
        kh.run_sync(kh.command(host, ["frobnicate"]))


# ---- reset: no transaction in progress when it is released (SEMANTICS 10.1, D-038)

def test_reset_keeps_cs_high_and_refuses_inside_a_transaction():
    chip, host = make()
    kh.run_sync(host.run(0b01))
    kh.run_sync(host.reset())
    assert chip.resets == [("assert", 1), ("release", 1)] and chip.run == 0
    assert kh.run_sync(host.id()) == (0x4B, 2)

    # a reset requested from inside a transaction (here: from the half-period
    # wait of its first bit) is refused, and the transaction is not cut
    spi = host.t.spi
    seen = []

    async def half():
        if spi.busy and not seen:
            seen.append(1)
            with pytest.raises(kh.KeyerError):
                kh.run_sync(host.reset())
    spi.half = half
    assert kh.run_sync(host.id()) == (0x4B, 2) and seen == [1]
    assert chip.resets == [("assert", 1), ("release", 1)]

    # and no transaction starts while the transport holds the chip in reset
    spi.in_reset = True
    with pytest.raises(kh.KeyerError):
        kh.run_sync(host.id())
    assert chip.csn == 1
    spi.in_reset = False
    assert kh.run_sync(host.id()) == (0x4B, 2)


def test_board_transport_parks_the_pins_around_reset(monkeypatch):
    """BoardTransport.setup() and reset() with a fake MicroPython `machine`
    and board SDK: CS_n is high and SCK low at both edges of every reset."""
    import types
    log, pins = [], {}

    class Pin:
        OUT, IN = 1, 0

        def __init__(self, num, mode, value=0):
            self.num, self.v = num, value
            pins[num] = self

        def value(self, v=None):
            if v is None:
                return self.v
            self.v = v
            log.append(("pin", self.num, v))

    class Board:
        shuttle = types.SimpleNamespace(tt_um_ahan17x_keyer=types.SimpleNamespace(enable=lambda: log.append(("enable",))))

        def reset_project(self, on):
            log.append(("reset", on, pins[19].v, pins[17].v))

        def clock_project_PWM(self, hz):
            log.append(("clock", hz))

    board = Board()
    monkeypatch.setitem(sys.modules, "machine", types.SimpleNamespace(Pin=Pin))
    monkeypatch.setitem(sys.modules, "ttboard", types.ModuleType("ttboard"))
    monkeypatch.setitem(sys.modules, "ttboard.demoboard",
                        types.SimpleNamespace(DemoBoard=types.SimpleNamespace(get=lambda: board)))
    slept = []
    monkeypatch.setitem(sys.modules, "time", types.SimpleNamespace(sleep_us=slept.append))
    t = kh.BoardTransport()
    with pytest.raises(kh.KeyerError):              # no SDK handle yet
        kh.run_sync(t.reset())
    pins[19].v = 0                                  # CS_n left low by something else
    t.setup()
    resets = [e for e in log if e[0] == "reset"]
    assert resets == [("reset", True, 1, 0), ("reset", False, 1, 0)]
    assert not t.spi.in_reset and slept and min(slept) >= 1
    kh.run_sync(t.xfer(kh.R_ID, [0, 0]))
    del log[:]
    kh.run_sync(kh.KeyerHost(t).reset())
    assert [e for e in log if e[0] == "reset"] == [("reset", True, 1, 0), ("reset", False, 1, 0)]
    t.spi.busy = True                               # as if inside a transaction
    with pytest.raises(kh.KeyerError):
        kh.run_sync(t.reset())



# ---- capture tools (docs/CAPTURE.md section 5) ------------------------------------

import protomodels as pm  # noqa: E402
import protomodels_eth_10bt as eth  # noqa: E402
import protomodels_usb_host as uh  # noqa: E402


def i2c_levels(ops, q=10):
    """Per-cycle nibbles (SCL bit 2, SDA bit 3) of a bus master doing `ops`:
    "S" START (or repeated START), "P" STOP, (byte, ack) nine clocks with
    the ACK bit as the ninth (0 = ACK). Each phase lasts q cycles."""
    w = [(1, 1)] * q

    def hold(scl, sda):
        w.extend([(scl, sda)] * q)
    for op in ops:
        if op == "S":
            hold(w[-1][0], 1)
            hold(1, 1)
            hold(1, 0)
            hold(0, 0)
        elif op == "P":
            hold(0, 0)
            hold(1, 0)
            hold(1, 1)
        else:
            byte, ack = op
            for b in [(byte >> (7 - i)) & 1 for i in range(8)] + [ack]:
                hold(0, b)
                hold(1, b)
                hold(0, b)
    hold(1, 1)
    return [(scl << 2) | (sda << 3) for scl, sda in w]


I2C_OPS = ["S", (0xA0, 0), (0x10, 0), "S", (0xA1, 0), (0x5A, 0), (0xC3, 1), "P",
           "S", (0xA4, 1), "P"]
I2C_TRANSFERS = [("start", 0x50, 0, 0, [(0x10, 0)], "restart"),
                 ("restart", 0x50, 1, 0, [(0x5A, 0), (0xC3, 1)], "stop"),
                 ("start", 0x52, 0, 1, [], "stop")]


def i2c_capture():
    """The entries a START-triggered capture records of I2C_OPS."""
    lv = i2c_levels(I2C_OPS)
    return kh.entries_from_wave(lv[lv.index(0x4):])


def test_expand_and_entries_from_wave_are_inverse():
    entries = [(0, 0x4), (3, 0x0), (4095, 0x0), (905, 0x8), (1, 0xC)]
    pads = kh.expand_capture(entries, group=1)
    assert len(pads) == 1 + 3 + 4095 + 905 + 1
    assert pads[0] == 0x40 and pads[2] == 0x40 and pads[3] == 0x00 and pads[-2] == 0x80 and pads[-1] == 0xC0
    assert kh.entries_from_wave([p >> 4 for p in pads]) == entries
    assert kh.capture_cycles(entries) == [0, 3, 4098, 5003, 5004]
    assert len(kh.expand_capture(entries, 0, tail=7)) == len(pads) + 7
    # a gap of exactly 4095 cycles before a change is a real entry, not an idle one
    lv = [1] + [0] * 4095 + [1]
    assert kh.entries_from_wave(lv) == [(0, 1), (1, 0), (4095, 1)]
    lv = [1] * 4096 + [0]
    assert kh.entries_from_wave(lv) == [(0, 1), (4095, 1), (1, 0)]
    assert kh.entries_from_wave([]) == [] and kh.expand_capture([], 3) == []


def test_capture_listing():
    entries = [(0, 0x4), (36, 0x0), (4095, 0x0), (10, 0x8)]
    text = kh.capture_listing(entries, 0, clock_hz=60000000, names={2: "SCL", 3: "SDA"}, mask=0xC)
    lines = text.splitlines()
    assert lines[0] == "capture: group 0 (pins 0-3), 4 entries, 4141 cycles = 69.017 us at 60 MHz"
    assert "p3=SDA p2=SCL p1 p0" in lines[1]
    assert lines[2].split() == ["0", "0", "0.000", "0", "0", "1", "-", "-", "trigger"]
    assert lines[3].split()[-2:] == ["SCL", "fall"] and lines[3].split()[1] == "36"
    assert lines[4].split()[-1] == "idle" and lines[4].split()[1] == "4131"
    assert lines[5].split()[1:3] == ["4141", "69.017"] and lines[5].endswith("SDA rise")
    # no clock, no names, another group: pin numbers, no time column
    text = kh.capture_listing([(0, 0x1), (5, 0x3)], 4)
    assert text.splitlines()[0] == "capture: group 4 (pins 16-19), 2 entries, 5 cycles"
    assert "p19 p18 p17 p16" in text and text.splitlines()[3].endswith("p17 rise")
    # the total is the sum of the deltas
    e = i2c_capture()
    assert ("%d cycles" % sum(d for d, _ in e)) in kh.capture_listing(e, 0)


def test_i2c_decoder_nack_and_repeated_start():
    class M:
        cycle = 0

        def pad(self):
            return self.v
    dec, m = pm.I2cDecoder(scl=2, sda=3), M()
    for c, v in enumerate(i2c_levels(I2C_OPS)):
        m.cycle, m.v = c, v
        dec.on_cycle(m)
    dec.finish()
    assert dec.transfers == I2C_TRANSFERS and dec.errors == []
    kinds = [k for k, _, _ in dec.events]
    assert kinds == ["start", "byte", "byte", "restart", "byte", "byte", "byte", "stop",
                     "start", "byte", "stop"]
    assert [d for k, _, d in dec.events if k == "byte"] == [(0xA0, 0), (0x10, 0), (0xA1, 0), (0x5A, 0),
                                                            (0xC3, 1), (0xA4, 1)]
    starts = [c for k, c, _ in dec.events if k == "start"]
    assert starts[0] == 30                    # 3 q of the idle bus, then SDA falls
    # a STOP three bits into a byte, and a capture that ends inside a byte
    dec = pm.I2cDecoder(scl=2, sda=3)
    lv = i2c_levels(["S", (0xA0, 0), "P"])
    cut = lv[:10 + 40 + 3 * 30] + i2c_levels(["P"])[10:]          # START, 3 bits, STOP
    for c, v in enumerate(cut):
        m.cycle, m.v = c, v
        dec.on_cycle(m)
    assert [e[0] for e in dec.errors] == ["stop inside a byte"] and dec.errors[0][2] == 3
    dec = pm.I2cDecoder(scl=2, sda=3)
    for c, v in enumerate(lv[:10 + 40 + 5 * 30]):
        m.cycle, m.v = c, v
        dec.on_cycle(m)
    dec.finish()
    assert dec.errors == [("incomplete byte", None, 5)] and dec.transfers == [("start", None, None, None, [], None)]


def test_decode_i2c_capture():
    res, text = kh.decode_capture(i2c_capture(), 0, "i2c")
    assert res["transfers"] == I2C_TRANSFERS and res["errors"] == []
    lines = text.splitlines()
    assert lines[0] == "i2c (scl bit 2 = pin 2, sda bit 3 = pin 3): 3 transfers, 0 errors"
    assert lines[1].split()[1:] == ["START", "0x50", "W", "ACK", ":", "10", "ACK"]
    assert lines[2].split()[1:] == ["RESTART", "0x50", "R", "ACK", ":", "5A", "ACK,", "C3", "NACK"]
    assert lines[3].split()[1:] == ["STOP"]
    assert lines[4].split()[1:] == ["START", "0x52", "W", "NACK"]
    assert lines[5].split()[1:] == ["STOP"] and len(lines) == 6
    # without the assumed idle level before the trigger the first START is missed
    res, _ = kh.decode_capture(i2c_capture(), 0, "i2c", initial=0x4)
    assert res["transfers"][0][0] == "restart" or res["errors"]
    # other pins of another group
    e = [(d, ((p >> 2) & 1) | ((p >> 3) & 1) << 1) for d, p in i2c_capture()]
    res, _ = kh.decode_capture(e, 1, "i2c", scl=0, sda=1)
    assert res["transfers"] == I2C_TRANSFERS


def test_decode_uart_capture():
    P = 40
    data = [0x55, 0x00, 0xFF, 0x4B, 0x0A]
    stim = pm.UartStimulus(ui_bit=0, period=P, data=data)
    lv = [v << 2 for v in stim.wave]
    entries = kh.entries_from_wave(lv[lv.index(0):])                      # triggered by the first start bit
    res, text = kh.decode_capture(entries, 4, "uart", period=P)
    assert res["bytes"] == data and res["errors"] == [] and res["starts"][0] == 0
    assert text.splitlines()[0] == "uart (line bit 2 = pin 18; 40 cycles per bit, 8N1): 5 bytes, 0 errors"
    assert text.splitlines()[4].split()[1:] == ["4B", "'K'"]
    res, _ = kh.decode_capture(entries, 4, "uart", baud=1500000, clock_hz=60000000)   # 40 cycles per bit
    assert res["bytes"] == data
    # a last byte whose last edge is a data bit (0xF0: bit 4 rises): the
    # stop bit is after the last entry, inside the default tail
    stim = pm.UartStimulus(ui_bit=0, period=P, data=[0x31, 0xF0])
    lv = [v << 2 for v in stim.wave]
    e = kh.entries_from_wave(lv[lv.index(0):])
    assert e[-1] == (5 * P, 0x4)                                          # start bit + bits 0-3 low
    assert kh.decode_capture(e, 4, "uart", period=P)[0]["bytes"] == [0x31, 0xF0]
    assert kh.decode_capture(e, 4, "uart", period=P, tail=2 * P)[0]["bytes"] == [0x31]
    with pytest.raises(kh.KeyerError):
        kh.decode_capture(entries, 4, "uart")                             # no period
    with pytest.raises(kh.KeyerError):
        kh.decode_capture(entries, 4, "uart", period=P, parity=1)         # unknown parameter
    with pytest.raises(kh.KeyerError):
        kh.decode_capture(entries, 4, "uart", period=P, bit=4)            # not a bit of the nibble
    with pytest.raises(kh.KeyerError):
        kh.decode_capture(entries, 4, "rs485")
    # a framing error: the stop bit of the second byte low
    bad = list(lv)
    s2 = stim.wave.index(0, 20 + 12 * P)                                  # second start bit
    for c in range(s2 + 9 * P, s2 + 10 * P):
        bad[c] = 0
    res, _ = kh.decode_capture(kh.entries_from_wave(bad[20:]), 4, "uart", period=P)
    assert [e[0] for e in res["errors"]] == ["framing"]


def spi_levels(frames, h=5, sck=0, mosi=1, csn=2, miso=3):
    """Mode 0 master: per frame CS_n falls, bytes MSB first (MOSI and MISO
    set while SCK is low, sampled at the rising edge), CS_n rises."""
    w = []

    def hold(s, mo, c, mi, n=h):
        w.extend([(s << sck) | (mo << mosi) | (c << csn) | (mi << miso)] * n)
    hold(0, 0, 1, 0, 2 * h)
    for out, inp in frames:
        hold(0, 0, 0, 0)
        for bo, bi in zip(out, inp):
            for i in range(7, -1, -1):
                hold(0, (bo >> i) & 1, 0, (bi >> i) & 1)
                hold(1, (bo >> i) & 1, 0, (bi >> i) & 1)
        hold(0, 0, 0, 0)
        hold(0, 0, 1, 0, 3 * h)
    return w


def test_decode_spi_capture():
    frames = [([0x9F, 0x00, 0x00], [0xFF, 0xEF, 0x40]), ([0x03, 0x12], [0x00, 0xA5])]
    lv = spi_levels(frames)
    entries = kh.entries_from_wave(lv[lv.index(0):])                      # triggered by CS_n falling
    res, text = kh.decode_capture(entries, 2, "spi", miso=3)
    assert res["mosi"] == [f[0] for f in frames] and res["miso"] == [f[1] for f in frames]
    assert res["errors"] == [] and res["starts"][0] == 0
    assert text.splitlines()[1].split()[1:] == ["MOSI", "9F", "00", "00", "|", "MISO", "FF", "EF", "40"]
    res, _ = kh.decode_capture(entries, 2, "spi")                         # MOSI only
    assert res["mosi"] == [f[0] for f in frames] and res["miso"] is None
    # a frame cut after 12 bits
    cut = spi_levels([([0xAB, 0xCD], [0, 0])])
    k = 10 + 5 + 12 * 10
    lv = cut[:k] + [0x4] * 10
    res, _ = kh.decode_capture(kh.entries_from_wave(lv[10:]), 2, "spi")
    assert res["mosi"] == [[0xAB]] and [e[0] for e in res["errors"]] == ["partial byte"]


def usb_levels(packets, T=32, gap_bits=3):
    """Line states of low-speed packets (bytes after SYNC; "keepalive" is a
    bare EOP), as nibbles with D+ on bit 0 and D- on bit 1."""
    lv = {uh.J: 0x2, uh.K: 0x1, uh.SE0: 0x0}
    syms = [uh.J] * 4
    for p in packets:
        syms += [uh.SE0, uh.SE0, uh.J] if p == "keepalive" else uh.encode(p)
        syms += [uh.J] * gap_bits
    return [lv[s] for s in syms for _ in range(T)]


def test_decode_usb_capture():
    setup = uh.setup_packet(0x80, 6, 0x0100, 0, 18)
    pkts = ["keepalive", uh.token_bytes("SETUP", 0, 0), uh.data_bytes(0, setup), [uh.PID["ACK"]],
            uh.token_bytes("IN", 5, 1), uh.data_bytes(1, [0x12, 0x01, 0x10]), uh.token_bytes("SOF", 0x3A, 0),
            uh.data_bytes(1, [1, 2], corrupt_crc=True), uh.token_bytes("OUT", 2, 0, bad_crc5=True)]
    entries = kh.entries_from_wave(usb_levels(pkts))
    res, text = kh.decode_capture(entries, 0, "usb")
    p = res["packets"]
    assert [x["pid"] for x in p] == ["SETUP", "DATA0", "ACK", "IN", "DATA1", "SOF", "DATA1", "OUT"]
    assert (p[0]["addr"], p[0]["ep"], p[0]["crc"]) == (0, 0, "ok") and p[0]["errors"] == []
    assert p[1]["payload"] == setup and p[1]["crc"] == "ok" and p[1]["errors"] == []
    assert p[2]["crc"] is None and p[2]["errors"] == []
    assert (p[3]["addr"], p[3]["ep"]) == (5, 1) and p[4]["payload"] == [0x12, 0x01, 0x10]
    assert p[5]["frame"] == 0x3A and p[5]["crc"] == "ok"
    assert p[6]["crc"] == "bad" and [e[0] for e in p[6]["errors"]] == ["crc16"]
    assert p[7]["crc"] == "bad" and [e[0] for e in p[7]["errors"]] == ["crc5"]
    assert [e[0] for e in res["errors"]] == ["crc16", "crc5"]
    assert [(k, n) for k, _, n in res["events"]] == [("keep-alive", 64)]
    assert "DATA0  80 06 00 01 00 00 12 00  crc ok" in text
    # D+ and D- on other bits, and a capture that ends inside a packet
    lv = usb_levels([uh.data_bytes(0, [0xAA])])
    e = kh.entries_from_wave([((v & 1) << 3) | ((v >> 1) << 2) for v in lv])
    res, _ = kh.decode_capture(e, 0, "usb", dp=3, dm=2)
    assert res["packets"][0]["payload"] == [0xAA] and res["errors"] == []
    res, _ = kh.decode_capture(kh.entries_from_wave(lv[:len(lv) // 2]), 0, "usb", tail=0)
    assert "eop" in [e[0] for e in res["errors"]]


def test_decode_manchester_capture():
    frame = eth.frame_bytes([0xFF] * 6, [0x02, 0, 0, 0, 0, 0x01], 0x88B5, list(range(46)))
    pulse = [(1, 0)] * 4                                                  # 100 ns at 25 ns per cycle
    wave = [(0, 0)] * 10 + pulse + [(0, 0)] * 500 + eth.wave(frame, half=2, soi=6, trail=20)
    entries = kh.entries_from_wave([p | (n << 1) for p, n in wave])
    res, text = kh.decode_capture(entries, 0, "manchester")
    assert [(round(s), w, lvl) for s, w, lvl in res["pulses"]] == [(250, 100.0, 1)]
    f, = res["frames"]
    assert f["dst"] == [0xFF] * 6 and f["src"] == [2, 0, 0, 0, 0, 1] and f["type"] == 0x88B5
    assert f["payload_len"] == 46 and f["fcs_ok"] and f["errors"] == [] and res["errors"] == []
    assert "type 0x88B5, 46 payload bytes, FCS ok" in text
    # a capture too short for the frame: the preamble is decoded, then it stops
    e = entries[:200]
    res, _ = kh.decode_capture(e, 0, "manchester", tail=0)
    assert res["frames"][0]["dst"] is None and res["errors"]


def fake_capture(chip, entries, group=0, mask=0xC, base=0x20):
    for i, (d, p) in enumerate(entries):
        chip.mem[base + i] = (d << 4) | p
    chip.cap_cfg = [group | (mask << 4), 0xC4]
    chip.cap_buf = [base, 200]
    chip.cr_count = [len(entries), 0]


def test_capture_commands():
    chip, host = make()
    entries = i2c_capture()
    fake_capture(chip, entries)
    assert kh.run_sync(host.capture_settings()) == (0, 0xC, 0x4, 0xC, 0x20, 200)
    assert kh.run_sync(host.capture_read()) == entries
    text = kh.run_sync(kh.command(host, ["capture", "read"]))
    assert text.splitlines()[0] == "# capture group 0 mask C entries %d" % len(entries)
    assert kh.parse_capture(text) == (entries, 0, 0xC)
    assert kh.run_sync(kh.command(host, ["drain"])).splitlines() == text.splitlines()[1:]
    lst = kh.run_sync(kh.command(host, ["capture", "listing", "--clock", "60000000", "--names", "2=SCL,3=SDA"]))
    assert lst.splitlines()[0].startswith("capture: group 0 (pins 0-3), %d entries" % len(entries))
    assert "p3=SDA p2=SCL p1 p0" in lst and lst.splitlines()[2].split()[4:] == ["0", "1", "-", "-", "trigger"]
    dec = kh.run_sync(kh.command(host, ["capture", "decode", "i2c"]))
    assert dec == kh.decode_capture(entries, 0, "i2c")[1]
    assert "0x50 W ACK : 10 ACK" in dec
    # an explicit base, and a group override (the same nibble read as group 2)
    fake_capture(chip, [(0, 0xF)], base=0x10)
    chip.cr_count = [len(entries), 0]
    dec = kh.run_sync(kh.command(host, ["capture", "decode", "i2c", "20", "--group", "2", "--clock", "1000000"]))
    assert "scl bit 2 = pin 10" in dec and "@0 (0.000 us)" in dec
    # a decoder bit outside the watch mask
    dec = kh.run_sync(kh.command(host, ["capture", "decode", "uart", "20", "--bit", "0", "--period", "10"]))
    assert dec.startswith("warning: bit (bit 0) is outside the watch mask C")
    assert kh.run_sync(kh.command(host, ["capture", "disarm"])) == "disarmed" and chip.cr_writes == [kh.CR_DISARM]
    assert kh.run_sync(kh.command(host, ["capture", "status"])).startswith("capture idle")
    assert kh.run_sync(kh.command(host, ["capture", "0", "C", "4", "C", "90", "40"])) == "armed"
    assert chip.cap_cfg == [0xC0, 0xC4] and chip.cap_buf == [0x90, 0x40] and chip.cr_writes[-1] == kh.CR_ARM
    with pytest.raises(kh.KeyerError):
        kh.run_sync(kh.command(host, ["capture", "listing", "--frobnicate", "1"]))
    with pytest.raises(kh.KeyerError):
        kh.run_sync(kh.command(host, ["capture", "decode", "i2c", "--clock"]))
    kh.run_sync(host.run(0b01))
    with pytest.raises(kh.KeyerError):                                    # the memory port rule
        kh.run_sync(kh.command(host, ["capture", "read"]))


def test_pc_side_capture_and_load(tmp_path, capsys):
    chip, host = make()
    entries = i2c_capture()
    fake_capture(chip, entries)
    sent = []

    def call(argv, capture):                    # the board, in process
        sent.append(argv)
        out = kh.run_sync(kh.command(host, argv))
        if capture:
            return 0, out + "\n"
        print(out)
        return 0, None

    assert kh._pc_main(["capture", "decode", "i2c", "--clock", "60000000"], call=call) == 0
    assert sent == [["capture", "read"]]
    assert capsys.readouterr().out.strip() == kh.decode_capture(entries, 0, "i2c", clock_hz=60000000)[1]
    f = tmp_path / "cap.txt"
    assert kh._pc_main(["capture", "read", "--save", str(f)], call=call) == 0
    assert kh.parse_capture(f.read_text()) == (entries, 0, 0xC)
    capsys.readouterr()
    sent.clear()
    assert kh._pc_main(["capture", "listing", "--file", str(f)], call=call) == 0 and sent == []
    assert capsys.readouterr().out.strip() == kh.capture_listing(entries, 0, mask=0xC)
    g = tmp_path / "drain.txt"                  # the output of `drain`: no header, so --group is needed
    g.write_text("\n".join("%4d %X" % e for e in entries))
    assert kh._pc_main(["capture", "decode", "i2c", "--file", str(g)], call=call) == 2
    capsys.readouterr()
    assert kh._pc_main(["capture", "decode", "i2c", "--file", str(g), "--group", "0"], call=call) == 0
    assert "0x50 W ACK" in capsys.readouterr().out
    assert kh._pc_main(["capture", "decode", "i2c", "20", "--group", "1"], call=call) == 0
    assert sent[-1] == ["capture", "read", "20", "--group", "1"]
    assert "pin 6" in capsys.readouterr().out
    # load FILE.s -D NAME=VALUE: the assembler symbols reach the image
    uart = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fw", "uart.s")
    sent.clear()
    assert kh._pc_main(["load", uart, "-D", "BAUD_DIV=434"], call=call) == 0
    with open(uart) as fh:
        words, _, _ = keyerasm.assemble(fh.read(), symbols={"BAUD_DIV": 434})
    image = keyerasm.to_list(words, size=max(words) + 1)
    assert sent == [["load"] + ["%04X" % w for w in image]] and chip.mem[:len(image)] == image
    with open(uart) as fh:
        default, _, _ = keyerasm.assemble(fh.read())
    assert keyerasm.to_list(default, size=len(image)) != image
    sent.clear()
    assert kh._pc_main(["load", uart, "-DBAUD_DIV=0x1B2"], call=call) == 0
    assert sent == [["load"] + ["%04X" % w for w in image]]
    assert kh._pc_main(["load", uart, "BAUD_DIV=434"], call=call) == 2


def test_board_main_resets_only_once_per_session(monkeypatch, capsys):
    """keyerhost.main() on the board: the first call selects, clocks and
    resets the project; later calls reuse the transport and do not reset
    the chip, so a capture armed by one command is still there for the next."""
    import types
    resets = []

    class Pin:
        OUT, IN = 1, 0

        def __init__(self, num, mode, value=0):
            self.v = value

        def value(self, v=None):
            if v is None:
                return self.v
            self.v = v

    class Board:
        shuttle = types.SimpleNamespace(tt_um_ahan17x_keyer=types.SimpleNamespace(enable=lambda: None))

        def reset_project(self, on):
            resets.append(on)

        def clock_project_PWM(self, hz):
            pass

    board = Board()
    monkeypatch.setitem(sys.modules, "machine", types.SimpleNamespace(Pin=Pin))
    monkeypatch.setitem(sys.modules, "ttboard", types.ModuleType("ttboard"))
    monkeypatch.setitem(sys.modules, "ttboard.demoboard",
                        types.SimpleNamespace(DemoBoard=types.SimpleNamespace(get=lambda: board)))
    monkeypatch.setitem(sys.modules, "time", types.SimpleNamespace(sleep_us=lambda us: None))
    monkeypatch.setattr(kh, "_board", None)
    kh.main(["stop"])
    assert resets == [True, False]
    kh.main(["stop"])
    kh.main(["status"])
    assert resets == [True, False]
    kh.main(["reset"])
    assert resets == [True, False, True, False]
    assert capsys.readouterr().out.splitlines()[-1] == "reset"
