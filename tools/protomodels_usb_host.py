"""USB low-speed host and bus model, for exercising a USB device on the ISS and the RTL.

Same contract as tools/protomodels.py: on_cycle(m) is called before each
Machine.step() with the machine in its state for the coming cycle; the model
drives m.ext_uio (the level of every uio pad nobody on the chip drives), reads
m.pad() (bits 0-7 uio) and m.cycle. It knows USB 1.1 / 2.0 low speed and
nothing else: no instruction, no engine, no firmware symbol. Its parameters
are two pin numbers and the number of core cycles per bit at the host's
clock.

The bus as the model sees it.
  - D+ and D- are two uio pads. The host has 15 k pull-downs on both, the
    device a 1.5 k pull-up on D- (that is what makes it low speed). So an
    undriven bus reads D+ = 0, D- = 1: the J state of low speed. K is
    D+ = 1, D- = 0; SE0 is both low. Whoever drives a pad wins over the
    pulls; the model drives through m.ext_uio and sees the chip's drive in
    m.pad().
  - Bits are NRZI coded (a 0 is a change of state, a 1 is none), with a
    0 stuffed after six consecutive 1s, counted from the start of SYNC. A
    packet is SYNC (KJKJKJKK), the PID byte (its low nibble the PID, the
    high nibble its complement), the payload least significant bit first,
    and the end of packet: SE0 for two bit times, then J.
  - Tokens carry ADDR (7 bits) and ENDP (4 bits) and a CRC-5 over those
    eleven bits; DATA0/DATA1 carry 0 to 8 bytes (low speed) and a CRC-16.
    Both CRCs are the reflected forms with all-ones start and complemented
    result (CRC-5/USB, CRC-16/USB of the CRC catalogue).

What the model checks on everything the device drives (the `errors` list,
tuples (kind, cycle, detail); nothing here asserts):
  - "contention": the pads differ from what the host is driving.
  - "unexpected activity": the bus leaves J while the host neither drives
    nor waits for a response.
  - "response early" / "response late": the device's SYNC must start 2 to
    6.5 bit times after the host's end of packet, measured as USB 2.0
    7.1.18 defines it, from the SE0-to-J transition of the host's EOP to
    the device's first J-to-K transition. A response that has not started
    18 bit times after that point is a time-out (not an error by itself:
    the device may be ignoring the packet on purpose; the transaction
    layer decides). Every measured delay is kept in `delays` (bit times).
  - "bit timing": every symbol of the device's packet is a whole number of
    bit periods: the device's bit rate, fitted over the packet, is within
    1.5 % of 1.5 Mbit/s, and every transition lies within `jitter_ns` of
    the ideal grid at that rate.
  - "sync", "bit stuffing" (a seventh 1, or six 1s right before the EOP
    with no stuffed 0), "bytes" (not a whole number of bytes), "pid"
    (complement nibble wrong, or a PID a device must not send), "length"
    (a handshake with payload, a DATA packet over 8 bytes), "crc16",
    "eop" (SE0 not 1.875 to 2.25 bit times, i.e. outside 1.25-1.50 us, or
    not followed by J, or an SE1), "glitch" (a symbol shorter than half a
    bit).
  - Transaction and control layer: "toggle" (DATA0/DATA1 sequence),
    "handshake" (an answer that is not allowed for the packet sent),
    "control" (a control transfer that failed or returned the wrong
    amount), "descriptor" (a descriptor that is not well formed).

Timing of the host itself. Host packets are sent with exact bit periods.
The host leaves `gap_bits` between the packets of one transaction (2 is the
minimum USB allows), answers a device's DATA packet `turnaround_bits` after
it, and leaves `txn_gap_bits` between transactions; to every start it adds
a pseudo-random 0 to bit_cycles - 1 cycles (seeded) so that the phase of
the host's bits against the device's clock is swept. A keep-alive (a bare
low-speed EOP) is sent before a transaction once `keepalive_bits` (1 ms =
1500 by default; None turns it off) have passed since the last one.

Stimulus. Every public method queues a script and returns a record that is
filled in while it is played; `busy` is True until the queue is empty:
  reset(bits, recovery)           bus reset: SE0, then idle
  idle(bits)
  setup(addr, data8, corrupt_crc) one SETUP transaction (token + DATA0)
  token_in(addr, ep, ...)         one IN transaction; the host ACKs good data
  token_out(addr, ep, toggle, data, corrupt_crc)
  control_read / control_nodata   a whole control transfer (setup stage,
                                  data stage with NAK retries, status stage)
  get_descriptor, set_address, set_configuration
  enumerate()                     the sequence a host runs on attach
Fault injection for the tests: corrupt_crc (flip a CRC-16 bit),
bad_crc5 (flip a CRC-5 bit of a token).
"""

import random

# line states
SE0, J, K, SE1 = 0, 1, 2, 3
_NAME = {SE0: "SE0", J: "J", K: "K", SE1: "SE1"}
_LEVELS = {J: (0, 1), K: (1, 0), SE0: (0, 0)}       # (D+, D-) for low speed

PID = {"OUT": 0xE1, "IN": 0x69, "SOF": 0xA5, "SETUP": 0x2D,
       "DATA0": 0xC3, "DATA1": 0x4B, "ACK": 0xD2, "NAK": 0x5A, "STALL": 0x1E, "PRE": 0x3C}
PID_NAME = {v: k for k, v in PID.items()}

BIT_RATE = 1.5e6
RATE_TOL = 0.015                # low-speed data rate tolerance
EOP_MIN, EOP_MAX = 1.875, 2.25  # source EOP width 1.25 to 1.50 us, in bit times
RESP_MIN, RESP_MAX = 2.0, 6.5   # device inter-packet delay, bit times
TIMEOUT = 18                    # host gives up waiting for a response, bit times
LS_MAXPKT = 8

DESC_DEVICE, DESC_CONFIG = 1, 2


# ------------------------------------------------------------------ coding

def crc5(bits):
    """CRC-5/USB of a list of bits (first transmitted first): the 5-bit
    field as sent, bit 0 first."""
    r = 0x1F
    for b in bits:
        r = (r >> 1) ^ 0x14 if (r ^ b) & 1 else r >> 1
    return r ^ 0x1F


def crc16(data):
    """CRC-16/USB of bytes: the 16-bit field, sent low byte first."""
    r = 0xFFFF
    for byte in data:
        for i in range(8):
            b = (byte >> i) & 1
            r = (r >> 1) ^ 0xA001 if (r ^ b) & 1 else r >> 1
    return r ^ 0xFFFF


def lsb_bits(data):
    return [(byte >> i) & 1 for byte in data for i in range(8)]


def token_bytes(pid, addr, ep, bad_crc5=False):
    """PID, then the 16 bits ADDR, ENDP, CRC5, all least significant bit first."""
    field = addr & 0x7F | (ep & 0xF) << 7
    c = crc5([(field >> i) & 1 for i in range(11)])
    if bad_crc5:
        c ^= 0x01
    v = field | c << 11
    return [PID[pid] if isinstance(pid, str) else pid, v & 0xFF, v >> 8]


def data_bytes(toggle, payload, corrupt_crc=False):
    c = crc16(payload)
    if corrupt_crc:
        c ^= 0x0100
    return [PID["DATA1" if toggle else "DATA0"]] + list(payload) + [c & 0xFF, c >> 8]


def encode(packet, stuff=True):
    """Line states for a packet (bytes after SYNC): SYNC, the bytes, bit
    stuffing from the start of SYNC, NRZI from J, then SE0 SE0 J."""
    bits = lsb_bits([0x80] + list(packet))
    out, ones = [], 0
    for b in bits:
        out.append(b)
        ones = ones + 1 if b else 0
        if stuff and ones == 6:
            out.append(0)
            ones = 0
    syms, lvl = [], J
    for b in out:
        if not b:
            lvl = K if lvl == J else J
        syms.append(lvl)
    return syms + [SE0, SE0, J]


def setup_packet(bm, req, value, index, length):
    return [bm, req, value & 0xFF, value >> 8, index & 0xFF, index >> 8, length & 0xFF, length >> 8]


# ----------------------------------------------------------------- records

class Packet:
    """A packet the device drove, as the host decoded it."""

    def __init__(self, start):
        self.start = start          # cycle of the first K
        self.eop_j = None           # cycle of the SE0-to-J transition of its EOP
        self.delay = None           # response delay, bit times
        self.bytes = []             # bytes after SYNC (PID first), unstuffed
        self.pid = None
        self.errors = []            # kinds of the errors in this packet
        self.rate = None            # fitted bit period / nominal

    @property
    def name(self):
        return PID_NAME.get(self.pid, "?")

    @property
    def payload(self):
        return self.bytes[1:-2] if self.name in ("DATA0", "DATA1") else []

    @property
    def ok(self):
        return not self.errors


class Txn:
    """One transaction: what the host sent and what came back."""

    def __init__(self, kind, addr, ep):
        self.kind, self.addr, self.ep = kind, addr, ep
        self.result = None          # "ACK" "NAK" "STALL" "DATA0" "DATA1" "timeout" "bad"
        self.packet = None          # the device's Packet, if any
        self.data = None            # payload of a good DATA answer
        self.delay = None
        self.done = False

    def __repr__(self):
        return "Txn(%s addr=%d ep=%d -> %s)" % (self.kind, self.addr, self.ep, self.result)


class Control:
    """One control transfer."""

    def __init__(self, addr, setup):
        self.addr, self.setup = addr, list(setup)
        self.status = None          # "ok" "stall" "fail"
        self.corrupt_setups = 0     # fault injection: the first n SETUP DATA0 packets get a bad CRC
        self.corrupt_status = 0     # the first n status-stage OUT DATA1 packets get a bad CRC
        self.ack_faults = {}        # data packet index -> "drop" or "corrupt" (the host's ACK)
        self.data = []
        self.txns = []
        self.done = False

    @property
    def ok(self):
        return self.status == "ok"


class Enumeration:
    def __init__(self):
        self.controls = []
        self.device8 = None         # first GET_DESCRIPTOR(device) at address 0
        self.device = None          # GET_DESCRIPTOR(device, 18) at the new address
        self.config9 = None
        self.config = None
        self.address = None
        self.ok = False
        self.done = False


# ------------------------------------------------------------------- model

class UsbLsHost:
    """A low-speed host port with one device on it.

    bit_cycles       core cycles per bit at the host's clock (32 at 48 MHz)
    dp, dm           uio pin numbers of D+ and D-
    gap_bits         idle time between the host's packets of one transaction
    turnaround_bits  from the device's EOP to the host's handshake
    txn_gap_bits     between transactions
    keepalive_bits   keep-alive period (None: none)
    jitter_ns        allowed deviation of a device transition from its ideal place
    seed             for the start-phase sweep (None: no sweep)
    """

    def __init__(self, bit_cycles=32, dp=0, dm=1, gap_bits=2, turnaround_bits=3, txn_gap_bits=4,
                 keepalive_bits=1500, jitter_ns=25.0, seed=1, other=0xFF):
        self.T = bit_cycles
        self.dp, self.dm = dp, dm
        self.gap_bits, self.turnaround_bits, self.txn_gap_bits = gap_bits, turnaround_bits, txn_gap_bits
        self.keepalive_bits = keepalive_bits
        clock = bit_cycles * BIT_RATE
        self.jitter = max(1.0, jitter_ns * 1e-9 * clock)    # cycles; 1 cycle is the grid
        self.rng = random.Random(seed) if seed is not None else None
        self.other = other & 0xFF
        self.errors = []
        self.delays = []            # every measured response delay, bit times
        self.packets = []           # every device packet decoded
        self.txns = []
        self.cycle = 0
        self._q = []
        self._active = False
        self._listening = False
        self._last_eop_j = None     # SE0-to-J of the last packet on the bus
        self._last_keepalive = None
        self._flag_idle = False
        self._gen = self._main()
        self._drive = next(self._gen)

    # ------------------------------------------------------------ the clock

    @property
    def busy(self):
        return bool(self._q) or self._active

    def on_cycle(self, m):
        self.cycle = m.cycle
        d = self._drive
        dpv, dmv = _LEVELS[J] if d is None else _LEVELS[d]
        mask = (1 << self.dp) | (1 << self.dm)
        m.ext_uio = (self.other & ~mask & 0xFF) | dpv << self.dp | dmv << self.dm
        pad = m.pad()
        s = ((pad >> self.dp) & 1) << 1 | ((pad >> self.dm) & 1)
        state = {0b00: SE0, 0b01: J, 0b10: K, 0b11: SE1}[s]
        self._drive = self._gen.send(state)

    def _err(self, kind, detail=None):
        self.errors.append((kind, self.cycle, detail))

    def _main(self):
        while True:
            if not self._q:
                self._active = False
                yield from self._cycle(None, idle=True)
                continue
            self._active = True
            script = self._q.pop(0)
            yield from script

    def _cycle(self, drive, idle=False):
        """One cycle: put `drive` on the bus (None: release), return the state seen."""
        s = yield drive
        if drive is not None:
            if s != drive:
                self._err("contention", "host drives %s, bus is %s" % (_NAME[drive], _NAME[s]))
            self._flag_idle = False
        elif idle or not self._listening:
            if s != J:
                if not self._flag_idle:
                    self._err("unexpected activity", _NAME[s])
                self._flag_idle = True
            else:
                self._flag_idle = False
        return s

    def _wait(self, cycles):
        for _ in range(max(0, int(cycles))):
            yield from self._cycle(None)

    def _phase(self):
        return self.rng.randrange(self.T) if self.rng else 0

    def _gap_from(self, ref, bits):
        """Wait until `bits` bit times (plus the phase sweep) after cycle `ref`."""
        target = ref + bits * self.T + self._phase()
        # the next cycle we yield is self.cycle + 1
        yield from self._wait(target - self.cycle - 1)

    # ------------------------------------------------------------ packet layer

    def _send(self, packet, stuff=True):
        """Drive a packet; returns the cycle of its SE0-to-J transition."""
        syms = encode(packet, stuff)
        eop_j = None
        for i, s in enumerate(syms):
            for _ in range(self.T):
                yield from self._cycle(s)
                if i == len(syms) - 1 and eop_j is None:
                    eop_j = self.cycle
        self._last_eop_j = eop_j
        return eop_j

    def _receive(self, ref):
        """Listen for the device's answer to the packet whose EOP ended at `ref`.
        Returns a Packet, or None on a time-out."""
        T = self.T
        self._listening = True
        try:
            flagged = False
            while True:
                s = yield from self._cycle(None)
                if s == K:
                    break
                if s != J and not flagged:
                    self._err("eop", "%s on the bus before the response" % _NAME[s])
                    flagged = True
                if self.cycle >= ref + TIMEOUT * T:
                    return None
            pkt = Packet(self.cycle)
            runs = []                   # [state, first cycle, length]
            cur, start = K, self.cycle
            while True:
                s = yield from self._cycle(None)
                if s != cur:
                    runs.append([cur, start, self.cycle - start])
                    cur, start = s, self.cycle
                    if s == SE1:
                        pkt.errors.append("eop")
                        self._err("eop", "SE1")
                if cur == J and runs and runs[-1][0] == SE0 and self.cycle - start + 1 >= T:
                    break               # SE0 then a whole bit of J: the packet is over
                if cur != SE0 and runs and runs[-1][0] == SE0 and cur != J:
                    break               # SE0 followed by K: broken EOP, analysed below
                if self.cycle - pkt.start > 400 * T:
                    pkt.errors.append("eop")
                    self._err("eop", "no end of packet")
                    break
            if cur == J and runs and runs[-1][0] == SE0:
                pkt.eop_j = start
            self._last_eop_j = pkt.eop_j if pkt.eop_j is not None else self.cycle
            self._analyse(pkt, runs, cur, ref)
            self.packets.append(pkt)
            return pkt
        finally:
            self._listening = False

    def _analyse(self, pkt, runs, after, ref):
        T = self.T

        def bad(kind, detail):
            if kind not in pkt.errors:
                pkt.errors.append(kind)
            self._err(kind, detail)

        # the response delay, as USB 2.0 7.1.18 measures it
        d = (pkt.start - ref) / T
        pkt.delay = d
        self.delays.append(d)
        if d < RESP_MIN:
            bad("response early", "%.2f bit times" % d)
        elif d > RESP_MAX:
            bad("response late", "%.2f bit times" % d)
        # the end of packet
        if not runs or runs[-1][0] != SE0 or after != J:
            bad("eop", "the packet does not end in SE0 then J")
            body = runs
            se0 = None
        else:
            body, se0 = runs[:-1], runs[-1]
            w = se0[2] / T
            if not EOP_MIN <= w <= EOP_MAX:
                bad("eop", "SE0 of %.3f bit times" % w)
        if any(r[0] not in (J, K) for r in body):
            bad("eop", "SE0 or SE1 inside the packet")
            body = [r for r in body if r[0] in (J, K)]
        # bit timing: whole bit periods, at 1.5 Mbit/s +- 1.5 %, on a regular grid
        counts = []
        for state, c, n in body:
            nb = int(round(n / T))
            if nb == 0:
                bad("glitch", "%s for %d cycles at %d" % (_NAME[state], n, c))
                nb = 1
            counts.append(nb)
        total = sum(counts)
        if total and se0 is not None:
            end = se0[1]
            period = (end - pkt.start) / total
            pkt.rate = period / T
            if abs(period / T - 1) > RATE_TOL:
                bad("bit timing", "bit period %.3f cycles, nominal %d" % (period, T))
            pos = 0
            for (state, c, n), nb in zip(body + [se0], counts + [0]):
                ideal = pkt.start + pos * period
                if abs(c - ideal) > self.jitter:
                    bad("bit timing", "transition at %d, %.1f cycles off the grid" % (c, c - ideal))
                    break
                pos += nb
        # NRZI decode from J, then SYNC and unstuffing
        cells = []
        for (state, _, _), nb in zip(body, counts):
            cells += [state] * nb
        bits, prev = [], J
        for s in cells:
            bits.append(1 if s == prev else 0)
            prev = s
        if bits[:8] != [0] * 7 + [1]:
            bad("sync", "first bits %s" % bits[:8])
        out, ones, i = [], 0, 0
        while i < len(bits):
            b = bits[i]
            if ones == 6:
                if b:
                    bad("bit stuffing", "seventh 1 at bit %d" % i)
                ones = 0
                i += 1
                continue
            out.append(b)
            ones = ones + 1 if b else 0
            i += 1
        if ones == 6:
            bad("bit stuffing", "six 1s before the EOP without a stuffed 0")
        data = out[8:]
        if len(data) % 8:
            bad("bytes", "%d bits after SYNC" % len(data))
        pkt.bytes = [sum(data[i + k] << k for k in range(8)) for i in range(0, len(data) - len(data) % 8, 8)]
        if not pkt.bytes:
            bad("pid", "no PID")
            return
        p = pkt.bytes[0]
        pkt.pid = p
        if (p & 0xF) ^ (p >> 4) != 0xF:
            bad("pid", "PID 0x%02X fails its check nibble" % p)
            return
        name = pkt.name
        if name in ("ACK", "NAK", "STALL"):
            if len(pkt.bytes) != 1:
                bad("length", "%s with %d more bytes" % (name, len(pkt.bytes) - 1))
        elif name in ("DATA0", "DATA1"):
            if len(pkt.bytes) < 3:
                bad("length", "DATA packet of %d bytes" % len(pkt.bytes))
            else:
                if len(pkt.bytes) - 3 > LS_MAXPKT:
                    bad("length", "%d data bytes, low speed allows 8" % (len(pkt.bytes) - 3))
                pay, c = pkt.bytes[1:-2], pkt.bytes[-2] | pkt.bytes[-1] << 8
                if crc16(pay) != c:
                    bad("crc16", "CRC 0x%04X, computed 0x%04X" % (c, crc16(pay)))
        else:
            bad("pid", "a device does not send %s" % name)

    # ------------------------------------------------------------ transactions

    def _keepalive(self):
        if self.keepalive_bits is None:
            return
        if self._last_keepalive is None or self.cycle - self._last_keepalive >= self.keepalive_bits * self.T:
            self._last_keepalive = self.cycle
            for s in (SE0, SE0, J):
                for _ in range(self.T):
                    yield from self._cycle(s)
            self._last_eop_j = self.cycle - self.T + 1
            yield from self._gap_from(self._last_eop_j, self.txn_gap_bits)

    def _start_txn(self):
        if self._last_eop_j is not None:
            yield from self._gap_from(self._last_eop_j, self.txn_gap_bits)
        yield from self._keepalive()

    def _txn_setup(self, addr, data8, corrupt_crc=False, bad_crc5=False):
        t = Txn("SETUP", addr, 0)
        self.txns.append(t)
        yield from self._start_txn()
        ref = yield from self._send(token_bytes("SETUP", addr, 0, bad_crc5))
        yield from self._gap_from(ref, self.gap_bits)
        ref = yield from self._send(data_bytes(0, data8, corrupt_crc))
        yield from self._handshake_answer(t, ref, ("ACK",))
        t.done = True
        return t

    def _txn_out(self, addr, ep, toggle, data, corrupt_crc=False, bad_crc5=False):
        t = Txn("OUT", addr, ep)
        self.txns.append(t)
        yield from self._start_txn()
        ref = yield from self._send(token_bytes("OUT", addr, ep, bad_crc5))
        yield from self._gap_from(ref, self.gap_bits)
        ref = yield from self._send(data_bytes(toggle, data, corrupt_crc))
        yield from self._handshake_answer(t, ref, ("ACK", "NAK", "STALL"))
        t.done = True
        return t

    def _handshake_answer(self, t, ref, allowed):
        p = yield from self._receive(ref)
        t.packet = p
        if p is None:
            t.result = "timeout"
            return
        t.delay = p.delay
        if not p.ok:
            t.result = "bad"
            return
        t.result = p.name
        if p.name not in allowed:
            self._err("handshake", "%s answered with %s" % (t.kind, p.name))

    def _txn_in(self, addr, ep, bad_crc5=False, expect=None, ack_fault=None):
        """IN transaction. `expect` = 0/1: the toggle the host expects; a good
        DATA packet is ACKed in either case (USB 8.6.4), and one with the
        other toggle is reported as result "dup" (discarded). ack_fault:
        "drop" sends no ACK, "corrupt" an ACK whose PID check fails (the
        host's ACK lost or damaged on the wire; the host itself has the data)."""
        t = Txn("IN", addr, ep)
        self.txns.append(t)
        yield from self._start_txn()
        ref = yield from self._send(token_bytes("IN", addr, ep, bad_crc5))
        p = yield from self._receive(ref)
        t.packet = p
        if p is None:
            t.result = "timeout"
        else:
            t.delay = p.delay
            if not p.ok:
                t.result = "bad"
            else:
                t.result = p.name
                if p.name in ("DATA0", "DATA1"):
                    t.data = p.payload
                    yield from self._gap_from(p.eop_j, self.turnaround_bits)
                    if ack_fault == "corrupt":
                        yield from self._send([PID["ACK"] ^ 0x01])
                    elif ack_fault != "drop":
                        yield from self._send([PID["ACK"]])
                    if expect is not None and (p.name == "DATA1") != bool(expect):
                        t.result = "dup"
                elif p.name not in ("NAK", "STALL"):
                    self._err("handshake", "IN answered with %s" % p.name)
        t.done = True
        return t

    # ------------------------------------------------------------ control transfers

    def _control(self, ctl, data_in=True, max_errors=3, max_naks=200, early_status=None):
        """setup stage, IN data stage (wLength from the setup bytes), status stage."""
        addr, setup = ctl.addr, ctl.setup
        wlen = setup[6] | setup[7] << 8
        # setup stage: retried after an error, never NAKed or STALLed
        corrupt = ctl.corrupt_setups
        for attempt in range(max_errors):
            t = yield from self._txn_setup(addr, setup, corrupt_crc=attempt < corrupt)
            ctl.txns.append(t)
            if t.result == "ACK":
                break
            if t.result in ("NAK", "STALL"):
                self._err("handshake", "SETUP answered with %s" % t.result)
        else:
            ctl.status = "fail"
            ctl.done = True
            return
        # data stage
        toggle, errors, naks, packets = 1, 0, 0, 0
        if data_in and wlen:
            while len(ctl.data) < wlen:
                if early_status is not None and packets >= early_status:
                    break
                t = yield from self._txn_in(addr, 0, expect=toggle,
                                            ack_fault=ctl.ack_faults.get(packets))
                ctl.txns.append(t)
                if t.result == "NAK":
                    naks += 1
                    if naks > max_naks:
                        ctl.status = "fail"
                        ctl.done = True
                        return
                    continue
                if t.result == "STALL":
                    ctl.status = "stall"
                    ctl.done = True
                    return
                if t.result in ("timeout", "bad"):
                    errors += 1
                    if errors >= max_errors:
                        ctl.status = "fail"
                        ctl.done = True
                        return
                    continue
                if t.result == "dup":
                    continue
                errors = 0
                packets += 1
                toggle ^= 1
                ctl.data += t.data
                if len(t.data) > wlen - (len(ctl.data) - len(t.data)):
                    self._err("control", "device sent more than wLength")
                if len(t.data) < LS_MAXPKT:
                    break           # a short packet ends the data stage
        # status stage: the other direction, always DATA1
        errors = naks = 0
        attempt = 0
        while True:
            if data_in and wlen:
                t = yield from self._txn_out(addr, 0, 1, [], corrupt_crc=attempt < ctl.corrupt_status)
                attempt += 1
            else:
                t = yield from self._txn_in(addr, 0, expect=1)
                if t.result in ("DATA0", "DATA1", "dup") and t.data:
                    self._err("control", "status stage with %d data bytes" % len(t.data))
            ctl.txns.append(t)
            if t.result in ("ACK", "DATA1"):
                ctl.status = "ok"
                break
            if t.result in ("DATA0", "dup"):
                self._err("toggle", "status stage answered with DATA0")
                ctl.status = "fail"
                break
            if t.result == "STALL":
                ctl.status = "stall"
                break
            if t.result == "NAK":
                naks += 1
                if naks > max_naks:
                    ctl.status = "fail"
                    break
                continue
            errors += 1
            if errors >= max_errors:
                ctl.status = "fail"
                break
        ctl.done = True

    # ------------------------------------------------------------ public stimulus

    def _queue(self, gen):
        self._q.append(gen)

    def reset(self, bits=15000, recovery=15000):
        """Bus reset: SE0 for `bits` bit times (10 ms = 15000 is what USB asks
        of a host), then the bus idles for `recovery` (TRSTRCY, 10 ms)."""
        rec = {"done": False, "start": None, "end": None}

        def script():
            yield from self._reset_script(bits, recovery, rec)
            rec["done"] = True
        self._queue(script())
        return rec

    def idle(self, bits, cycles=0):
        def script():
            yield from self._wait(bits * self.T + cycles)
        self._queue(script())

    def setup(self, addr, data8, corrupt_crc=False, bad_crc5=False):
        holder = []

        def script():
            holder.append((yield from self._txn_setup(addr, data8, corrupt_crc, bad_crc5)))
        self._queue(script())
        return holder

    def token_in(self, addr, ep=0, bad_crc5=False, expect=None):
        holder = []

        def script():
            holder.append((yield from self._txn_in(addr, ep, bad_crc5, expect)))
        self._queue(script())
        return holder

    def token_out(self, addr, ep=0, toggle=1, data=(), corrupt_crc=False, bad_crc5=False):
        holder = []

        def script():
            holder.append((yield from self._txn_out(addr, ep, toggle, list(data), corrupt_crc, bad_crc5)))
        self._queue(script())
        return holder

    def control_read(self, addr, setup, corrupt_setups=0, early_status=None, corrupt_status=0, ack_faults=None):
        ctl = Control(addr, setup)
        ctl.corrupt_setups = corrupt_setups
        ctl.corrupt_status = corrupt_status
        ctl.ack_faults = dict(ack_faults or {})
        self._queue(self._control(ctl, True, early_status=early_status))
        return ctl

    def control_nodata(self, addr, setup, corrupt_setups=0):
        ctl = Control(addr, setup)
        ctl.corrupt_setups = corrupt_setups
        self._queue(self._control(ctl, False))
        return ctl

    def get_descriptor(self, addr, dtype, length, index=0, **kw):
        return self.control_read(addr, setup_packet(0x80, 6, dtype << 8 | index, 0, length), **kw)

    def set_address(self, addr, new, **kw):
        return self.control_nodata(addr, setup_packet(0x00, 5, new, 0, 0), **kw)

    def set_configuration(self, addr, value, **kw):
        return self.control_nodata(addr, setup_packet(0x00, 9, value, 0, 0), **kw)

    def enumerate(self, new_addr=5, first_len=64, reset_bits=15000, recovery_bits=15000, early_status=None):
        """What a host does when a device appears: reset; GET_DESCRIPTOR(device)
        at address 0 (`first_len` = 64 or 8; with early_status=1 the host
        reads only the first packet and goes to the status stage, as Windows
        does); reset; SET_ADDRESS; GET_DESCRIPTOR(device, 18);
        GET_DESCRIPTOR(configuration, 9), then the whole wTotalLength;
        SET_CONFIGURATION with the configuration's value."""
        e = Enumeration()

        def script():
            yield from self._reset_script(reset_bits, recovery_bits)
            c = Control(0, setup_packet(0x80, 6, DESC_DEVICE << 8, 0, first_len))
            e.controls.append(c)
            yield from self._control(c, True, early_status=early_status)
            if not self._check_ok(c, "GET_DESCRIPTOR(device) at address 0"):
                e.done = True
                return
            e.device8 = c.data
            if len(c.data) < 8 or c.data[1] != DESC_DEVICE or c.data[7] != LS_MAXPKT:
                self._err("descriptor", "device descriptor head %s" % c.data[:8])
            yield from self._reset_script(reset_bits, recovery_bits)
            c = Control(0, setup_packet(0x00, 5, new_addr, 0, 0))
            e.controls.append(c)
            yield from self._control(c, False)
            if not self._check_ok(c, "SET_ADDRESS"):
                e.done = True
                return
            e.address = new_addr
            yield from self._wait(2 * self.T)       # TDSETADDR: 2 ms in USB; compressed here
            c = Control(new_addr, setup_packet(0x80, 6, DESC_DEVICE << 8, 0, 18))
            e.controls.append(c)
            yield from self._control(c, True)
            if not self._check_ok(c, "GET_DESCRIPTOR(device, 18)"):
                e.done = True
                return
            e.device = c.data
            self._check_device(c.data)
            c = Control(new_addr, setup_packet(0x80, 6, DESC_CONFIG << 8, 0, 9))
            e.controls.append(c)
            yield from self._control(c, True)
            if not self._check_ok(c, "GET_DESCRIPTOR(configuration, 9)"):
                e.done = True
                return
            e.config9 = c.data
            total = c.data[2] | c.data[3] << 8 if len(c.data) >= 4 else 9
            c = Control(new_addr, setup_packet(0x80, 6, DESC_CONFIG << 8, 0, total))
            e.controls.append(c)
            yield from self._control(c, True)
            if not self._check_ok(c, "GET_DESCRIPTOR(configuration, %d)" % total):
                e.done = True
                return
            e.config = c.data
            self._check_config(c.data, total)
            value = c.data[5] if len(c.data) > 5 else 1
            c = Control(new_addr, setup_packet(0x00, 9, value, 0, 0))
            e.controls.append(c)
            yield from self._control(c, False)
            if not self._check_ok(c, "SET_CONFIGURATION(%d)" % value):
                e.done = True
                return
            e.ok = True
            e.done = True
        self._queue(script())
        return e

    def _reset_script(self, bits, recovery, rec=None):
        if self._last_eop_j is not None:
            yield from self._gap_from(self._last_eop_j, self.txn_gap_bits)
        for i in range(bits * self.T):
            yield from self._cycle(SE0)
            if rec is not None and i == 0:
                rec["start"] = self.cycle
        if rec is not None:
            rec["end"] = self.cycle + 1
        self._last_eop_j = self.cycle + 1
        self._last_keepalive = self.cycle
        yield from self._wait(recovery * self.T)

    def _check_ok(self, c, what):
        if not c.ok:
            self._err("control", "%s: %s" % (what, c.status))
            return False
        wlen = c.setup[6] | c.setup[7] << 8
        if len(c.data) > wlen:
            self._err("control", "%s: %d bytes for wLength %d" % (what, len(c.data), wlen))
        return True

    def _check_device(self, d):
        if len(d) != 18 or d[0] != 18 or d[1] != DESC_DEVICE:
            self._err("descriptor", "device descriptor %s" % d)
            return
        if d[7] != LS_MAXPKT:
            self._err("descriptor", "bMaxPacketSize0 %d (low speed: 8)" % d[7])
        if d[17] < 1:
            self._err("descriptor", "no configuration")

    def _check_config(self, d, total):
        if len(d) != total or len(d) < 9 or d[1] != DESC_CONFIG:
            self._err("descriptor", "configuration %s" % d)
            return
        i, interfaces = 0, 0
        while i < len(d):
            n = d[i]
            if n < 2 or i + n > len(d):
                self._err("descriptor", "descriptor of length %d at offset %d" % (n, i))
                return
            if d[i + 1] == 4:
                interfaces += 1
            if d[i + 1] == 5:
                if n != 7:
                    self._err("descriptor", "endpoint descriptor of length %d" % n)
                elif (d[i + 4] | d[i + 5] << 8) > LS_MAXPKT:
                    self._err("descriptor", "endpoint packet size over 8 at low speed")
                elif d[i + 3] & 3 == 3 and d[i + 6] < 10:
                    self._err("descriptor", "low-speed interrupt interval under 10 ms")
            i += n
        if interfaces != d[4]:
            self._err("descriptor", "bNumInterfaces %d, %d interface descriptors" % (d[4], interfaces))
