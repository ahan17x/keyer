"""Serializer scenarios shared by the model tests (tools/test_ser_scenarios.py)
and the lockstep tests (test/test_ser.py): firmware, line stimulus and checks.

A scenario is (words, models, cycles, check). `models` are objects with
on_cycle(m), called before every cycle with the golden model, as the
lockstep harness does (test/keyer_tb.py): they set m.ext_uio for the cycle
and may record m.pad(). `check(m, models)` runs after the last cycle and
asserts on what the models saw and on thread 0's outbox bytes, which the
caller collects into `got`.

The line coders, decoders and CRCs come from tools/test_iss_ser.py, where
they are checked against published values; they know the protocols and
nothing about the engine.
"""

import binascii
import random

import keyer_isa as isa
import keyerasm
from test_iss_ser import (J, K, SE0, PN, NRZI, MANCH, STUFF, CRC32, RXEN, RXSKIP,
                          ST_END, ST_C5OK, ST_COK, ST_OVR, ST_SERR, ST_FERR,
                          crc16_usb, crc5_good, crc16_good, crc32_good, lsb_bits, bits_to_bytes,
                          usb_unstuff, nrzi_decode, usb_packet_syms, manchester_halves,
                          with_pair, sym_at, pair)

PREAMBLE = [0x55] * 7 + [0xD5]

# bytes and then the 16-bit status of every frame go to the outbox, the
# status after an 0xEE marker (the convention of tools/test_iss_ser.py)
RX_LOOP = """
rxl:    serrx r2
        beq   rxend
        push  r2
        bra   rxl
rxend:  ldi   r3, 0xEE
        push  r3
        push  r2
        swap  r2
        push  r2
        bra   rxl
"""


def asm(src):
    words, syms, _ = keyerasm.assemble(src)
    words = keyerasm.to_list(words)
    used = max(i for i, w in enumerate(words) if w) + 1
    return words[:used], syms


def split_frames(box):
    res, cur, i = [], [], 0
    while i < len(box):
        if box[i] == 0xEE and i + 2 < len(box):
            res.append((cur, box[i + 1] | box[i + 2] << 8))
            cur = []
            i += 3
        else:
            cur.append(box[i])
            i += 1
    return res, cur


class PairLog:
    """Records the pair's pad symbol and drive state for every cycle."""

    def __init__(self, k):
        self.k, self.pads, self.oe, self.c0 = k, [], [], None

    def on_cycle(self, m):
        if self.c0 is None:
            if not m.threads[0].running:        # the program is still being loaded
                return
            self.c0 = m.cycle
        self.pads.append((m.pad() >> (2 * self.k)) & 3)
        self.oe.append((m.uio_oe >> (2 * self.k)) & 3)

    def runs(self):
        """[(first cycle, length, pad pair value, oe pair value)] of constant stretches."""
        out = []
        for i, (p, o) in enumerate(zip(self.pads, self.oe)):
            if out and out[-1][2] == p and out[-1][3] == o:
                out[-1][1] += 1
            else:
                out.append([self.c0 + i, 1, p, o])
        return [tuple(r) for r in out]


class LineDriver:
    """Drives the pair from outside with a list of (P, N) levels, T cycles
    each, starting `delay` cycles after thread 0 starts; `idle` before and after. Other uio pins stay pulled up."""

    def __init__(self, k, levels, T, delay, idle):
        self.k, self.levels, self.T, self.delay, self.idle = k, levels, T, delay, idle
        self.c0 = None

    def on_cycle(self, m):
        if self.c0 is None and m.threads[0].running:
            self.c0 = m.cycle + self.delay      # counted from the start of thread 0
        i = -1 if self.c0 is None else (m.cycle - self.c0) // self.T
        pv, nv = self.levels[i] if 0 <= i < len(self.levels) else self.idle
        m.ext_uio = with_pair(0xFF, self.k, pv, nv)

    def done(self, m):
        return self.c0 is not None and m.cycle > self.c0 + len(self.levels) * self.T


# ------------------------------------------------------------------ transmit

def nrzi_tx(T=4, k=1):
    """A USB-shaped frame: SYNC and PID outside the CRC, a payload that needs
    stuffing inside the data and again in the CRC's neighbourhood, CRC-16 and
    the end of packet from the engine, then a second frame after SERWT."""
    payload = [0xFF, 0xFF, 0x3F, 0x00, 0xFE, 0xFF]
    src = """
        ldi r1, %d
        sett r1
        ldi r0, %d
        sercfg r0
        seri 0x80
        seri 0xC3
    """ % (T, NRZI | STUFF | pair(k))
    src += "".join("        seric %d\n" % b for b in payload)
    src += """
        serwt
        setd 0
        waitd 3
        seri 0x80
        seri 0xD2
        serwt
        ldi r5, 0xA5
        push r5
        halt
    """
    words, _ = asm(src)
    log = PairLog(k)

    def check(m, got):
        assert got == [0xA5], got
        frames = decode_nrzi_frames(log, T)
        assert len(frames) == 2, frames
        crc = crc16_usb(payload)
        assert frames[0] == [0x80, 0xC3] + payload + [crc & 0xFF, crc >> 8], [hex(b) for b in frames[0]]
        assert frames[1] == [0x80, 0xD2]
    return words, [log], 60 + T * 8 * 22, check


def decode_nrzi_frames(log, T):
    """Frames driven on the pair: every driven stretch must be a whole
    number of symbol periods; a frame is K/J symbols, then SE0 for exactly
    two periods, J for one, then released. Returns the unstuffed bytes."""
    frames, syms = [], []
    for c, n, pad, oe in log.runs():
        if oe != 3:
            assert oe == 0, "pair half driven at cycle %d" % c
            if syms:
                assert syms[-3:] == [SE0, SE0, J], (c, syms[-4:])
                bits, _ = usb_unstuff(nrzi_decode(syms[:-3]))
                frames.append(bits_to_bytes(bits))
                syms = []
            continue
        assert n % T == 0, "a symbol of %d cycles at cycle %d (T = %d)" % (n, c, T)
        s = {(0, 1): J, (1, 0): K, (0, 0): SE0}[(pad & 1, pad >> 1)]
        syms += [s] * (n // T)
    assert not syms, "pair still driven at the end"
    return frames


def manchester_tx(T=2, k=0):
    """An Ethernet-shaped frame: preamble and SFD outside the CRC, 60 bytes
    inside (some from a register), CRC-32 and the start of idle from the
    engine."""
    head = [0xFF] * 6 + [0x02, 0x00, 0x00, 0xC0, 0xFF, 0xEE, 0x08, 0x00]
    src = """
        clr %d
        clr %d
        oen %d
        oen %d
        ldi r1, %d
        sett r1
        ldi r0, %d
        sercfg r0
        ldi r2, 7
pre:    seri 0x55
        djnz r2, pre
        seri 0xD5
    """ % (2 * k, 2 * k + 1, 2 * k, 2 * k + 1, T, MANCH | CRC32 | pair(k))
    src += "".join("        seric %d\n" % b for b in head)
    src += """
        ldi r2, 46
        ldi r3, 0x10
pay:    sertxc r3
        addi r3, 3
        djnz r2, pay
        serwt
        ldi r5, 0x5A
        push r5
        halt
    """
    words, _ = asm(src)
    payload = [(0x10 + 3 * i) & 0xFF for i in range(46)]
    log = PairLog(k)

    def check(m, got):
        assert got == [0x5A], got
        runs = [r for r in log.runs() if r[3] == 3]
        assert all(r[3] == 3 or r[0] < log.c0 + 20 for r in log.runs()[1:]), "pair released after start-up"
        # the frame is everything between the first rise of P and the final both-low
        first = next(i for i, r in enumerate(runs) if r[2] == 1)
        body = runs[first:]
        assert body[-1][2] == 0, "the pair must end driven low (idle)"
        halves = []
        for c, n, pad, _ in body[:-1]:
            assert pad in (1, 2), "illegal pair state %d at cycle %d" % (pad, c)
            assert n % T == 0, (c, n)
            halves += [pad & 1] * (n // T)
        # the tail: P high for six periods after the last half-bit
        data = PREAMBLE + head + payload
        fcs = list(binascii.crc32(bytes(head + payload)).to_bytes(4, "little"))
        want = manchester_halves(lsb_bits(data + fcs))
        # the first half of the first bit is low, like the idle before it
        assert halves[:len(want) - 1] == want[1:], "Manchester stream differs"
        assert halves[len(want) - 1:] == [1] * 6, halves[len(want) - 1:]
    return words, [log], 80 + T * 2 * 8 * 82, check


# ------------------------------------------------------------------- receive

def nrzi_rx(T=8, k=0, gap=6):
    """`gap` is the idle time between packets in bit times: a host that drains
    the outbox over SPI needs a long one, or the 16-byte outbox fills, the
    thread blocks in PUSH and the receiver overruns."""
    token = [0x2D, 0x00, 0x10]
    data = [0xC3, 0xFF, 0xFF, 0x01, 0x80, 0x7E, 0xFF, 0xFF, 0xFF]
    crc = crc16_usb(data[1:])
    data += [crc & 0xFF, crc >> 8]
    bad = [0x4B, 0x12, 0x34, 0x00, 0x00]
    seven = usb_packet_syms([0xA5, 0xFF], stuff=False)       # seven ones in a row on the wire
    short = usb_packet_syms([0x69, 0x0F])
    short = short[:-3 - 3] + [SE0, SE0, J]                   # three bits missing
    frames = [usb_packet_syms(token), usb_packet_syms(data), usb_packet_syms(bad), seven, short,
              usb_packet_syms(token)]
    syms = [J] * 10
    for f in frames:
        syms += f + [J] * gap
    words, _ = asm("""
        ldi r1, %d
        sett r1
        ldi r0, %d
        sercfg r0
    """ % (T, NRZI | STUFF | RXEN | RXSKIP | pair(k)) + RX_LOOP)
    drv = LineDriver(k, [PN[s] for s in syms], T, 30, PN[J])

    def check(m, got):
        res, rest = split_frames(got)
        assert not rest, rest
        assert len(res) == 6, res
        assert res[0] == (token, ST_END | ST_C5OK), res[0]
        assert res[1][0] == data and res[1][1] & (ST_END | ST_COK) == ST_END | ST_COK, res[1]
        assert res[2][0] == bad and not res[2][1] & ST_COK, res[2]
        assert res[3][1] & ST_SERR, res[3]
        assert res[4][1] & ST_FERR, res[4]
        assert res[5] == (token, ST_END | ST_C5OK), res[5]
        for _, st in res:
            assert not st & ST_OVR
    return words, [drv], 30 + len(syms) * T + 200, check, drv


def manchester_rx(T=4, k=3):
    payload = [0x01, 0x02, 0x03, 0xF0, 0x0F, 0x80, 0xFF, 0x00, 0xD5, 0x55]
    fcs = list(binascii.crc32(bytes(payload)).to_bytes(4, "little"))
    good = manchester_halves(lsb_bits(PREAMBLE + payload + fcs))
    badp = payload[:]
    badp[3] ^= 0x10
    bad = manchester_halves(lsb_bits(PREAMBLE + badp + fcs))
    # idle low; after a frame the line is held high for six periods (start of idle)
    levels = []
    for f in (good, bad, good):
        levels += [(h, 1 - h) for h in f] + [(1, 0)] * 6 + [(0, 0)] * 40
    words, _ = asm("""
        ldi r1, %d
        sett r1
        ldi r0, %d
        sercfg r0
    """ % (T, MANCH | CRC32 | RXEN | pair(k)) + RX_LOOP)
    drv = LineDriver(k, levels, T, 40, (0, 0))

    def check(m, got):
        res, rest = split_frames(got)
        assert not rest, rest
        assert [r[0] for r in res] == [payload + fcs, badp + fcs, payload + fcs], res
        assert res[0][1] & ST_COK and not res[1][1] & ST_COK and res[2][1] & ST_COK
        assert all(st & ST_END and not st & (ST_OVR | ST_FERR) for _, st in res)
    return words, [drv], 40 + len(levels) * T + 200, check, drv


# -------------------------------------------------------------------- random

class RandomLine:
    """Random stimulus on all uio pins, biased toward what makes the
    receiver do something: stretches of noise, and frames coded for the mode
    and symbol period the engine has at that moment (read from the model, as
    a bench operator would set the generator to the device's settings),
    sometimes cut short or corrupted."""

    def __init__(self, rng):
        self.r, self.queue, self.hold = rng, [], 0
        self.cur = 0xFF

    def _frame(self, m):
        s = m.ser
        mode, k = s.cfg & 3, (s.cfg >> 6) & 3
        T = max(1, min(m.threads[s.owner].period, 12))
        n = self.r.randrange(1, 5)
        data = [self.r.choice([0xFF, 0x00, self.r.randrange(256)]) for _ in range(n)]
        if mode == 2:
            fcs = list(binascii.crc32(bytes(data)).to_bytes(4, "little"))
            if self.r.random() < 0.3:
                fcs[0] ^= 1
            lv = [(h, 1 - h) for h in manchester_halves(lsb_bits(PREAMBLE[-2:] + data + fcs))]
            lv += [(1, 0)] * 6 + [(0, 0)] * self.r.randrange(4, 12)
        else:
            crc = crc16_usb(data[1:])
            pkt = data + [crc & 0xFF, crc >> 8]
            syms = usb_packet_syms(pkt, stuff=self.r.random() < 0.8)
            lv = [PN[x] for x in [J] * 3 + syms + [J] * self.r.randrange(2, 6)]
        if self.r.random() < 0.2:
            lv = lv[:self.r.randrange(1, len(lv))]
        for pv, nv in lv:
            jit = T + (self.r.choice([-1, 1]) if self.r.random() < 0.03 else 0)
            self.queue += [with_pair(self.r.randrange(256) if self.r.random() < 0.02 else 0xFF, k, pv, nv)] * max(1, jit)

    def on_cycle(self, m):
        if self.queue:
            self.cur = self.queue.pop(0)
        elif self.hold:
            self.hold -= 1
        else:
            x = self.r.random()
            if x < 0.25 and (m.ser.cfg & 3) in (1, 2) and (m.ser.tx_state == 0 or x < 0.02):
                self._frame(m)
            elif x < 0.40:
                self.cur = self.r.randrange(256)
                self.hold = self.r.randrange(0, 12)
        m.ext_uio = self.cur


def random_program(rng, n=110, tx=0.5):
    """A loop of serializer, timer and pin instructions for one thread. Only
    waits that cannot hang: timeout forms after a SETD, with a running timer."""
    E = isa.encode
    w = [E("LDI", rd=1, imm=rng.randrange(1, 9)), E("SETT", rs=1),
         E("LDI", rd=2, imm=rng.choice([1, 2]) | 16 | (rng.randrange(64) << 2)), E("SERCFG", rs=2)]
    while len(w) < n:
        x = rng.random()
        r = rng.randrange(8)
        if x < 0.02:
            cfg = rng.randrange(256)
            if rng.random() < 0.85:
                cfg = (cfg & ~3) | rng.choice([1, 2]) | 16
            w += [E("LDI", rd=r, imm=cfg), E("SERCFG", rs=r)]
        elif x < 0.04:
            w += [E("LDI", rd=1, imm=rng.randrange(1, 9)), E("SETT", rs=1)]
        elif x < 0.50:
            w += self_wait(rng, E, r, tx)
        elif x < 0.60:
            w.append(E("SERST", rd=r))
        elif x < 0.70:
            w += [E("LDI", rd=r, imm=rng.choice([0xFF, 0x00, 0x80, 0x7E, rng.randrange(256)]))]
        elif x < 0.80:
            w.append(E(rng.choice(["SET", "CLR", "OEN", "OEF", "OD", "PP"]), pin=rng.randrange(8)))
        elif x < 0.84:
            w.append(E("WAITD", k=rng.randrange(0, 3)))
        elif x < 0.88:
            w.append(isa.encode("SERRX", rd=r) | (rng.randrange(6, 16)))      # an undefined function code
        elif x < 0.93:
            w.append(E(rng.choice(["BEQ", "BNE", "BCS", "BCC"]), off=rng.choice([1, 2])))
        else:
            w.append(E(rng.choice(["RCL", "INC", "SWAP"]), rd=r))
    return w


def self_wait(rng, E, r, tx):
    name = rng.choice(["SERTXT", "SERTXCT", "SERWTT"]) if rng.random() < tx else "SERRXT"
    ops = {} if name == "SERWTT" else ({"rd": r} if name == "SERRXT" else {"rs": r})
    return [E("SETD", k=rng.randrange(0, 20)), E(name, **ops)]


def random_scenario(seed):
    rng = random.Random(seed)
    tx = (0.5, 0.04, 0.15)[seed % 3]      # transmit-heavy, receive-heavy, mixed
    a = random_program(rng, tx=tx)
    b = random_program(rng, tx=tx)
    a.append(isa.encode("JMP", addr=0))
    base = len(a)
    b.append(isa.encode("JMP", addr=base))
    return a + b, base, [RandomLine(rng)]
