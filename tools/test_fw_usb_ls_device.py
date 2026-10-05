"""fw/usb_ls_device.s on the golden model against a USB low-speed host that
knows only the USB specification (tools/protomodels_usb_host.py).
Run: python3 -m pytest tools/test_fw_usb_ls_device.py -q

Bus resets and the recovery after them are compressed (64 and 16 bit times
instead of 10 ms each) except in test_enumeration_with_full_length_reset;
the device has to treat any SE0 longer than 2.5 us (3.75 bit times) as a
reset, so 64 bit times (43 us) is still a reset by the standard.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyer_isa as isa  # noqa: E402
import keyerasm  # noqa: E402
from keyersim import Machine  # noqa: E402
import protomodels_usb_host as usb  # noqa: E402
from protomodels_usb_host import UsbLsHost, setup_packet  # noqa: E402

FW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fw", "usb_ls_device.s")
RST, REC = 64, 16                    # compressed bus reset and recovery, bit times
WORDS = 253                          # the image size the header states


def assemble(source=None, **symbols):
    words, syms, _ = keyerasm.assemble(source or open(FW).read(), symbols=symbols or None)
    return keyerasm.to_list(words), syms


def machine(T=32, image=None, source=None, **symbols):
    words, syms = assemble(source, BITT=T, **symbols)
    if image is not None:
        words = image(list(words), syms)
    m = Machine()
    m.load(words)
    m.host_run(0, True)
    return m, syms


class Slack:
    """How many slots thread 0 waited in each `waitd 0` (the response
    deadline) before it completed: 0 means the firmware arrived after the
    deadline and the response went out late. Reads the model's public state
    only."""

    def __init__(self, m):
        w0 = isa.encode("WAITD", k=0)
        self.at = {a for a, w in enumerate(m.imem) if w == w0}
        self.waits = []
        self._n = None

    def before(self, m):
        t = m.threads[0]
        self._pc = t.pc if m.cycle % 2 == 0 and t.running else None

    def after(self, m):
        if self._pc in self.at:
            if m.threads[0].blocked:
                self._n = (self._n or 0) + 1
            else:
                self.waits.append(self._n or 0)
                self._n = None


def play(m, host, limit=3_000_000, slack=None):
    n = 0
    while host.busy:
        host.on_cycle(m)
        if slack:
            slack.before(m)
        m.step()
        if slack:
            slack.after(m)
        n += 1
        assert n < limit, "the host never finished"
    return n


def outbox(m):
    out = []
    while True:
        b = m.host_outbox_pop(0)
        if b is None:
            return out
        out.append(b)


def declared(syms, words, first, last):
    """The descriptor bytes the firmware source declares: the SERIC
    immediates from label `first` up to label `last`."""
    seric = isa.encode("SERIC", n=0)
    return [w & 0xFF for w in words[syms[first]:syms[last]] if w & 0xFF00 == seric]


def descriptors():
    words, syms = assemble()
    return declared(syms, words, "dev", "cfg"), declared(syms, words, "cfg", "desc_end")


DEV, CFG = descriptors()


def in_window(delays):
    return all(usb.RESP_MIN <= d <= usb.RESP_MAX for d in delays)


# ------------------------------------------------------------------ the host model itself

def test_host_crcs_and_tokens_match_published_values():
    """The host's CRCs and token coding, against values that do not come
    from this project: the CRC catalogue check values for "123456789", and
    the SETUP token every USB trace shows for address 0."""
    assert usb.crc16(b"123456789") == 0xB4C8
    assert usb.crc5(usb.lsb_bits(b"123456789")) == 0x19
    assert usb.token_bytes("SETUP", 0, 0) == [0x2D, 0x00, 0x10]
    # the token example of the USB CRC note: address 0x15, endpoint 0xE, CRC5 0b10111 (MSB first)
    c = usb.token_bytes("IN", 0x15, 0xE)[2] >> 3
    assert int("{:05b}".format(c)[::-1], 2) == 0b10111
    # the ones are counted across byte boundaries: DATA0's last two bits
    # and four of 0xFF make six, so one stuffed zero, then four more ones
    syms = usb.encode([0xC3, 0xFF])
    assert len(syms) == 8 + 16 + 1 + 3      # SYNC, 16 bits, one stuffed, EOP
    assert len(usb.encode([0xC3, 0xFF, 0xFF])) == 8 + 24 + 3 + 3


class FakeBus:
    """A stand-in for the chip: answers the host's first packet with a
    canned waveform (a list of line states, one per cycle) starting `delay`
    cycles after the SE0-to-J transition of the host's EOP."""

    def __init__(self, wave, delay):
        self.cycle, self.ext_uio, self.wave, self.delay = 0, 0xFF, wave, delay
        self._prev, self._t = None, None

    def _line(self):
        return {0b00: usb.SE0, 0b01: usb.J, 0b10: usb.K, 0b11: usb.SE1}[(self.ext_uio & 1) << 1 | (self.ext_uio >> 1) & 1]

    def pad(self):
        if self._t is not None and self._t <= self.cycle < self._t + len(self.wave):
            dp, dm = {usb.J: (0, 1), usb.K: (1, 0), usb.SE0: (0, 0)}[self.wave[self.cycle - self._t]]
            return (self.ext_uio & ~3) | dp | dm << 1
        return self.ext_uio

    def step(self):
        s = self._line()
        if self._prev == usb.SE0 and s == usb.J and self._t is None:
            self._t = self.cycle + self.delay
        self._prev = s
        self.cycle += 1


def fake_in(wave, delay_bits=3.5, T=32, extra_bits=40):
    """One IN transaction against a FakeBus; the host keeps watching the
    idle bus for `extra_bits` after it is done."""
    h = UsbLsHost(bit_cycles=T, keepalive_bits=None, seed=None)
    rec = h.token_in(0, 0)
    bus = FakeBus(wave, int(delay_bits * T))
    n = extra_bits * T
    while h.busy or n:
        n -= 0 if h.busy else 1
        h.on_cycle(bus)
        bus.step()
    return h, rec[0]


def cells(syms, T=32):
    return [s for s in syms for _ in range(T)]


def kinds(h):
    return {e[0] for e in h.errors}


GOOD = usb.data_bytes(1, [0x12, 0x01, 0xFF, 0xFF, 0x00, 0x7E, 0x3F, 0x80])


def test_host_accepts_a_correct_waveform():
    h, t = fake_in(cells(usb.encode(GOOD)))
    assert h.errors == [] and t.result == "DATA1" and t.data == GOOD[1:-2]
    assert h.delays == [3.5]


@pytest.mark.parametrize("fault, kind", [
    ("late", "response late"),
    ("early", "response early"),
    ("crc", "crc16"),
    ("nostuff", "bit stuffing"),
    ("stretch", "bit timing"),
    ("eop3", "eop"),
    ("pid", "pid"),
    ("eopK", "eop"),
])
def test_host_flags_a_faulty_waveform(fault, kind):
    """The control for every check the lockstep and model tests rely on: a
    waveform with exactly one fault produces that error."""
    delay, T = 3.5, 32
    pkt = list(GOOD)
    syms = None
    if fault == "late":
        delay = 7
    elif fault == "early":
        delay = 1.5
    elif fault == "crc":
        pkt = usb.data_bytes(1, GOOD[1:-2], corrupt_crc=True)
    elif fault == "pid":
        pkt[0] = 0x4A
    if fault == "nostuff":
        wave = cells(usb.encode(pkt, stuff=False))
    else:
        syms = usb.encode(pkt)
        if fault == "eop3":
            syms = syms[:-3] + [usb.SE0] * 3 + [usb.J]
        if fault == "eopK":
            syms = syms[:-1] + [usb.K, usb.J]
        wave = cells(syms)
        if fault == "stretch":
            wave = wave[:10 * T] + [wave[10 * T]] * (T // 2) + wave[10 * T:]
    h, t = fake_in(wave, delay)
    assert kind in kinds(h), (fault, h.errors)
    assert t.result != "DATA1" or fault in ("late", "early") and t.result == "bad"


def test_host_times_out_on_silence_and_flags_late_activity():
    h, t = fake_in([], 3)
    assert t.result == "timeout" and h.errors == []
    h, t = fake_in(cells(usb.encode([0x5A])), 20)         # a NAK after the host gave up
    assert t.result == "timeout" and "unexpected activity" in kinds(h)


# ------------------------------------------------------------------ enumeration

def enumerate_once(T=32, seed=1, first_len=64, early_status=None, reset_bits=RST, recovery_bits=REC, **kw):
    m, syms = machine(T)
    h = UsbLsHost(bit_cycles=T, seed=seed, **kw)
    e = h.enumerate(new_addr=5, first_len=first_len, reset_bits=reset_bits, recovery_bits=recovery_bits,
                    early_status=early_status)
    sl = Slack(m)
    n = play(m, h, slack=sl)
    return m, h, e, sl, n


def test_descriptors_are_well_formed():
    """What the firmware source declares, checked field by field."""
    assert len(DEV) == 18 and len(CFG) == 25
    assert DEV[:2] == [18, 1] and DEV[2:4] == [0x10, 0x01]      # USB 1.10
    assert DEV[7] == 8                                          # bMaxPacketSize0, low speed
    assert DEV[17] == 1                                         # one configuration
    assert CFG[:2] == [9, 2] and CFG[2] | CFG[3] << 8 == 25 and CFG[4] == 1 and CFG[5] == 1
    assert CFG[9:11] == [9, 4] and CFG[13] == 1                 # one interface, one endpoint
    assert CFG[18:21] == [7, 5, 0x81] and CFG[21] == 3          # EP1 IN, interrupt
    assert CFG[22] | CFG[23] << 8 == 8 and CFG[24] >= 10        # 8 bytes, >= 10 ms at low speed


def test_enumeration():
    """A host's whole enumeration sequence. Every descriptor byte matches
    the source, every response starts inside the 2 to 6.5 bit window, the
    firmware never reached a response deadline late, and the SPI host got
    the bRequest of every accepted SETUP."""
    m, h, e, sl, n = enumerate_once()
    assert h.errors == [], h.errors[:5]
    assert e.ok and e.address == 5
    assert e.device8 == DEV and e.device == DEV
    assert e.config9 == CFG[:9] and e.config == CFG
    assert [c.status for c in e.controls] == ["ok"] * 6
    assert in_window(h.delays) and len(h.delays) == 24
    assert min(sl.waits) >= 1
    assert outbox(m) == [6, 5, 6, 6, 6, 9]
    assert m.threads[0].running
    print("\n48 MHz: %d cycles, delays %.3f..%.3f bit times, slack %d..%d slots"
          % (n, min(h.delays), max(h.delays), min(sl.waits), max(sl.waits)))


@pytest.mark.parametrize("first_len, early", [(8, None), (64, 1), (18, None)])
def test_enumeration_variants(first_len, early):
    """Linux's 8-byte first request, and Windows' 64-byte request that reads
    one packet and goes straight to the status stage (an OUT in the middle
    of the data stage)."""
    m, h, e, sl, _ = enumerate_once(first_len=first_len, early_status=early, seed=11)
    assert h.errors == [], h.errors[:5]
    assert e.ok and e.device8 == DEV[:min(first_len, 8 if early else 18)]
    assert e.config == CFG


def test_response_window_over_phases_and_clocks():
    """The host's bit grid swept against the device's tick grid (eight
    seeds), at 48 MHz (period 32) and 24 MHz (period 16): every response
    of every transaction starts 2 to 6.5 bit times after the host's EOP."""
    report = []
    for T in (32, 16):
        delays, waits = [], []
        for seed in range(8):
            _, h, e, sl, _ = enumerate_once(T=T, seed=seed)
            assert h.errors == [] and e.ok, (T, seed, h.errors[:3])
            delays += h.delays
            waits += sl.waits
        assert in_window(delays)
        assert min(waits) >= 1, "a response deadline was missed"
        report.append("period %d: %d responses, delay %.3f..%.3f bit times, slack %d..%d slots"
                      % (T, len(delays), min(delays), max(delays), min(waits), max(waits)))
    print("\n" + "\n".join(report))


def test_enumeration_at_24_mhz():
    m, h, e, sl, _ = enumerate_once(T=16, seed=4)
    assert h.errors == [] and e.ok and e.device == DEV and e.config == CFG
    assert outbox(m) == [6, 5, 6, 6, 6, 9]


def test_enumeration_with_full_length_reset():
    """The first bus reset is the real 10 ms (15000 bit times) followed by
    the real 10 ms recovery; keep-alives every 1 ms throughout."""
    _, h, e, _, n = enumerate_once(reset_bits=15000, recovery_bits=15000)
    assert h.errors == [] and e.ok and e.config == CFG
    assert n > 2 * 15000 * 32


def test_keep_alives_are_not_resets():
    """A keep-alive (a bare EOP) before every transaction: the device keeps
    its address."""
    _, h, e, _, _ = enumerate_once(keepalive_bits=1)
    assert h.errors == [] and e.ok


# ------------------------------------------------------------------ requests

def device_at(addr=5, T=32, seed=2, source=None, **kw):
    """A device that has been reset and given an address (no checks)."""
    m, syms = machine(T, source=source)
    h = UsbLsHost(bit_cycles=T, seed=seed, **kw)
    h.reset(RST, REC)
    if addr:
        h.set_address(0, addr)
    play(m, h)
    outbox(m)
    assert h.errors == []
    return m, h


@pytest.mark.parametrize("dtype, wlen", [(1, n) for n in (1, 7, 8, 9, 16, 17, 18, 19, 64, 255, 1000)]
                         + [(2, n) for n in (1, 8, 9, 15, 16, 24, 25, 26, 255)])
def test_get_descriptor_honours_wlength(dtype, wlen):
    """Exactly min(wLength, length) bytes in 8-byte packets with a short
    last one; a transfer that ends on a packet boundary at wLength needs no
    zero-length packet."""
    m, h = device_at()
    c = h.get_descriptor(5, dtype, wlen)
    play(m, h)
    want = (DEV if dtype == 1 else CFG)[:wlen]
    assert h.errors == [] and c.ok and c.data == want, (c.status, c.data)
    sizes = [len(t.data) for t in c.txns if t.kind == "IN" and t.data is not None]
    assert sizes == [8] * (len(want) // 8) + ([len(want) % 8] if len(want) % 8 else [])


def test_unsupported_requests_are_stalled():
    """GET_STATUS and GET_CONFIGURATION (data stage), SET_FEATURE (status
    stage), a string descriptor, a class request and SET_CONFIGURATION(2):
    the SETUP is ACKed and the next stage STALLed, an OUT in that state too;
    the next SETUP clears it."""
    m, h = device_at()
    stalls = [h.control_read(5, setup_packet(0x80, 0, 0, 0, 2)),          # GET_STATUS
              h.control_read(5, setup_packet(0x80, 8, 0, 0, 1)),          # GET_CONFIGURATION
              h.get_descriptor(5, 3, 255),                                # string descriptor
              h.control_read(5, setup_packet(0xA1, 1, 0, 0, 8)),          # class request
              h.control_nodata(5, setup_packet(0x00, 3, 1, 0, 0)),        # SET_FEATURE
              h.set_configuration(5, 2)]
    out = h.token_out(5, 0, 1, [])
    ok = h.get_descriptor(5, 1, 18)
    play(m, h)
    assert [c.status for c in stalls] == ["stall"] * 6
    assert all(c.txns[0].result == "ACK" for c in stalls)
    assert out[0].result == "STALL"
    assert ok.ok and ok.data == DEV and h.errors == []
    assert outbox(m) == [0, 8, 6, 1, 3, 9, 6]


def test_in_before_any_setup_is_naked():
    """After a reset, before any SETUP: IN on endpoint 0 is NAKed (nothing to
    send yet), IN on endpoint 1 (the interrupt endpoint) is NAKed, and so is
    endpoint 0 again after a finished transfer."""
    m, h = device_at(addr=0)
    a = h.token_in(0, 0)
    b = h.token_in(0, 1)
    c = h.get_descriptor(0, 1, 8)
    d = h.token_in(0, 0)
    play(m, h)
    assert a[0].result == "NAK" and b[0].result == "NAK" and d[0].result == "NAK"
    assert c.ok and h.errors == []


def test_bad_token_crc_and_other_addresses_are_ignored():
    """No answer at all to: a token with a wrong CRC-5 (IN, and a SETUP with
    its DATA0), a token for another address, a SETUP for endpoint 2. The
    host times out each time; then a transfer works."""
    m, h = device_at()
    a = h.token_in(5, 0, bad_crc5=True)
    b = h.setup(5, setup_packet(0x80, 6, 0x100, 0, 18), bad_crc5=True)
    c = h.token_in(4, 0)
    d = h.setup(0, setup_packet(0x80, 6, 0x100, 0, 18))
    e = h.token_in(0, 0)
    f = h.token_out(6, 0, 1, [])
    ok = h.get_descriptor(5, 2, 9)
    play(m, h)
    assert [x[0].result for x in (a, b, c, d, e, f)] == ["timeout"] * 6
    assert ok.ok and ok.data == CFG[:9] and h.errors == []
    assert outbox(m) == [6]


def test_corrupted_data_crc_gets_no_handshake_and_the_retry_works():
    """A SETUP whose DATA0 has a bad CRC-16 is not ACKed, twice; the third
    attempt is. In the same transfer the status stage's DATA1 is corrupted
    once: no handshake, and the retry is ACKed."""
    m, h = device_at()
    c = h.get_descriptor(5, 1, 18, corrupt_setups=2, corrupt_status=1)
    play(m, h)
    res = [t.result for t in c.txns]
    assert res[:3] == ["timeout", "timeout", "ACK"]
    assert res[-2:] == ["timeout", "ACK"]
    assert c.ok and c.data == DEV and h.errors == []
    assert outbox(m) == [6]                       # only the accepted SETUP is reported


@pytest.mark.parametrize("fault", ["drop", "corrupt"])
def test_lost_host_ack_repeats_the_packet_with_the_same_toggle(fault):
    """The host's ACK of a data packet does not arrive: the device sends
    the same packet with the same DATA toggle again; the host, which had
    it, ACKs and discards it (USB 8.6.4); the transfer completes."""
    m, h = device_at()
    c = h.get_descriptor(5, 2, 25, ack_faults={1: fault})
    play(m, h)
    res = [t.result for t in c.txns if t.kind == "IN"]
    assert res == ["DATA1", "DATA0", "dup", "DATA1", "DATA0"], res
    assert c.ok and c.data == CFG and h.errors == []


def test_new_address_takes_effect_after_the_status_stage():
    m, h = device_at(addr=0)
    s = h.setup(0, setup_packet(0x00, 5, 9, 0, 0))
    early = h.token_in(9, 0)              # the status stage has not happened
    st = h.token_in(0, 0, expect=1)       # the status stage, at the old address
    old = h.token_in(0, 0)
    new = h.get_descriptor(9, 1, 18)
    play(m, h)
    assert s[0].result == "ACK" and early[0].result == "timeout"
    assert st[0].result == "DATA1" and st[0].data == []
    assert old[0].result == "timeout" and new.ok and new.data == DEV
    assert h.errors == []


def test_bus_reset_in_the_middle_returns_to_address_0():
    """A reset after SET_ADDRESS and in the middle of a data stage: the
    device answers at address 0 again, not at its old address, and has
    forgotten the transfer (IN is NAKed)."""
    m, h = device_at(addr=7)
    h.setup(7, setup_packet(0x80, 6, 0x200, 0, 25))
    first = h.token_in(7, 0)
    h.reset(RST, REC)
    old = h.token_in(7, 0)
    nak = h.token_in(0, 0)
    c = h.get_descriptor(0, 1, 18)
    play(m, h)
    assert first[0].result == "DATA1" and first[0].data == CFG[:8]
    assert old[0].result == "timeout" and nak[0].result == "NAK"
    assert c.ok and c.data == DEV and h.errors == []


def foreign_tokens_keep_the_address(source=None):
    """IN tokens for address 127, one per start phase over a whole idle-poll
    period (8 bit times, 256 cycles): the idle loop's SE0 check runs at
    every position relative to the packet. PID 0x69 then 0x7F holds K for
    seven bit times, longer than the reset threshold."""
    m, h = device_at(seed=None, keepalive_bits=None, source=source)
    for k in range(8 * 32):
        h.idle(0, cycles=k)
        h.token_in(127, 0)
    c = h.get_descriptor(5, 1, 18)
    c0 = h.get_descriptor(0, 1, 18)
    play(m, h)
    return c, c0, h


def test_traffic_for_others_is_never_taken_for_a_reset():
    """The idle loop reads D- and then D+ two cycles later to look for SE0.
    In the other order a J-to-K edge between the two reads looks like SE0,
    and the long K that follows like a reset (the first version of the
    firmware had that order; review found it, this test shows it)."""
    c, c0, h = foreign_tokens_keep_the_address()
    assert c.ok and c.data == DEV and c0.status == "fail" and h.errors == []
    src = open(FW).read()
    a, b = "bp1   DM, idle", "bp1   DP, idle"
    swapped = src.replace(a, "@@").replace(b, a).replace("@@", b)
    assert swapped != src
    c, c0, h = foreign_tokens_keep_the_address(swapped)
    assert not c.ok and c0.ok, "the D+-first order should reset the device at some phase"


def test_reset_threshold_over_all_poll_phases():
    """The header's numbers: an idle device takes SE0 that lasts 14 bit
    times for a reset at every phase of its 8-bit poll, and one of 3 bit
    times (2 us, under USB's 2.5 us) never; the 2-bit SE0 of an EOP or
    keep-alive is covered by every other test."""
    for bits, is_reset in ((3, False), (14, True)):
        m, h = device_at(seed=None, keepalive_bits=None)
        probes = []
        for k in range(0, 256, 8):
            h.idle(0, cycles=k)
            h.reset(bits, 4)
            probes.append(h.token_in(5, 0))
            if is_reset:
                h.set_address(0, 5)
        play(m, h)
        res = {p[0].result for p in probes}
        assert res == ({"timeout"} if is_reset else {"NAK"}), (bits, res)
        assert h.errors == []


def test_set_configuration():
    m, h = device_at()
    a = h.set_configuration(5, 1)
    b = h.set_configuration(5, 0)
    c = h.get_descriptor(5, 1, 18)          # still at address 5
    play(m, h)
    assert a.ok and b.ok and c.ok and h.errors == []


# ------------------------------------------------------------------ the checks catch a broken device

def run_enum(m, T=32):
    h = UsbLsHost(bit_cycles=T, seed=5)
    e = h.enumerate(new_addr=5, reset_bits=RST, recovery_bits=REC)
    play(m, h)
    return h, e


def test_model_catches_a_late_device():
    """The same firmware with the response deadline at 8 bit times instead
    of 4: every answer is late, and the host says so."""
    m, _ = machine(RESP=8)
    h, _ = run_enum(m)
    assert "response late" in kinds(h)
    assert min(h.delays) > usb.RESP_MAX


def test_model_catches_an_early_device():
    m, _ = machine(RESP=1)
    h, _ = run_enum(m)
    assert "response early" in kinds(h)


def test_model_catches_a_wrong_crc():
    """One descriptor byte sent with SERI instead of SERIC (left out of the
    CRC): the packet carrying it has a wrong CRC-16."""
    def mutate(words, syms):
        a = syms["dev"] + 8                       # the SERIC of bDeviceClass
        assert words[a] == isa.encode("SERIC", n=0xFF)
        words[a] = isa.encode("SERI", n=0xFF)
        return words
    m, _ = machine(image=mutate)
    h, e = run_enum(m)
    assert "crc16" in kinds(h) and not e.ok


def test_model_catches_a_missing_stuffed_bit():
    """The engine configured without bit stuffing: bDeviceClass 0xFF goes
    out as eight 1s in a row."""
    m, _ = machine(USBCFG=0x31)
    h, e = run_enum(m)
    assert "bit stuffing" in kinds(h) and not e.ok


def test_image_size():
    words, syms, _ = keyerasm.assemble(open(FW).read())
    assert max(words) + 1 == WORDS <= 256
    assert len(words) == WORDS
