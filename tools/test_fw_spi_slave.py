"""fw/spi_slave.s on the golden model against an SPI master that knows only
mode 0 (tools/protomodels_spi_slave.py).
Run: python3 -m pytest tools/test_fw_spi_slave.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyerasm  # noqa: E402
from keyersim import Machine  # noqa: E402
from protomodels_spi_slave import SpiMasterModel  # noqa: E402

FW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fw", "spi_slave.s")
FILL = 0xFF
# what the firmware's header asks of the master, in core cycles
HALF_MIN, CS_SETUP, CS_HOLD, CS_IDLE, AFTER_ABORT = 10, 20, 2, 32, 48


def machine():
    words, syms, _ = keyerasm.assemble(open(FW).read())
    m = Machine()
    m.load(keyerasm.to_list(words))
    m.host_run(0, True)
    return m, syms


def master(half=HALF_MIN, **kw):
    kw.setdefault("cs_setup", CS_SETUP)
    kw.setdefault("cs_hold", CS_HOLD)
    kw.setdefault("cs_idle", CS_IDLE)
    kw.setdefault("setup", 2)                 # the header promises 2 cycles at the minimum half period
    kw.setdefault("hold", 1)
    kw.setdefault("t_dis", 24)
    return SpiMasterModel(sck_ui_bit=3, mosi_ui_bit=4, csn_ui_bit=5, miso_pin=0, half=half, **kw)


def play(m, mm, extra=60, on_cycle=None):
    n = 0
    while mm.busy or extra:
        if not mm.busy:
            extra -= 1
        if on_cycle:
            on_cycle(m)
        mm.on_cycle(m)
        m.step()
        n += 1
        assert n < 200000
    return n


def outbox(m):
    out = []
    while m.threads[0].outbox:
        out.append(m.host_outbox_pop(0))
    return out


@pytest.mark.parametrize("half", [HALF_MIN, 11, 16, 37])
@pytest.mark.parametrize("phase", [0, 1])
def test_frames_in_both_directions(half, phase):
    """Several frames of several bytes at the fastest clock the firmware
    supports and at slower ones, with the SCK edges on either thread parity.
    Every MOSI byte reaches the outbox; the reply bytes go out in order, one
    per byte clocked, across frame boundaries."""
    m, _ = machine()
    mm = master(half)
    tx = [[0x9F], [0x03, 0x00, 0x10, 0xA5, 0x5A], [0x00, 0xFF, 0x80, 0x01]]
    reply = [0x81, 0x00, 0xFF, 0x55, 0xAA, 0x01, 0x80, 0x7E, 0x3C, 0xC3]
    for b in reply:
        assert m.host_inbox_push(0, b)
    frames = [mm.send(f, align=(phase, 2)) for f in tx]
    play(m, mm)
    assert mm.errors == [], mm.errors[:3]
    assert [f.sent for f in frames] == tx
    assert outbox(m) == sum(tx, [])
    assert sum((f.received for f in frames), []) == reply
    for f in frames:
        assert 0 < f.miso_on - f.cs_fall <= 18, f.miso_on - f.cs_fall          # on the bus in time
        assert f.miso_off - f.cs_rise <= 24, f.miso_off - f.cs_rise            # and off it again
        # MISO moves only after a falling edge, 5 to 8 cycles after it
        for c in f.miso_changes:
            d = c - max(e for e in f.falling if e < c)
            assert 5 <= d <= 8, d
    t = m.threads[0]
    assert t.running and m.uio_oe & 1 == 0


def test_filler_when_the_inbox_is_empty_and_late_reply():
    """No reply queued: the master reads the filler. A reply queued while
    CS_n is high replaces a staged filler; the byte after it is taken at the
    seventh falling edge, so a byte queued during a frame's first byte is
    the frame's second."""
    m, _ = machine()
    mm = master()
    a = mm.send([0x11, 0x22])
    play(m, mm)
    assert a.received == [FILL, FILL] and outbox(m) == [0x11, 0x22]
    assert m.host_inbox_push(0, 0x42)
    b = mm.send([0x33, 0x44, 0x55])
    late = {"at": None}

    def feed(mach):
        if b.rising and late["at"] is None:                # the frame's first byte is being clocked
            late["at"] = mach.cycle
            mach.host_inbox_push(0, 0x99)

    play(m, mm, on_cycle=feed)
    assert b.received == [0x42, 0x99, FILL], b.received
    assert mm.errors == []


def test_aborted_frame_resynchronises():
    """CS_n rises in the middle of a byte: the partial byte is not reported,
    the reply byte that was partly shifted out is used up, and the next frame
    starts on a byte boundary with the next reply byte."""
    for bits in (1, 3, 7, 9, 12, 15):
        m, _ = machine()
        mm = master()
        for b in (0xA1, 0xB2, 0xC3, 0xD4):
            m.host_inbox_push(0, b)
        bad = mm.send([0x5A, 0xC3], bits=bits)
        good = mm.send([0x12, 0x34], gap=AFTER_ABORT - CS_IDLE)
        play(m, mm)
        assert mm.errors == [], (bits, mm.errors[:3])
        whole = bits // 8
        assert bad.partial == bits % 8
        assert outbox(m) == [0x5A, 0xC3][:whole] + [0x12, 0x34], bits
        assert bad.received == [0xA1, 0xB2][:whole]
        # the aborted byte's reply is gone; the good frame gets the two after it
        assert good.received == [0xA1, 0xB2, 0xC3, 0xD4][whole + 1:whole + 3], (bits, good.received)


def test_next_frame_too_soon_after_an_abort_gets_the_filler_not_garbage():
    """After an aborted frame the thread needs up to 48 cycles of CS_n high
    to stage the next reply. A frame that starts after only 32 is still
    received whole, and its first reply byte is the filler or the next reply
    byte, never a mixture; the reply stream stays in order."""
    for bits in (3, 7, 12):
        m, _ = machine()
        mm = master()
        for b in (0xA1, 0xB2, 0xC3, 0xD4):
            m.host_inbox_push(0, b)
        mm.send([0x5A, 0xC3], bits=bits)
        good = mm.send([0x12, 0x34])
        play(m, mm)
        rest = [0xA1, 0xB2, 0xC3, 0xD4][bits // 8 + 1:]
        assert outbox(m)[-2:] == [0x12, 0x34] and mm.errors == []
        assert good.received in (rest[:2], [FILL, rest[0]]), (bits, good.received)


def test_frame_ending_on_a_byte_boundary_keeps_the_staged_reply():
    m, _ = machine()
    mm = master()
    for b in (1, 2, 3):
        m.host_inbox_push(0, b)
    a, b = mm.send([0xEE]), mm.send([0xDD, 0xCC])
    play(m, mm)
    assert a.received == [1] and b.received == [2, 3] and mm.errors == []


def test_traffic_for_another_slave_is_ignored():
    """SCK and MOSI move with CS_n high: nothing received, MISO never driven,
    no reply byte consumed."""
    m, _ = machine()
    mm = master()
    m.host_inbox_push(0, 0x77)
    other = mm.send([0xDE, 0xAD, 0xBE, 0xEF], select=False)
    mine = mm.send([0x01])
    play(m, mm)
    assert other.miso_on is None and mm.errors == []
    assert outbox(m) == [0x01] and mine.received == [0x77]


def test_master_that_breaks_off_with_sck_high():
    """A broken master raises CS_n during the high phase of a pulse, at any
    bit: the thread leaves the frame, releases MISO, and the next frame works."""
    for bits in (1, 4, 8, 13, 16):
        m, _ = machine()
        mm = master(t_dis=30)
        for b in (0x10, 0x20, 0x30, 0x40):
            m.host_inbox_push(0, b)
        mm.send([0xFF, 0x00], bits=bits, cs_high_abort=True)
        good = mm.send([0x6B, 0x7C], gap=10)
        play(m, mm)
        assert [e for e in mm.errors if e[0] == "miso driven while cs high"] == [], (bits, mm.errors)
        assert outbox(m)[-2:] == [0x6B, 0x7C], bits
        assert len(good.received) == 2 and good.miso_off - good.cs_rise <= 24


def test_full_outbox_drops_bytes_and_never_blocks():
    """17 bytes with nobody reading: the first 16 stay, the thread keeps
    following the clock, and after the host drains the outbox the next frame
    arrives whole."""
    m, _ = machine()
    mm = master()
    data = list(range(0x40, 0x51))
    mm.send(data)
    play(m, mm)
    assert outbox(m) == data[:16]
    mm.send([0xAB, 0xCD])
    play(m, mm)
    assert outbox(m) == [0xAB, 0xCD] and not m.threads[0].blocked


def test_long_frame_with_a_host_that_keeps_up():
    """More bytes than either FIFO holds: the host refills the inbox and
    drains the outbox while the frame runs (one byte per 160 cycles here)."""
    m, _ = machine()
    mm = master()
    data = [(37 * i + 5) & 0xFF for i in range(40)]
    reply = [(91 * i + 17) & 0xFF for i in range(40)]
    pending, got = list(reply), []

    def host(mach):
        while pending and mach.host_inbox_push(0, pending[0]):
            pending.pop(0)
        while True:                                        # a byte pushed in the last cycle is not
            b = mach.host_outbox_pop(0)                    # visible to the host yet (SEMANTICS 8)
            if b is None:
                break
            got.append(b)

    for _ in range(40):                                    # the inbox is full before the frame starts
        host(m)
    fr = mm.send(data)
    play(m, mm, on_cycle=host)
    host(m)
    assert got == data and fr.received == reply and mm.errors == []


def test_half_period_below_the_minimum_is_caught_by_the_model():
    """The control for the timing checks: at a half period of 6 the slave
    cannot keep MISO stable before the rising edge, and the model says so."""
    m, _ = machine()
    mm = master(half=6)
    for b in (0x55, 0xAA, 0x33):
        m.host_inbox_push(0, b)
    mm.send([0x0F, 0xF0, 0x3C])
    play(m, mm)
    assert any(e[0] == "miso setup" for e in mm.errors)


def test_image_size():
    words, _, _ = keyerasm.assemble(open(FW).read())
    assert max(words) + 1 == 55
