"""pytest for the serializer of the golden model (SEMANTICS 15).

Written from docs/SEMANTICS.md section 15 and from the two protocols (USB
low speed: NRZI, bit stuffing, CRC-5, CRC-16, SE0 SE0 J end of packet;
10BASE-T: IEEE 802.3 Manchester, CRC-32), not from the model's code. The
line coders, the stuffer, the decoders and the CRCs below are independent
of tools/keyersim.py: the CRCs are checked against the published check
values, CRC-32 against binascii.crc32.

Run: python3 -m pytest tools/ -q
"""

import binascii
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyer_isa as isa  # noqa: E402
import keyerasm  # noqa: E402
from keyersim import Machine  # noqa: E402

# Register names of SEMANTICS 13 (the u_ser row), in that order.
SER_NAMES = ("cfg owner tx_hold tx_hold_c tx_full tx_state tx_sh tx_c tx_app tx_n tx_half "
             "tx_bit tx_ones tx_line crc_m crc5 rx_state rx_sh rx_n rx_ones rx_psym rx_last "
             "rx_cnt rx_w rx_first rx_hold rx_valid rx_end rx_ovr rx_serr rx_ferr rx_c5ok "
             "rx_cok").split()
OFF_VALUES = dict(rx_sh=0xFF, rx_w=1)         # 15.4: the values while the receiver does not run

# configuration bits (15.1)
NRZI, MANCH = 1, 2
STUFF, CRC32, RXEN, RXSKIP = 4, 8, 16, 32


def pair(k):
    return k << 6


# status bits (15.7)
ST_TXFULL, ST_TXBUSY, ST_RXVALID, ST_INFRAME, ST_END = 1, 2, 4, 8, 16
ST_C5OK, ST_COK, ST_OVR, ST_SERR, ST_FERR = 32, 64, 128, 256, 512


# ---------------------------------------------------------------- protocol helpers (independent)

def crc16_usb(data):
    """CRC-16/USB: poly 0x8005 reflected, init 0xFFFF, final complement."""
    reg = 0xFFFF
    for byte in data:
        for i in range(8):
            b = (byte >> i) & 1
            if (reg ^ b) & 1:
                reg = (reg >> 1) ^ 0xA001
            else:
                reg >>= 1
    return reg ^ 0xFFFF


def crc5_usb_bits(bits):
    """The five CRC-5/USB bits a USB transmitter sends after `bits`, in wire order."""
    reg = 0x1F
    for b in bits:
        if (reg ^ b) & 1:
            reg = (reg >> 1) ^ 0x14
        else:
            reg >>= 1
    reg ^= 0x1F
    return [(reg >> i) & 1 for i in range(5)]


def crc5_good(bits):
    """USB receive check: the last five bits are the CRC-5 of the bits before them."""
    return len(bits) >= 5 and crc5_usb_bits(bits[:-5]) == bits[-5:]


def crc16_good(data):
    return len(data) >= 2 and crc16_usb(data[:-2]) == (data[-2] | data[-1] << 8)


def crc32_good(data):
    return len(data) >= 4 and binascii.crc32(bytes(data[:-4])) == int.from_bytes(bytes(data[-4:]), "little")


def lsb_bits(data):
    return [(byte >> i) & 1 for byte in data for i in range(8)]


def bits_to_bytes(bits):
    assert len(bits) % 8 == 0, len(bits)
    return [sum(bits[8 * j + i] << i for i in range(8)) for j in range(len(bits) // 8)]


def test_protocol_helpers_check_values():
    # published check values of the CRC catalogue, message "123456789"
    assert crc16_usb(b"123456789") == 0xB4C8
    v = crc5_usb_bits(lsb_bits(b"123456789"))
    assert sum(b << i for i, b in enumerate(v)) == 0x19
    # SETUP to address 0, endpoint 0 is 2D 00 10: 11 zero bits, CRC-5 = 0b00010
    tok = lsb_bits([0x00, 0x10])
    assert tok[:11] == [0] * 11 and crc5_good(tok)
    assert crc32_good([1, 2, 3] + list(binascii.crc32(bytes([1, 2, 3])).to_bytes(4, "little")))


def usb_stuff(bits):
    """USB bit stuffing: a 0 after every six consecutive ones (counted from the
    start of SYNC). Returns the stuffed bits and the stuffed positions."""
    out, pos, ones = [], [], 0
    for b in bits:
        out.append(b)
        ones = ones + 1 if b else 0
        if ones == 6:
            pos.append(len(out))
            out.append(0)
            ones = 0
    return out, pos


def usb_unstuff(bits):
    out, pos, ones = [], [], 0
    for i, b in enumerate(bits):
        if ones == 6:
            assert b == 0, "seven ones on the wire at bit %d" % i
            pos.append(i)
            ones = 0
            continue
        out.append(b)
        ones = ones + 1 if b else 0
    return out, pos


J, K, SE0, SE1 = "J", "K", "0", "1"


def nrzi(bits):
    """NRZI from J: a 0 is a change, a 1 is none."""
    line, out = J, []
    for b in bits:
        if b == 0:
            line = K if line == J else J
        out.append(line)
    return out


def nrzi_decode(syms):
    prev, out = J, []
    for s in syms:
        out.append(1 if s == prev else 0)
        prev = s
    return out


def usb_packet_syms(data, stuff=True):
    """SYNC + data as line symbols, then the EOP (SE0 SE0 J)."""
    bits = lsb_bits([0x80] + list(data))
    if stuff:
        bits = usb_stuff(bits)[0]
    return nrzi(bits) + [SE0, SE0, J]


def manchester_halves(bits):
    """IEEE 802.3: first half the complement of the bit, second half the bit (as seen on P)."""
    out = []
    for b in bits:
        out += [1 - b, b]
    return out


# low speed: J is P (D+) low and N (D-) high
PN = dict([(J, (0, 1)), (K, (1, 0)), (SE0, (0, 0)), (SE1, (1, 1))])
SYM = dict((v, s) for s, v in PN.items())


def sym_at(pad, k):
    return SYM[((pad >> (2 * k)) & 1, (pad >> (2 * k + 1)) & 1)]


def with_pair(base, k, pv, nv):
    p = 2 * k
    v = base & ~(3 << p) & 0xFF
    return v | (pv << p) | (nv << (p + 1))


# ---------------------------------------------------------------- machine helpers

def asm(src):
    words, syms, _ = keyerasm.assemble(src)
    return keyerasm.to_list(words), syms


def boot(src, ext_uio=0xFF, t1=False):
    m = Machine(trace=True)
    words, syms = asm(src)
    m.load(words)
    m.ext_uio = ext_uio
    m.test_got = []                            # thread 0's outbox, drained by the host in run()
    m.host_run(0, True)
    if t1:
        m.host_set_pc(1, syms["t1"])
        m.host_run(1, True)
    return m, syms


def ser_dict(s):
    return dict((n, getattr(s, n)) for n in SER_NAMES)


class Rec:
    """Per-cycle record, taken before each step: the state during cycle c."""

    def __init__(self, cycle, m, ser):
        self.cycle = cycle
        self.pad = m.pad()
        self.uio_out, self.uio_oe, self.od_mask = m.uio_out, m.uio_oe, m.od_mask
        # 15.1: a tick of thread t's timer is a cycle with period != 0 and prescale = 0
        self.tick = tuple(int(t.period != 0 and t.prescale == 0) for t in m.threads)
        self.now = tuple(t.now for t in m.threads)
        self.deadline = tuple(t.deadline for t in m.threads)
        self.ser = ser_dict(m.ser) if ser else None


def run(m, cycles, ext=None, ui=None, ser=False, stop=None, recs=None):
    """Step `cycles` cycles. ext(c) / ui(c), if given, set the external uio /
    ui level during cycle c. Appends one record per cycle to `recs` (a fresh
    list by default) and returns it; a run from cycle 0 indexes it by cycle."""
    recs = [] if recs is None else recs
    for _ in range(cycles):
        c = m.cycle
        if ext is not None:
            m.ext_uio = ext(c)
        if ui is not None:
            m.ext_ui = ui(c)
        recs.append(Rec(c, m, ser))
        m.step()
        got = getattr(m, "test_got", None)
        while got is not None:
            v = m.host_outbox_pop(0)
            if v is None:
                break
            got.append(v)
        if stop is not None and stop(m):
            break
    return recs


def name_of(word):
    ins = isa.decode(word)[0]
    return ins.name if ins is not None else None


def done_cycles(m, name):
    """Cycles at which an instruction with this mnemonic completed."""
    return [r.cycle for r in m.trace if r.done and name_of(r.word) == name]


def changes(recs, mask):
    """Cycles at which pad & mask differs from the cycle before."""
    return [recs[i].cycle for i in range(1, len(recs)) if (recs[i].pad ^ recs[i - 1].pad) & mask]


def check_pin_events(m, recs):
    """pin_events, replayed, gives the drive state of every recorded cycle (13)."""
    ev = list(m.pin_events)
    state, j = (0, 0, 0), 0
    for r in recs:
        while j < len(ev) and ev[j][0] <= r.cycle:
            state = ev[j][1:]
            j += 1
        assert (r.uio_out, r.uio_oe) == state[:2], "pin_events disagree at cycle %d" % r.cycle


def off_state(rx_last=0):
    """The engine's registers while it is off (15.4); rx_last is level[P] (15.1)."""
    d = dict((n, OFF_VALUES.get(n, 0)) for n in SER_NAMES)
    d["rx_last"] = rx_last
    return d


# ---------------------------------------------------------------- reset, SERCFG

def test_reset_values():
    m = Machine()
    for n in SER_NAMES:
        v = getattr(m.ser, n)
        assert v == 0 and isinstance(v, int), n                  # 3.1
    m.step()
    # 15.4: from the end of cycle 0 on, rx_sh = 0xFF and rx_w = 1 while the engine is off;
    # rx_last(1) = level(0)[0] = 0 (level reads 0 in cycles 0 and 1)
    assert ser_dict(m.ser) == off_state(0)
    m.run(20)
    assert ser_dict(m.ser) == off_state(1)     # the pull-up on uio0, through the synchroniser


def test_sercfg_resets_mid_frame_overrides_tick_and_sets_owner():
    # T = 1: every cycle is a symbol tick, so the SERCFG of thread 1 lands in a
    # tick. The byte 0x00 in NRZI changes the line at every symbol.
    m, syms = boot("""
            ldi r1, 1
            sett r1
            ldi r0, 0x41            ; NRZI, pair 1
            sercfg r0
            ldi r2, 0
            sertx r2
            sertx r2
            sertx r2
            halt
    t1:     wt1 ui0
            ldi r0, 0x52            ; Manchester, receiver on, pair 1
            sercfg r0
            halt
    """, ext_uio=0xFB, t1=True)
    recs = run(m, 20, ser=True)
    run(m, 40, ser=True, ui=lambda c: 1, recs=recs)
    (c_cfg,) = [c for c in done_cycles(m, "SERCFG") if c & 1]
    r_before, r_cfg, r_after = recs[c_cfg - 1], recs[c_cfg], recs[c_cfg + 1]
    assert r_cfg.ser["tx_state"] == 1 and r_cfg.tick[0] == 1   # a data bit was due in this tick
    assert r_before.pad & 0x0C != r_cfg.pad & 0x0C            # the line was changing every cycle
    assert r_after.pad == r_cfg.pad                            # SERCFG: no engine pin write
    # rx_last(c + 1) = level(c)[P] with the P of the old configuration; level(c) = pad(c - 2)
    expect = off_state((recs[c_cfg - 2].pad >> 2) & 1)
    expect.update(cfg=0x52, owner=1)
    assert r_after.ser == expect
    # the pins are left as they were; the new owner's timer is off, so no ticks
    assert all(r.pad == r_after.pad for r in recs[c_cfg + 1:])


# ---------------------------------------------------------------- NRZI transmit

def _find_last_byte():
    """A last payload byte after [C3 FF] whose CRC-16 makes the stuffed packet
    end on a stuffed zero (six ones just before the EOP)."""
    for x in range(256):
        data = [0xC3, 0xFF, x]
        crc = crc16_usb(data[1:])
        stuffed, pos = usb_stuff(lsb_bits([0x80] + data + [crc & 0xFF, crc >> 8]))
        if pos and pos[-1] == len(stuffed) - 1:
            return x
    raise AssertionError("no payload byte found")


LAST = _find_last_byte()


def first_even_from(c):
    return c if c % 2 == 0 else c + 1


@pytest.mark.parametrize("T", [8, 5])
def test_nrzi_frame_stuffing_crc16_eop(T):
    k = 1
    p = 2 * k
    jlvl = with_pair(0xFF, k, 0, 1)            # bus idle J from the pull-ups: P low, N high
    m, _ = boot("""
            ldi r1, %d
            sett r1
            ldi r0, %d
            sercfg r0
            seri 0x80               ; SYNC
            seri 0xC3               ; DATA0 PID, not in the CRC
            seric 0xFF
            seric %d
            serwt
            halt
    """ % (T, NRZI | STUFF | pair(k), LAST), ext_uio=jlvl)
    recs = run(m, 200 * T + 200, stop=lambda mm: not mm.threads[0].running)
    check_pin_events(m, recs)
    ch = changes(recs, 3 << p)
    # every pad change is in the cycle after a tick of the owner's timer (15.3)
    assert ch and all(recs[e - 1].tick[0] for e in ch)
    # the first symbol is on the pads in the cycle after the second tick at which tx_full was seen
    q = done_cycles(m, "SERI")[0]
    ticks = [r.cycle for r in recs if r.cycle > q and r.tick[0]]
    f = ticks[1] + 1
    assert ch[0] == f
    assert sym_at(recs[f - 1].pad, k) == J
    # independent decoder: one symbol per T cycles from f
    syms = []
    while True:
        i = len(syms)
        s = sym_at(recs[f + i * T].pad, k)
        assert all(sym_at(recs[f + i * T + j].pad, k) == s for j in range(T))
        if s == SE0:
            break
        syms.append(s)
    n = len(syms)
    assert all(sym_at(recs[f + (n + 1) * T + j].pad, k) == SE0 for j in range(T))
    assert all(sym_at(recs[f + (n + 2) * T + j].pad, k) == J for j in range(T))
    rel = f + (n + 3) * T                      # released after one J period
    assert (recs[rel - 1].uio_oe >> p) & 3 == 3
    assert all((r.uio_oe >> p) & 3 == 0 and sym_at(r.pad, k) == J for r in recs[rel:])
    assert syms[:8] == list("KJKJKJKK")        # SYNC from the byte 0x80
    bits, stuffed = usb_unstuff(nrzi_decode(syms))
    assert stuffed[-1] == len(syms) - 1        # a stuffed zero just before the EOP
    assert len(stuffed) >= 2                   # and one inside the frame (C3 FF)
    data = bits_to_bytes(bits)
    assert data[:4] == [0x80, 0xC3, 0xFF, LAST] and len(data) == 6
    assert crc16_usb(data[2:4]) == data[4] | data[5] << 8
    # SERWT completes at the first slot of the thread after the last tail tick
    (w,) = done_cycles(m, "SERWT")
    assert w == first_even_from(rel)


def test_nrzi_unmarked_frame_has_no_crc_and_owner_thread1_timer():
    # thread 1 owns the engine: its timer gives the ticks, thread 0's timer is ignored
    T = 7
    k = 3
    p = 2 * k
    m, _ = boot("""
            ldi r1, 3
            sett r1                 ; thread 0's timer: not the owner
            halt
    t1:     ldi r1, %d
            sett r1
            ldi r0, %d
            sercfg r0
            seri 0x80
            seri 0xFC               ; ends with six ones: stuffed zero before the EOP
            serwt
            halt
    """ % (T, NRZI | STUFF | pair(k)), ext_uio=with_pair(0xFF, k, 0, 1), t1=True)
    recs = run(m, 40 * T + 200, stop=lambda mm: not mm.threads[1].running)
    ch = changes(recs, 3 << p)
    assert ch and all(recs[e - 1].tick[1] for e in ch)
    q = done_cycles(m, "SERI")[0]
    ticks = [r.cycle for r in recs if r.cycle > q and r.tick[1]]
    f = ticks[1] + 1
    assert ch[0] == f
    syms = [sym_at(recs[f + i * T].pad, k) for i in range(18)]
    assert syms[-1] == SE0 and SE0 not in syms[:-1]
    bits, stuffed = usb_unstuff(nrzi_decode(syms[:-1]))
    assert stuffed == [16] and bits_to_bytes(bits) == [0x80, 0xFC]
    assert sym_at(recs[f + 18 * T].pad, k) == SE0 and sym_at(recs[f + 19 * T].pad, k) == J
    assert (recs[f + 20 * T - 1].uio_oe >> p) & 3 == 3 and (recs[f + 20 * T].uio_oe >> p) & 3 == 0


def test_nrzi_crc_covers_exactly_the_marked_bytes():
    # unmarked, marked, unmarked: the CRC-16 of the middle byte alone is appended
    T, k = 4, 0
    m, _ = boot("""
            ldi r1, %d
            sett r1
            ldi r0, %d
            sercfg r0
            seri 0x80
            seric 0xA5
            seri 0x3C
            serwt
            halt
    """ % (T, NRZI | STUFF | pair(k)), ext_uio=with_pair(0xFF, k, 0, 1))
    recs = run(m, 100 * T, stop=lambda mm: not mm.threads[0].running)
    f = changes(recs, 3)[0]
    syms = []
    while sym_at(recs[f + len(syms) * T].pad, k) != SE0:
        syms.append(sym_at(recs[f + len(syms) * T].pad, k))
    data = bits_to_bytes(usb_unstuff(nrzi_decode(syms))[0])
    crc = crc16_usb([0xA5])
    assert data == [0x80, 0xA5, 0x3C, crc & 0xFF, crc >> 8]


# ---------------------------------------------------------------- Manchester transmit

PREAMBLE = [0x55] * 7 + [0xD5]


@pytest.mark.parametrize("T", [2, 3])
def test_manchester_frame_crc32_tail(T):
    k = 0
    payload = [0xFF, 0xFF, 0x02, 0x00, 0x5E, 0x10, 0x08, 0x00, 0xA5]
    src = ["ldi r1, %d" % T, "sett r1", "oen uio0", "oen uio1",       # idle: both driven low
           "ldi r0, %d" % (MANCH | CRC32 | pair(k)), "sercfg r0"]
    src += ["seri 0x%02X" % b for b in PREAMBLE]
    src += ["seric 0x%02X" % b for b in payload]
    src += ["serwt", "halt"]
    m, _ = boot("\n".join(src), ext_uio=0xFC)
    recs = run(m, 2 * T * 8 * 40 + 200, stop=lambda mm: not mm.threads[0].running)
    check_pin_events(m, recs)
    ch = changes(recs, 3)
    assert ch and all(recs[e - 1].tick[0] for e in ch)
    q = done_cycles(m, "SERI")[0]
    ticks = [r.cycle for r in recs if r.cycle > q and r.tick[0]]
    f = ticks[1] + 1                           # first half of the first bit
    assert ch[0] == f and recs[f - 1].pad & 3 == 0
    nbits = 8 * (len(PREAMBLE) + len(payload) + 4)
    halves = []
    for i in range(2 * nbits):
        pv = recs[f + i * T].pad & 3
        assert pv in (1, 2)                    # N = ~P during the frame
        assert all(recs[f + i * T + j].pad & 3 == pv for j in range(T))
        halves.append(pv & 1)
    bits = []
    for i in range(nbits):
        a, b = halves[2 * i], halves[2 * i + 1]
        assert a != b                          # a mid-bit transition in every bit
        bits.append(b)                         # low then high is a 1
    data = bits_to_bytes(bits)
    assert data[:8] == PREAMBLE and data[8:-4] == payload
    assert int.from_bytes(bytes(data[-4:]), "little") == binascii.crc32(bytes(payload))
    tail = f + 2 * nbits * T
    assert all(recs[tail + j].pad & 3 == 1 for j in range(6 * T))   # P high, N low for six ticks
    end = tail + 6 * T                         # then both driven low
    assert all(r.pad & 3 == 0 and r.uio_oe & 3 == 3 for r in recs[end:])
    (w,) = done_cycles(m, "SERWT")
    assert w == first_even_from(end)


# ---------------------------------------------------------------- receive

RX_LOOP = """
loop:   serrx r2
        beq fend
        push r2
        bra loop
fend:   ldi r3, 0xEE
        push r3
        push r2
        swap r2
        push r2
        bra loop
"""


def rx_firmware(T, cfg):
    return """
            ldi r1, %d
            sett r1
            ldi r0, %d
            sercfg r0
    """ % (T, cfg) + RX_LOOP


def outbox(m):
    return getattr(m, "test_got", []) + list(m.threads[0].outbox)


def drive_syms(syms, k, T, c0, base=0xFF):
    """ext_uio(c) for a symbol stream starting at cycle c0, J before and after."""
    levels = [with_pair(base, k, *PN[s]) for s in syms]
    idle = with_pair(base, k, *PN[J])

    def ext(c):
        i = (c - c0) // T
        return levels[i] if 0 <= i < len(levels) else idle
    return ext


def frames_syms(frames, gap=12):
    out = [J] * gap
    for fr in frames:
        out += fr + [J] * gap
    return out


def split_frames(box):
    """Outbox bytes of RX_LOOP -> list of (bytes, status)."""
    res, cur, i = [], [], 0
    while i < len(box):
        if box[i] == 0xEE and i + 2 < len(box):
            res.append((cur, box[i + 1] | box[i + 2] << 8))
            cur = []
            i += 3
        else:
            cur.append(box[i])
            i += 1
    assert not cur, cur
    return res


def test_nrzi_receive_token_and_data_crc_verdicts():
    T, k = 8, 0
    token = [0x2D, 0x00, 0x10]                 # SETUP, address 0, endpoint 0
    data_pkt = [0xC3, 0xFF, 0x01, 0x80]
    crc = crc16_usb(data_pkt[1:])
    data_pkt += [crc & 0xFF, crc >> 8]
    bad_token = [0x2D, 0x01, 0x10]             # wrong CRC-5
    syms = frames_syms([usb_packet_syms(token), usb_packet_syms(data_pkt), usb_packet_syms(bad_token)])
    c0 = 40
    m, _ = boot(rx_firmware(T, NRZI | STUFF | RXEN | RXSKIP | pair(k)))
    recs = run(m, c0 + len(syms) * T + 100, ext=drive_syms(syms, k, T, c0), ser=True)
    got = split_frames(outbox(m))
    assert [g[0] for g in got] == [token, data_pkt, bad_token]
    for pkt, (_, st) in zip([token, data_pkt, bad_token], got):
        body = pkt[1:]                         # RXSKIP: the PID is not in the CRCs
        exp = ST_END
        exp |= ST_C5OK if crc5_good(lsb_bits(body)) else 0
        exp |= ST_COK if crc16_good(body) else 0
        assert st == exp, (pkt, hex(st))
    assert got[0][1] & ST_C5OK and got[1][1] & ST_COK and not got[2][1] & ST_C5OK
    # end of packet: rx_end rises in the cycle after the first sample that sees
    # SE0, samples being 1 + (T >> 1) cycles after a change of P is seen and
    # then every T cycles (15.6)

    def lvl(c):
        return recs[c - 2].pad if c >= 2 else 0          # level(c) = pad(c - 2)
    first_end = next(r.cycle for r in recs if r.ser["rx_end"])
    smp, nxt = None, None
    for c in range(c0, first_end + 1):
        if (lvl(c) ^ lvl(c - 1)) & 1:
            nxt = c + 1 + (T >> 1)
        elif c == nxt:
            if lvl(c) & 3 == 0:
                smp = c
                break
            nxt = c + T
    assert smp is not None and first_end == smp + 1
    # docs/spec-questions.md Q21 (reading chosen): a frame start does not clear
    # the CRC verdicts, so during the data packet they still show the token's
    in_second = [r.ser for r in recs if r.cycle > first_end and r.ser["rx_state"] == 1]
    in_second = in_second[:len(in_second) // 2]
    assert in_second and all(s["rx_c5ok"] == 1 and s["rx_end"] == 0 for s in in_second)


def test_nrzi_receive_stuffing_error_and_short_frame():
    T, k = 6, 2
    # seven ones after the PID with no stuffed zero
    bad = nrzi(lsb_bits([0x80, 0xC3]) + [1] * 8 + lsb_bits([0x00])) + [SE0, SE0, J]
    # SYNC, PID, then three bits and the EOP: the frame ends off a byte boundary
    short = nrzi(usb_stuff(lsb_bits([0x80, 0x2D]) + [1, 0, 1])[0]) + [SE0, SE0, J]
    good = usb_packet_syms([0xD2])             # ACK: a clean frame clears the error bits
    # the trailing 1 of SYNC counts: 0x3F needs a stuffed zero after its fifth bit
    early = usb_packet_syms([0x3F, 0x00])
    assert len(early) == 8 + 17 + 3
    syms = frames_syms([bad, short, good, early])
    c0 = 40
    m, _ = boot(rx_firmware(T, NRZI | STUFF | RXEN | RXSKIP | pair(k)))
    run(m, c0 + len(syms) * T + 100, ext=drive_syms(syms, k, T, c0))
    got = split_frames(outbox(m))
    assert len(got) == 4
    assert got[3][0] == [0x3F, 0x00] and not got[3][1] & (ST_SERR | ST_FERR)
    assert got[0][0][0] == 0xC3 and got[0][1] & ST_SERR
    assert got[1][0] == [0x2D] and got[1][1] & ST_FERR and not got[1][1] & ST_SERR
    assert got[2][0] == [0xD2] and not got[2][1] & (ST_SERR | ST_FERR | ST_OVR)


def test_nrzi_receive_overrun_serrxt_bytes_then_status():
    T, k = 8, 0
    token = [0x2D, 0x00, 0x10]
    syms = frames_syms([usb_packet_syms(token)])
    c0 = 40
    m, _ = boot("""
            ldi r1, %d
            sett r1
            ldi r0, %d
            sercfg r0
            wt1 ui0                 ; wait until the frame is over: two bytes are lost
            setc
            setd 20
            serrxt r2               ; the held byte: Z = 0, C = 0
            bne ok1
            ldi r7, 0xB1
    ok1:    bcc ok2
            ldi r7, 0xB2
    ok2:    setc
            setd 20
            serrxt r3               ; the frame end: Z = 1, C = 0, r3 = status
            beq ok3
            ldi r7, 0xB3
    ok3:    bcc ok4
            ldi r7, 0xB4
    ok4:    halt
    """ % (T, NRZI | STUFF | RXEN | RXSKIP | pair(k)))
    run(m, c0 + len(syms) * T + 20, ext=drive_syms(syms, k, T, c0))
    s = m.ser
    assert (s.rx_hold, s.rx_valid, s.rx_ovr, s.rx_end) == (0x2D, 1, 1, 1)
    # a second frame: its start drops the byte still held (rx_valid <= 0), so
    # its own first byte is kept; its second and third bytes are lost again
    syms2 = frames_syms([usb_packet_syms([0x69, 0x00, 0x10])])   # IN, address 0, endpoint 0
    c1 = m.cycle
    run(m, len(syms2) * T + 20, ext=drive_syms(syms2, k, T, c1))
    assert (s.rx_hold, s.rx_valid, s.rx_ovr, s.rx_end) == (0x69, 1, 1, 1)
    run(m, 400, ui=lambda c: 1, stop=lambda mm: not mm.threads[0].running)
    t = m.threads[0]
    assert t.regs[7] == 0
    assert t.regs[2] == 0x69
    assert t.regs[3] == ST_END | ST_OVR | ST_C5OK   # CRC-5 still checks the bits of the lost bytes
    assert (s.rx_valid, s.rx_end) == (0, 0)


@pytest.mark.parametrize("T", [4, 6, 2])
def test_manchester_receive_crc32_and_idle_timeout(T):
    k = 2
    p = 2 * k
    payload = [0x01, 0x02, 0x03, 0xF0, 0x0F, 0x80]
    fcs = list(binascii.crc32(bytes(payload)).to_bytes(4, "little"))
    halves = manchester_halves(lsb_bits(PREAMBLE + payload + fcs))
    c0 = 50

    def ext(c):
        i = (c - c0) // T
        pv = 0 if i < 0 else halves[min(i, len(halves) - 1)]   # the line then stays put
        return with_pair(0xFF, k, pv, 1 - pv)
    m, _ = boot(rx_firmware(T, MANCH | CRC32 | RXEN | pair(k)))
    recs = run(m, c0 + len(halves) * T + 20 * T, ext=ext, ser=True)
    got = split_frames(outbox(m))
    assert len(got) == 1
    body, st = got[0]
    assert body == payload + fcs               # the preamble and SFD are not delivered
    assert crc32_good(body)
    assert st == ST_END | ST_COK | (ST_C5OK if crc5_good(lsb_bits(body)) else 0)
    # the idle timeout: the last mid-bit change of P is on the pads in cycle e
    # and seen in cycle e + 2; the receiver ignores edges up to and including
    # e + 2 + T + (T >> 1) - 1 and ends the frame if none comes for a further
    # 2 T cycles (15.6), so rx_end reads 1 from the cycle after that
    e = max(changes(recs, 1 << p))
    first_end = next(r.cycle for r in recs if r.ser["rx_end"])
    assert first_end == (e + 2) + T + (T >> 1) - 1 + 2 * T + 1


def test_receiver_held_while_transmitter_busy():
    T, k = 4, 0
    m, _ = boot("""
            ldi r1, %d
            sett r1
            ldi r0, %d
            sercfg r0
            seri 0x80
            seri 0x00
            serwt
            halt
    """ % (T, NRZI | STUFF | RXEN | pair(k)), ext_uio=with_pair(0xFF, k, 0, 1))
    recs = run(m, 400, ser=True, stop=lambda mm: not mm.threads[0].running)
    busy = [i for i in range(len(recs) - 1) if recs[i].ser["tx_state"] != 0]
    assert len(busy) == 20 * T                 # 16 data ticks (80 00 needs no stuffing), 4 tail ticks
    held = dict(rx_state=0, rx_sh=0xFF, rx_n=0, rx_ones=0, rx_psym=0, rx_cnt=0, rx_w=1, rx_first=0)
    for i in busy:
        s = recs[i + 1].ser
        assert dict((n, s[n]) for n in held) == held, i
        assert s["rx_valid"] == 0 and s["rx_end"] == 0


@pytest.mark.parametrize("mode,stuff", [(NRZI, STUFF), (MANCH, 0), (MANCH, STUFF)])
def test_tx_rx_round_trip_two_machines(mode, stuff):
    """One machine transmits, a second receives its pads."""
    if mode == NRZI:
        T, k, cfg_tx, cfg_rx = 8, 1, NRZI | stuff, NRZI | stuff | RXEN | RXSKIP
        pre, data = [0x80, 0xC3], [0xFF, LAST]
    else:
        T, k, cfg_tx, cfg_rx = 2, 0, MANCH | CRC32 | stuff, MANCH | CRC32 | stuff | RXEN
        # 0x0F after the SFD (which ends in two ones): stuffed after its fourth bit
        pre, data = PREAMBLE, [0x0F, 0x20, 0xFF, 0x00]
    src = ["ldi r1, %d" % T, "sett r1"]
    if mode == MANCH:
        src += ["oen uio%d" % (2 * k), "oen uio%d" % (2 * k + 1)]
    src += ["ldi r0, %d" % (cfg_tx | pair(k)), "sercfg r0", "delay 20"]
    src += ["seri 0x%02X" % b for b in pre] + ["seric 0x%02X" % b for b in data]
    src += ["serwt", "halt"]
    a, _ = boot("\n".join(src), ext_uio=with_pair(0xFF, k, 0, 1))
    b, _ = boot(rx_firmware(T, cfg_rx | pair(k)))
    mask = 3 << (2 * k)
    for _ in range(3000):
        b.ext_uio = (0xFF & ~mask) | (a.pad() & mask)
        a.step()
        run(b, 1)
    got = split_frames(outbox(b))
    assert len(got) == 1
    body, st = got[0]
    if mode == NRZI:
        crc = crc16_usb(data)
        assert body == [0xC3] + data + [crc & 0xFF, crc >> 8]
    else:
        assert body == data + list(binascii.crc32(bytes(data)).to_bytes(4, "little"))
    assert st & (ST_END | ST_COK | ST_SERR | ST_FERR | ST_OVR) == ST_END | ST_COK


# ---------------------------------------------------------------- instructions

def _flags(tag):
    """Push (Z << 2) | C of the moment; clobbers r4, Z and C."""
    return """
            ldi r4, 0
            bne nz_%s
            ldi r4, 2
    nz_%s:  rcl r4
            push r4
    """ % (tag, tag)


def test_timeout_forms_and_status():
    m, _ = boot("""
            ldi r1, 4
            sett r1
            ldi r0, 0
            sercfg r0               ; mode 0: nothing ever drains or arrives
            ldi r2, 0x5A
            ldi r3, 0x77
            cmpi r2, 0              ; Z = 0, C = 0
            setd 3
            serrxt r3               ; times out: C = 1, Z and r3 unchanged
    """ + _flags("a") + """
            push r3
            sertx r2                ; tx_full = 1, stays
            serst r5
            push r5
            cmpi r2, 0x5A           ; Z = 1, C = 0
            setd 2
            sertxt r3               ; times out
    """ + _flags("b") + """
            cmpi r2, 0x5A
            setd 2
            sertxct r3              ; times out
    """ + _flags("c") + """
            cmpi r2, 0x5A
            setd 2
            serwtt                  ; times out: holding register full
    """ + _flags("d") + """
            sercfg r0               ; empties it
            cmpi r2, 0              ; Z = 0
            setc
            setd 0
            serwtt                  ; idle and empty: C = 0 although the deadline is reached
    """ + _flags("e") + """
            cmpi r2, 0x5A           ; Z = 1
            setc
            sertxct r2              ; room: C = 0, marked
    """ + _flags("f") + """
            serst r5
            push r5
            halt
    """)
    recs = run(m, 3000, stop=lambda mm: not mm.threads[0].running)
    assert outbox(m) == [0b001, 0x77, ST_TXFULL, 0b101, 0b101, 0b101, 0b000, 0b100, ST_TXFULL]
    assert (m.ser.tx_hold, m.ser.tx_hold_c, m.ser.tx_full) == (0x5A, 1, 1)
    # the first timeout completes at the first slot at which NOW has reached DEADLINE (7.4)
    first = next(r for r in m.trace if name_of(r.word) == "SERRXT")
    done = next(r for r in m.trace if name_of(r.word) == "SERRXT" and r.done)
    slots = list(range(first.cycle, done.cycle + 1, 2))
    reached = [((recs[c].now[0] - recs[c].deadline[0]) & 0xFFFF) < 0x8000 for c in slots]
    assert len(slots) > 2 and reached == [False] * (len(slots) - 1) + [True]


def test_sertx_takes_rs_low_byte_and_seric_the_immediate():
    m, _ = boot("""
            ldi r2, 0xA3
            ldih r2, 0x01
            sertx r2
            halt
    """)
    run(m, 20)
    assert (m.ser.tx_hold, m.ser.tx_hold_c, m.ser.tx_full) == (0xA3, 0, 1)
    m, _ = boot("""
            seric 0x9C
            halt
    """)
    run(m, 20)
    assert (m.ser.tx_hold, m.ser.tx_hold_c, m.ser.tx_full) == (0x9C, 1, 1)


def test_undefined_serializer_function_is_a_nop():
    words = [isa.encode("LDI", rd=0, imm=0x11), isa.encode("LDI", rd=1, imm=0x2E)]
    base = isa.encode("SERCFG", rs=1) & ~0x1F         # major E, sub-opcode 15, r = 1, t = 0, g = 0
    for g in range(6, 16):
        for t in (0, 1):
            words.append(base | (t << 4) | g)
    words.append(isa.encode("HALT"))
    m = Machine(trace=True)
    m.load(words)
    m.host_run(0, True)
    run(m, 200, stop=lambda mm: not mm.threads[0].running)
    assert all(r.done for r in m.trace)
    assert m.threads[0].pc == len(words)
    assert m.threads[0].regs[:2] == [0x11, 0x2E]
    assert ser_dict(m.ser) == off_state(1)


@pytest.mark.parametrize("mode", [0, 3])
def test_mode_off_does_nothing(mode):
    T, k = 4, 0
    syms = frames_syms([usb_packet_syms([0x2D, 0x00, 0x10])])
    c0 = 40
    m, _ = boot("""
            ldi r1, %d
            sett r1
            ldi r0, %d
            sercfg r0
            seri 0x80
            serst r5
            push r5
            serwt                   ; never completes
            halt
    """ % (T, mode | STUFF | CRC32 | RXEN | RXSKIP | pair(k)))
    recs = run(m, c0 + len(syms) * T + 100, ext=drive_syms(syms, k, T, c0), ser=True)
    assert m.pin_events == [] and all(r.uio_oe == 0 for r in recs)
    assert outbox(m) == [ST_TXFULL]
    assert m.threads[0].running and m.threads[0].blocked
    c_cfg = done_cycles(m, "SERCFG")[0]
    for r in recs[c_cfg + 1:]:
        s = r.ser
        assert s["tx_state"] == 0 and s["rx_state"] == 0 and s["rx_sh"] == 0xFF and s["rx_cnt"] == 0
        assert s["rx_valid"] == 0 and s["rx_end"] == 0 and s["crc_m"] == 0 and s["crc5"] == 0
    # rx_last follows level(c)[P] in every cycle, whatever the mode (15.1)
    for i in range(3, len(recs)):
        assert recs[i].ser["rx_last"] == recs[i - 3].pad & 1


@pytest.mark.parametrize("pull_low", [False, True])
def test_open_drain_pin_ignores_the_engine(pull_low):
    # P open-drain from the host (released, or pulled low by firmware): the
    # engine drives N only and never writes P, the release included.
    T, k = 4, 0
    m, _ = boot("""
            ldi r1, %d
            sett r1
            ldi r0, %d
            %s
            sercfg r0
            seri 0x80
            seri 0x00
            serwt
            halt
    """ % (T, NRZI | pair(k), "clr uio0" if pull_low else "nop"), ext_uio=0xFE)
    m.host_pinmode(0x01)
    recs = run(m, 400, stop=lambda mm: not mm.threads[0].running)
    oe_p = 1 if pull_low else 0
    c_cfg = done_cycles(m, "SERCFG")[0]
    assert all(r.uio_out & 1 == 0 and r.uio_oe & 1 == oe_p for r in recs[c_cfg:])
    assert m.uio_oe & 1 == oe_p
    assert all(r.uio_out & r.od_mask == 0 for r in recs)
    assert len(changes(recs, 2)) > 8           # N carries the frame
    assert recs[-1].uio_oe & 2 == 0            # and N is released after the tail


def test_od_instruction_masks_engine_write_in_the_same_cycle():
    # T = 1: every cycle is a tick and the byte 0x00 changes the line every
    # cycle. An OD on N committed in cycle c masks the engine's write to N in c
    # (the engine uses od_mask after the core's command, 5.3).
    m, _ = boot("""
            ldi r1, 1
            sett r1
            ldi r0, %d
            sercfg r0
            ldi r2, 0
            sertx r2
            sertx r2
            nop
            nop
            od uio1
            sertx r2
            halt
    """ % (NRZI | pair(0)), ext_uio=0xFE)
    recs = run(m, 60, stop=lambda mm: not mm.threads[0].running)
    (c,) = done_cycles(m, "OD")
    assert recs[c].uio_oe & 3 == 3
    assert all(r.uio_oe & 2 == 0 and r.uio_out & 2 == 0 for r in recs[c + 1:])
    assert (recs[c + 1].uio_out ^ recs[c].uio_out) & 1    # P still follows the engine
