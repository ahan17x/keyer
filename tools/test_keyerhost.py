"""pytest for tools/keyerhost.py against a pin-level fake chip.

The fake decodes the SPI waveform that BitBangSPI produces (so the bit order
and CS framing are checked) and implements just enough registers: ID, CTRL,
STAT, PCs, program memory, LEVELS and the FIFOs with a thread that echoes
inbox + 1 while it runs. The real chip is covered by test/test_host.py,
which runs the same self-test bodies through the simulation transport.
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

