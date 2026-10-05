"""fw/i2c_slave.s on the golden model with the I2C master model.
Run: python3 -m pytest tools/test_fw_i2c_slave.py -q

The master (tools/protomodels_i2c_slave.py) knows only the bus rules. The
EEPROM contents are a jump table in program memory, built here with
keyer_isa.encode and loaded at the firmware's TABLE symbol. Expected data
comes from a few lines of reference behaviour in this file (a pointer that
wraps, a write buffer of eight pairs), not from the firmware.
"""

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyer_isa  # noqa: E402
import keyerasm  # noqa: E402
import protomodels as pm  # noqa: E402
from keyersim import Machine  # noqa: E402
from protomodels_i2c_slave import I2cMasterModel  # noqa: E402

FWDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fw")
FW = os.path.join(FWDIR, "i2c_slave.s")
SCL, SDA = 2, 3                             # uio2, uio3
Q_MIN = 8                                   # quarter period the firmware header promises
RELEASE_MAX = 56                            # SCL released at most this long after its falling edge
RELEASE_MAX_OTHER = 34                      # the same after an address byte that is not ours
HOLD_MAX = 40                               # longest time the slave holds SCL low
# a short timeout for the tests: SETD 4 with a 200-cycle tick fires 600..800 cycles later
FAST = {"TMO_PERIOD": 200, "TMO_TICKS": 4}
TMO_LO, TMO_HI = 600, 800

START, STOP = ("start",), ("stop",)


def load_fw(symbols=None):
    with open(FW) as f:
        words, syms, _ = keyerasm.assemble(f.read(), symbols=symbols)
    return keyerasm.to_list(words), syms, max(words) + 1


def table_words(data):
    """The EEPROM contents as the host loads them: LDI r0, byte / RET per byte."""
    out = []
    for b in data:
        out += [keyer_isa.encode("LDI", rd=0, imm=b), keyer_isa.encode("RET")]
    return out


def pattern(n):
    return [(0x35 * i + 0x1B) & 0xFF for i in range(n)]


class Bench:
    """Machine + firmware + table + master. run(ops) queues bus operations and
    steps until the master has finished them."""

    def __init__(self, data=None, quarter=Q_MIN, symbols=None, lead=40, hold=None):
        sy = dict(FAST)
        sy.update(symbols or {})
        self.words, self.syms, self.nwords = load_fw(sy)
        self.size = self.syms["TABLE_BYTES"]
        self.addr = self.syms["I2C_ADDR"]
        self.W, self.R = self.addr << 1, (self.addr << 1) | 1
        self.data = list(pattern(self.size) if data is None else data)
        assert len(self.data) == self.size
        self.m = Machine()
        self.m.load(self.words)
        self.m.load(table_words(self.data), base=self.syms["TABLE"])
        self.m.host_run(0, True)
        self.q = quarter
        self.master = I2cMasterModel(scl=SCL, sda=SDA, quarter=quarter, data_hold=hold)
        self.master.queue([("wait", lead)])
        self.sda_driven = []                # cycles in which the slave pulled SDA low
        self.scl_driven = []                # cycles in which the slave pulled SCL low

    def step(self, n=1):
        m = self.m
        for _ in range(n):
            self.master.on_cycle(m)
            if m.uio_oe & (1 << SDA):
                self.sda_driven.append(m.cycle)
            if m.uio_oe & (1 << SCL):
                self.scl_driven.append(m.cycle)
            m.step()

    def run(self, ops, tail=40, limit=2_000_000):
        self.master.queue(ops)
        n = 0
        while not self.master.idle:
            self.step()
            n += 1
            assert n < limit, "the master did not finish"
        self.step(tail)

    def outbox(self):
        out = []
        while True:
            b = self.m.host_outbox_pop(0)
            if b is None:
                return out
            out.append(b)

    def released(self):
        """The slave drives neither line."""
        return self.m.uio_oe & ((1 << SDA) | (1 << SCL)) == 0

    def idling(self):
        """The thread is not in a transfer: it drives nothing and follows
        the bus from its idle loop (or straight from a STOP it saw)."""
        t, sy = self.m.threads[0], self.syms
        return (t.running and self.released() and t.lr in (sy["listen"], sy["idle"] + 1)
                and sy["clk"] <= t.pc < sy["listen"])

    def at_rest(self):
        """Idling on a free bus: watching SCL and SDA, both high, for a START."""
        t, sy = self.m.threads[0], self.syms
        return self.idling() and t.pc in (sy["clk_h1"], sy["clk_h1"] + 1)

    def host_patch(self, ptr, byte):
        """What the host does with a (pointer, byte) pair: stop the thread,
        rewrite the table entry, set the PC to `listen`, run the thread."""
        self.m.host_run(0, False)
        self.step(4)
        self.m.load([keyer_isa.encode("LDI", rd=0, imm=byte)], base=self.syms["TABLE"] + 2 * ptr)
        self.step(4)
        assert self.m.host_set_pc(0, self.syms["listen"])
        self.m.host_run(0, True)
        self.step(20)                       # it catches a START from 14 cycles after the restart

    def release_times(self, since=0):
        """For every stretch the master saw (from entry `since` of its list
        on): cycles from the SCL falling edge to the slave letting SCL go."""
        return [n + 2 * self.q for _, n in self.master.stretches[since:] if n]


def runs(cycles):
    """A sorted list of cycle numbers as (first, length) of each unbroken run."""
    out = []
    for c in cycles:
        if out and out[-1][0] + out[-1][1] == c:
            out[-1][1] += 1
        else:
            out.append([c, 1])
    return [tuple(r) for r in out]


def read_ops(b, n):
    """Address + R, n bytes, the last one NACKed, STOP."""
    return [START, ("write", b.R)] + [("read", i < n - 1) for i in range(n)] + [STOP]


# ---------------------------------------------------------------- memory map

def test_image_fits_program_memory():
    words, syms, n = load_fw()
    assert n <= syms["TABLE"], "program (%d words) runs into the table at %d" % (n, syms["TABLE"])
    assert syms["TABLE"] + 2 * syms["TABLE_BYTES"] <= 256
    assert syms["TABLE_BYTES"] & (syms["TABLE_BYTES"] - 1) == 0      # the wrap is an AND
    assert syms["i2c_init"] == 0                                    # thread 0 starts at 0
    assert n == 86 and syms["TABLE"] == 128 and syms["TABLE_BYTES"] == 64   # as the header says


# ---------------------------------------------------------------- reads

def test_set_pointer_then_read_across_the_wrap():
    b = Bench()
    n = b.size
    b.run([START, ("write", b.W), ("write", n - 2), STOP] + read_ops(b, 5))
    assert b.master.errors == []
    assert b.master.read == [b.data[n - 2], b.data[n - 1], b.data[0], b.data[1], b.data[2]]
    assert b.master.acks == [(b.W, True), (n - 2, True), (b.R, True)]
    assert [k for k, _ in b.master.events] == ["start", "stop", "start", "stop"]
    assert b.outbox() == []                 # a pointer write alone hands nothing to the host
    assert b.at_rest()


def test_pointer_byte_wraps_at_table_size():
    b = Bench()
    b.run([START, ("write", b.W), ("write", 0xC0 | 7), STOP] + read_ops(b, 1))
    assert b.master.errors == []
    assert b.master.read == [b.data[7]]     # only the low bits of the pointer byte count


def test_current_address_read_continues_from_the_last_pointer():
    b = Bench()
    b.run([START, ("write", b.W), ("write", 20), STOP] + read_ops(b, 3))
    b.run(read_ops(b, 1))                   # no pointer write: carries on at 23
    b.run(read_ops(b, 2))
    assert b.master.errors == []
    assert b.master.read == b.data[20:26]
    assert b.at_rest()


def test_first_read_after_reset_starts_at_zero():
    b = Bench()
    b.run(read_ops(b, 2))
    assert b.master.errors == [] and b.master.read == b.data[0:2]


def test_repeated_start():
    b = Bench()
    b.run([START, ("write", b.W), ("write", 9), START, ("write", b.R), ("read", True), ("read", False),
           START, ("write", b.W), ("write", 40), START, ("write", b.R), ("read", False), STOP])
    assert b.master.errors == []
    assert b.master.read == [b.data[9], b.data[10], b.data[40]]
    assert [k for k, _ in b.master.events] == ["start", "start", "start", "start", "stop"]
    assert all(acked for _, acked in b.master.acks)
    assert b.at_rest()


def test_every_byte_value_reads_back():
    """All 256 byte values through the table, 64 at a time."""
    for base in range(0, 256, 64):
        data = [(base + i) ^ 0xA5 for i in range(64)]
        b = Bench(data=data)
        b.run(read_ops(b, 64))
        assert b.master.errors == [] and b.master.read == data


# ---------------------------------------------------------------- other devices

def test_transaction_for_another_device_is_ignored():
    b = Bench()
    b.run([START, ("write", b.W), ("write", 12), STOP])
    assert b.sda_driven, "the slave ACKs its own address"
    b.sda_driven.clear()
    seen = len(b.master.stretches)
    others = [(b.addr ^ 0x01), (b.addr ^ 0x40), 0x00, 0x7F]         # neighbours, general call
    ops = []
    for a in others:
        ops += [START, ("write", a << 1, False), ("write", 0x5A, False), ("write", 0x00, False), STOP]
        ops += [START, ("write", (a << 1) | 1, False), ("read", True), ("read", False), STOP]
    b.run(ops)
    assert b.master.errors == []            # every byte NACKed, as the script demands
    assert b.sda_driven == []               # SDA never driven
    assert b.master.read == [0xFF, 0xFF] * len(others)
    assert max(b.release_times(seen)) <= RELEASE_MAX_OTHER
    assert b.outbox() == [] and b.at_rest()
    b.run(read_ops(b, 1))                   # still there, pointer untouched
    assert b.master.errors == [] and b.master.read[-1] == b.data[12]


def test_address_and_table_are_equ_symbols():
    data = pattern(16)
    b = Bench(data=data, symbols={"I2C_ADDR": 0x2C, "TABLE": 0xE0, "TABLE_BYTES": 16})
    b.run([START, ("write", 0x50 << 1, False), STOP,
           START, ("write", 0x2C << 1), ("write", 14), START, ("write", (0x2C << 1) | 1),
           ("read", True), ("read", True), ("read", False), STOP])
    assert b.master.errors == []
    assert b.master.read == [data[14], data[15], data[0]]


# ---------------------------------------------------------------- writes

def test_data_bytes_are_acked_and_handed_to_the_host():
    """Bytes after the pointer byte are ACKed and arrive in the outbox as
    (pointer, byte) pairs; the table changes only when the host patches it."""
    b = Bench()
    n = b.size
    new = [0xDE, 0x00, 0xFF]
    b.run([START, ("write", b.W), ("write", n - 2)] + [("write", v) for v in new] + [STOP])
    assert b.master.errors == []
    assert all(acked for _, acked in b.master.acks)
    pairs = b.outbox()
    assert pairs == [n - 2, 0xDE, n - 1, 0x00, 0, 0xFF]             # the pointer wraps here too
    b.run(read_ops(b, 1))                   # the pointer moved past the written bytes
    assert b.master.read == [b.data[1]]
    b.run([START, ("write", b.W), ("write", n - 2)] + read_ops(b, 3))
    assert b.master.read[1:] == [b.data[n - 2], b.data[n - 1], b.data[0]]    # not stored yet
    for ptr, byte in zip(pairs[0::2], pairs[1::2]):
        b.host_patch(ptr, byte)
    b.run([START, ("write", b.W), ("write", n - 2)] + read_ops(b, 4))
    assert b.master.read[4:] == new + [b.data[1]]
    assert b.master.errors == [] and b.at_rest()


def test_host_may_stop_the_thread_anywhere_and_restart_it_at_listen():
    """Stopped in the middle of its own ACK, with SDA and SCL held low, the
    thread lets both go when the host restarts it at `listen`; the transfer
    it was in is lost, the next one works and sees the patched byte."""
    b = Bench()
    b.run([START, ("write", b.W), ("write", 40), STOP])
    b.run([START, ("wbits", b.W, 8)], tail=0)
    n = 0
    while not b.m.uio_oe & (1 << SDA):      # until the slave pulls SDA low for the ACK
        b.step()
        n += 1
        assert n < 60
    b.m.host_run(0, False)
    b.step(300)
    assert b.m.uio_oe & (1 << SDA) and b.m.uio_oe & (1 << SCL)      # frozen, holding both lines
    b.host_patch(40, 0x77)
    assert b.released() and b.idling()
    b.run([("rbits", 1), STOP] + read_ops(b, 2))
    assert b.master.bits[0][0] == 1         # the ACK was lost with the transfer
    assert b.master.errors == []
    assert b.master.read == [0x77, b.data[41]]
    assert b.at_rest()


def test_write_buffer_full_is_a_nack():
    """Eight pairs fit the outbox. The ninth data byte is NACKed and dropped,
    the pointer stays, and the slave is deaf until the next START. Once the
    host has emptied the outbox, writes are accepted again."""
    b = Bench()
    data = [0x80 + i for i in range(10)]
    b.run([START, ("write", b.W), ("write", 4)]
          + [("write", v) for v in data[:8]]
          + [("write", data[8], False), ("write", data[9], False), STOP])
    assert b.master.errors == []
    assert b.m.threads[0].outbox and len(b.m.threads[0].outbox) == 16
    b.run([START, ("write", b.W), ("write", 30), ("write", 0x11, False), STOP])   # still full
    assert b.master.errors == []
    assert len(b.m.threads[0].outbox) == 16
    b.run(read_ops(b, 1))
    assert b.master.read == [b.data[30]]    # the NACKed byte did not advance the pointer
    pairs = b.outbox()
    assert pairs == [x for i, v in enumerate(data[:8]) for x in (4 + i, v)]
    b.run([START, ("write", b.W), ("write", 50), ("write", 0x22), ("write", 0x33), STOP])
    assert b.master.errors == []
    assert b.outbox() == [50, 0x22, 51, 0x33]
    assert b.at_rest()


def test_host_draining_keeps_a_long_write_going():
    """A host that empties the outbox between bytes never sees a NACK."""
    b = Bench()
    got = []
    b.run([START, ("write", b.W), ("write", 0)])
    for i in range(20):
        b.run([("write", i ^ 0x5A)], tail=0)
        got += b.outbox()
    b.run([STOP])
    assert b.master.errors == []
    assert got == [x for i in range(20) for x in (i, i ^ 0x5A)]


# ---------------------------------------------------------------- START and STOP anywhere

def test_stop_in_the_middle_of_a_byte_then_a_good_transaction():
    b = Bench()
    ops = []
    for nbits in range(1, 8):               # STOP inside the address byte
        ops += [START, ("wbits", b.W, nbits), STOP]
    ops += [START, ("write", b.W), ("wbits", 0xFF, 3), STOP]         # inside the pointer byte
    ops += [START, ("write", b.W), ("write", 33), ("wbits", 0xA5, 6), STOP]   # inside a data byte
    ops += [START, STOP]                                           # nothing at all
    b.run(ops)
    assert b.master.errors == []
    assert b.at_rest()
    assert b.outbox() == []                 # the cut-off data byte was not handed over
    b.run(read_ops(b, 2))
    assert b.master.errors == []
    assert b.master.read == b.data[33:35]   # the complete pointer byte counted, the cut-off one did not
    assert [k for k, _ in b.master.events] == ["start", "stop"] * 11


def test_stop_and_start_while_the_slave_sends_ones():
    """During a read the master can only signal while the slave's bit is a
    1 (SDA released) or in the ACK slot."""
    data = pattern(64)
    data[10] = 0xFF
    data[11] = 0xFF
    data[12] = 0x0F
    b = Bench(data=data)
    b.run([START, ("write", b.W), ("write", 10), START, ("write", b.R), ("rbits", 3), STOP])
    assert b.master.errors == [] and b.at_rest()
    assert [lv for lv, _ in b.master.bits] == [1, 1, 1]
    b.run([START, ("write", b.R), ("rbits", 5), START, ("write", b.R), ("read", False), STOP])
    assert b.master.errors == [] and b.at_rest()
    assert b.master.read == [0x0F]          # 10 and 11 were begun, each begun byte moves the pointer
    b.run([START, ("write", b.W), ("write", 12), START, ("write", b.R), ("rbits", 8), STOP])
    assert b.master.errors == [] and b.at_rest()            # STOP in the ACK slot
    assert [lv for lv, _ in b.master.bits[-8:]] == [0, 0, 0, 0, 1, 1, 1, 1]


def test_start_in_the_middle_of_a_byte():
    b = Bench()
    b.run([START, ("wbits", b.W, 4), START, ("write", b.W), ("wbits", 0x00, 5),
           START, ("write", b.W), ("write", 17), ("wbits", 0xFF, 2),
           START, ("write", b.R), ("read", False), STOP])
    assert b.master.errors == []
    assert b.master.read == [b.data[17]]
    assert b.outbox() == [] and b.at_rest()


# ---------------------------------------------------------------- timing

SCENARIO_DATA = pattern(64)
SCENARIO_DATA[1] = 0x00                     # every bit pulled low by the slave
SCENARIO_DATA[2] = 0xFF                     # every bit released


def scenario(b):
    """Every kind of transfer once. Returns what the master must have read
    and what the host must find in the outbox."""
    d = b.data
    b.run([START, ("write", b.W), ("write", 62), STOP]
          + read_ops(b, 5)
          + read_ops(b, 1)
          + [START, ("write", (b.addr ^ 1) << 1, False), ("write", 0x33, False), STOP]
          + [START, ("write", b.W), ("write", 5), START, ("write", b.R), ("read", True), ("read", False), STOP]
          + [START, ("write", b.W), ("write", 9), ("write", 0xDE), ("write", 0x01), STOP]
          + [START, ("wbits", b.W, 5), STOP]
          + [START, ("write", b.W), ("wbits", 0xFF, 3), START, ("write", b.R), ("read", False), STOP])
    return [d[62], d[63], d[0], d[1], d[2], d[3], d[5], d[6], d[11]], [9, 0xDE, 10, 0x01]


def test_minimum_timing_in_every_phase():
    """Quarter period 8 (SCL low 16, high 16, START/STOP set-up and hold 16):
    the fastest master the header promises. The lead-in moves the whole
    transfer against the thread's slots and its poll loop."""
    for lead in range(40, 64):
        b = Bench(data=SCENARIO_DATA, quarter=Q_MIN, lead=lead)
        read, pairs = scenario(b)
        assert b.master.errors == [], (lead, b.master.errors[:3])
        assert b.master.read == read, lead
        assert b.outbox() == pairs, lead
        assert b.at_rest(), lead
        # the master's own phases are as short as promised, and no shorter
        assert min(n for _, n in b.master.scl_low) == 2 * Q_MIN
        assert min(n for _, n in b.master.scl_high) == 2 * Q_MIN


def test_short_and_long_data_hold():
    """A master may change SDA as soon as SCL is low, or as late as just
    before it rises. One cycle after the falling edge the slave is still
    looking at SCL high and sees the data change first; it must not take
    that for a START or STOP."""
    for hold in (1, 2, 3, 4, 5, 6, 14):
        for lead in range(40, 64):
            b = Bench(data=SCENARIO_DATA, quarter=Q_MIN, lead=lead, hold=hold)
            read, pairs = scenario(b)
            assert b.master.errors == [], (hold, lead, b.master.errors[:3])
            assert b.master.read == read and b.outbox() == pairs, (hold, lead)
            assert b.at_rest()
            assert {h for _, h, who in b.master.hold if who == "m"} == {hold}


def test_one_cycle_faster_is_too_fast():
    """The limit in the header is the real one: with a quarter period of 7 a
    bit the slave sends can change in the cycle SCL rises."""
    bad = 0
    for lead in range(40, 64):
        b = Bench(data=SCENARIO_DATA, quarter=Q_MIN - 1, lead=lead)
        try:
            read, pairs = scenario(b)
        except AssertionError:
            bad += 1
            continue
        bad += bool(b.master.errors) or b.master.read != read
    assert bad > 0


def test_set_up_hold_and_stretch_bounds():
    """In cycles, at the fastest clock and at two slower ones:
      - SDA never changes while SCL is high (the model's errors);
      - the slave changes SDA 9 cycles or more after SCL fell (hold);
      - a bit the slave sends inside a byte is on SDA within 14 cycles;
      - SDA is stable before SCL rises: 2 cycles at the fastest clock (4
        from quarter 9 on), 4 when the slave itself lets SCL rise;
      - the slave lets SCL go within 56 cycles of the falling edge and
        never holds it for more than 40 cycles at a time."""
    for q in (Q_MIN, Q_MIN + 1, 13):
        for lead in range(40, 64):
            b = Bench(data=SCENARIO_DATA, quarter=q, lead=lead)
            scenario(b)
            mm = b.master
            assert mm.errors == []
            stretch = dict(mm.stretches)                    # cycle the master released SCL -> stretch
            slave = [(c, h) for c, h, who in mm.hold if who == "s"]
            assert slave and min(h for _, h in slave) >= 9
            if q == Q_MIN:
                inside = [h for c, h in slave if stretch.get(c - h + 2 * q) == 0]
                assert inside and max(inside) <= 14
            rise_setup = dict(mm.setup)                     # cycle of the rising edge -> set-up
            for released, n in mm.stretches:
                if released + n in rise_setup:
                    need = 4 if n or q > Q_MIN else 2
                    assert rise_setup[released + n] >= need, (q, lead, released, n)
            assert max(b.release_times()) <= RELEASE_MAX
            assert max(n for _, n in mm.scl_low) <= max(RELEASE_MAX, 2 * q)
            assert max(n for _, n in runs(b.scl_driven)) <= HOLD_MAX


def test_slow_master_never_sees_a_stretch():
    """With SCL low for 56 cycles or more the slave has always let go
    before the master does."""
    for q in (28, 40):
        b = Bench(data=SCENARIO_DATA, quarter=q)
        read, pairs = scenario(b)
        assert b.master.errors == [] and b.master.read == read and b.outbox() == pairs
        assert all(n == 0 for _, n in b.master.stretches)
        assert all(n == 2 * q for _, n in b.master.scl_low)


def test_default_constants_at_100_khz():
    """The .equ defaults (50 MHz clock, 30 ms timeout) with a 100 kHz master."""
    b = Bench(symbols={"TMO_PERIOD": 50000, "TMO_TICKS": 30}, quarter=125)
    b.run([START, ("write", b.W), ("write", 63), START, ("write", b.R), ("read", True), ("read", False), STOP])
    assert b.master.errors == []
    assert b.master.read == [b.data[63], b.data[0]]
    assert all(n == 0 for _, n in b.master.stretches)


# ---------------------------------------------------------------- timeouts

def test_master_dies_with_scl_low_during_the_ack():
    """The master stops with SCL low while the slave holds SDA low for its
    ACK. The slave must let SDA go after the timeout and take the next
    START as if nothing had happened."""
    b = Bench()
    b.run([START, ("write", b.W), ("write", 21), STOP])
    b.sda_driven.clear()
    b.run([START, ("wbits", b.W, 8), ("wait", 1500)])
    (first, held), = runs(b.sda_driven)     # one run: the ACK that nobody clocked
    assert TMO_LO <= held <= TMO_HI + 20, held
    assert b.idling()                       # (SCL is still low: the bus is not free yet)
    b.run([("rbits", 1), STOP] + read_ops(b, 1))
    assert b.master.bits[0][0] == 1         # the ACK slot, clocked too late: nothing there
    assert b.master.errors == []
    assert b.master.read == [b.data[21]]
    assert b.at_rest()


def test_master_dies_with_scl_low_during_a_read():
    """The same while the slave is sending a 0 bit."""
    data = pattern(64)
    data[8] = 0x00
    b = Bench(data=data)
    b.run([START, ("write", b.W), ("write", 8), START, ("write", b.R), ("rbits", 2)])
    assert b.m.uio_oe & (1 << SDA)          # the third bit, a 0, is on the bus
    b.run([("wait", 1500)], tail=0)
    assert b.idling()
    b.run([("rbits", 7), STOP] + read_ops(b, 1))
    assert [lv for lv, _ in b.master.bits] == [0, 0] + [1] * 7
    assert b.master.errors == []
    assert b.master.read == [data[9]]


def test_master_dies_with_scl_high_while_the_slave_holds_sda():
    """SCL stays high in the middle of a 0 bit from the slave. Nobody can
    signal on a bus whose SDA is held low, so after the timeout the slave
    lets go. That is an SDA edge during SCL high, which the model reports;
    it is the only error."""
    data = pattern(64)
    data[8] = 0x00
    b = Bench(data=data)
    b.run([START, ("write", b.W), ("write", 8), START, ("write", b.R), ("high", 1500), ("rbits", 1)], tail=0)
    mm = b.master
    assert mm.bits == [(0, mm.bits[0][1])]
    assert len(mm.errors) == 1 and mm.errors[0][0] == "sda changed while scl high"
    rise = [c for c, _ in mm.scl_low][-1]                   # start of the long high phase
    assert TMO_LO - 10 <= mm.errors[0][1] - rise <= TMO_HI + 10, mm.errors[0][1] - rise
    assert b.at_rest()
    b.run([("rbits", 8), STOP] + read_ops(b, 1))
    assert [lv for lv, _ in mm.bits[1:]] == [1] * 8
    assert len(mm.errors) == 1              # nothing new
    assert mm.read == [data[9]]


def test_slow_but_alive_master_is_not_timed_out():
    """A clock pulse that takes less than the timeout costs nothing, whether
    the master dawdles with SCL low or with SCL high, while the slave
    receives, ACKs or sends a 0."""
    data = pattern(64)
    data[3] = 0x00
    data[4] = 0x7F
    b = Bench(data=data)
    pause = TMO_LO - 100
    b.run([START, ("wbits", b.W, 4), ("wait", pause), ("wbits", (b.W << 4) & 0xFF, 4),
           ("wait", pause), ("rbits", 1),                          # the address ACK, held low for a long time
           ("high", pause), ("write", 3), ("wait", pause),
           START, ("write", b.R), ("wait", pause), ("read", True),  # a 0 from the slave waits for SCL to rise
           ("high", pause), ("read", False), STOP])                # and another one for SCL to fall
    assert b.master.errors == []
    assert b.master.bits[0][0] == 0
    assert b.master.read == data[3:5]
    assert b.at_rest()


# ---------------------------------------------------------------- everything, at random

class Reference:
    """What the bus must show, written from the 24Cxx rules and the write
    buffer of the firmware header: pointer, wrap, eight pairs per drain."""

    def __init__(self, data):
        self.data, self.size = list(data), len(data)
        self.ptr = self.count = self.pending = 0
        self.read, self.acks, self.pairs = [], [], []

    def data_byte(self, v):
        if self.pending == 0:
            self.count = 0
        if self.count == 8:
            return False
        self.count += 1
        self.pending += 2
        self.pairs += [self.ptr, v]
        self.ptr = (self.ptr + 1) % self.size
        return True

    def next_byte(self):
        v = self.data[self.ptr]
        self.ptr = (self.ptr + 1) % self.size
        return v


@pytest.mark.parametrize("seed,quarter,hold", [(1, Q_MIN, None), (2, Q_MIN, 1), (3, 9, 3), (4, 11, None),
                                               (5, 30, 1), (6, Q_MIN, 2)])
def test_random_transactions(seed, quarter, hold):
    rng = random.Random(seed)
    data = [rng.randrange(256) for _ in range(64)]
    b = Bench(data=data, quarter=quarter, lead=40 + rng.randrange(8), hold=hold)
    ref = Reference(data)
    drained = []
    in_transfer = False
    for _ in range(60):
        ops = [START]
        kind = rng.choice(["ptr", "write", "read", "ptr+read", "other", "cut"])
        if kind in ("ptr", "write", "ptr+read"):
            p = rng.randrange(256)
            ops += [("write", b.W), ("write", p)]
            ref.acks += [(b.W, True), (p, True)]
            ref.ptr = p % ref.size
        if kind == "write":
            alive = True
            for _ in range(rng.randrange(1, 6)):
                v = rng.randrange(256)
                alive = alive and ref.data_byte(v)
                ops.append(("write", v, alive))
                ref.acks.append((v, alive))
        if kind == "ptr+read":
            ops.append(START)
        if kind in ("read", "ptr+read"):
            n = rng.randrange(1, 5)
            ops.append(("write", b.R))
            ref.acks.append((b.R, True))
            for i in range(n):
                ops.append(("read", i < n - 1))
                ref.read.append(ref.next_byte())
        if kind == "other":
            a = rng.choice([x for x in range(128) if x != b.addr])
            rw = rng.randrange(2)
            ops.append(("write", (a << 1) | rw, False))
            ref.acks.append(((a << 1) | rw, False))
            if rw:
                ops.append(("read", False))
                ref.read.append(0xFF)
            else:
                v = rng.randrange(256)
                ops.append(("write", v, False))
                ref.acks.append((v, False))
        if kind == "cut":                   # a byte cut short: address, pointer or data
            depth = rng.randrange(3)
            if depth >= 1:
                ops.append(("write", b.W))
                ref.acks.append((b.W, True))
            if depth == 2:
                p = rng.randrange(256)
                ops.append(("write", p))
                ref.acks.append((p, True))
                ref.ptr = p % ref.size
            ops.append(("wbits", rng.randrange(256), rng.randrange(1, 8)))
        in_transfer = rng.random() < 0.25   # sometimes run into the next START without a STOP
        if not in_transfer:
            ops += [STOP, ("wait", rng.randrange(12))]
        b.run(ops, tail=0)
        if not in_transfer and rng.random() < 0.3:
            got = b.outbox()                # the host empties the outbox
            drained += got
            ref.pending = 0
    b.run([STOP] if in_transfer else [])
    drained += b.outbox()
    assert b.master.errors == []
    assert b.master.read == ref.read
    assert b.master.acks == ref.acks
    assert drained == ref.pairs
    assert b.at_rest()


# ---------------------------------------------------------------- the project's own master

def test_keyer_master_talks_to_keyer_slave_on_one_chip():
    """fw/i2c_master.s on thread 0 (uio2, uio3) and this slave on thread 1
    (moved to uio4, uio5), the two pairs of pins wired together outside the
    chip. The master firmware and its bytecode were written without this
    slave in mind, so this checks the slave against a second, independent
    idea of the bus."""
    with open(os.path.join(FWDIR, "i2c_master.s")) as f:
        mwords, msyms, _ = keyerasm.assemble(f.read(), symbols={"I2C_Q": 16})
    org = max(mwords) + 1
    data = [0x81, 0x00, 0xFF, 0x5A, 0x3C, 0xA7, 0x12, 0xE0]
    with open(FW) as f:
        swords, ssyms, _ = keyerasm.assemble(f.read(), origin=org, symbols=dict(
            FAST, SCL=4, SDA=5, TABLE_BYTES=len(data), TABLE=256 - 2 * len(data)))
    assert max(swords) < ssyms["TABLE"]
    image = dict(mwords)
    image.update(swords)
    m = Machine()
    m.load(keyerasm.to_list(image))
    m.load(table_words(data), base=ssyms["TABLE"])

    class Wires:                            # uio2 - uio4 (SCL) and uio3 - uio5 (SDA), pull-ups
        def on_cycle(self, mm):
            low = mm.uio_oe & ~mm.uio_out
            ext = 0xFF
            for a, b in ((2, 4), (3, 5)):
                if low & (1 << a):
                    ext &= ~(1 << b)
                if low & (1 << b):
                    ext &= ~(1 << a)
            mm.ext_uio = ext

    cmds = ([0x01, 0x03, 2, 0xA0, 6, 0x01, 0x03, 1, 0xA1, 0x04, 4, 0x02]    # pointer 6, repeated START, read 4
            + [0x01, 0x03, 4, 0xA0, 3, 0xC3, 0x5A, 0x02]                    # pointer 3, two data bytes
            + [0x01, 0x03, 1, 0xA1, 0x04, 1, 0x02]                          # current-address read
            + [0x01, 0x03, 2, 0xA2, 0x55, 0x02])                            # somebody else
    host, wires = pm.HostFeeder(0, cmds), Wires()
    m.host_set_pc(1, ssyms["i2c_init"])
    m.host_run(1, True)
    for _ in range(40):                     # the slave is listening before the master starts
        wires.on_cycle(m)
        m.step()
    m.host_run(0, True)
    for _ in range(30000):
        host.on_cycle(m)
        wires.on_cycle(m)
        m.step()
    assert not host.pending
    master_out = list(m.threads[0].outbox)
    assert master_out == [0, 0, data[6], data[7], data[0], data[1],         # two ACKed writes, four bytes
                          0, 0, data[5],                                    # ACKed, ACKed, one byte
                          1], master_out                                    # first byte NACKed
    assert list(m.threads[1].outbox) == [3, 0xC3, 4, 0x5A]
    t0, t1 = m.threads
    assert t0.blocked and t0.pc == msyms["i2c_cmd"] + 1                     # waiting for a command
    assert t1.pc in (ssyms["clk_h1"], ssyms["clk_h1"] + 1) and t1.lr == ssyms["listen"]   # for a START
    assert m.uio_oe & 0x3C == 0


# ---------------------------------------------------------------- the model itself

def _bare_bus(ops, quarter=6, rogue=None, lines=None, **kw):
    """The master model on a bus with no slave: a machine in which no thread
    runs. `rogue(mm)` returns the lines (a uio mask) some other device pulls
    low in the coming cycle; it is applied through the machine's pin
    drivers, which are all 0 after reset."""
    m = Machine()
    mm = I2cMasterModel(scl=SCL, sda=SDA, quarter=quarter, **kw)
    mm.queue(ops)
    n = 0
    while not mm.idle:
        if rogue:
            m.uio_oe = rogue(mm)
        mm.on_cycle(m)
        m.step()
        n += 1
        assert n < 100000
    return mm


def test_model_timing_on_an_empty_bus():
    q = 6
    mm = _bare_bus([START, ("write", 0xA5, None), ("read", False), STOP,
                    START, ("write", 0x00), START, ("write", 0xFF, False), STOP], q)
    assert mm.acks == [(0xA5, False), (0x00, False), (0xFF, False)]
    assert mm.read == [0xFF]
    assert [e[0] for e in mm.errors] == ["missing ack"] and mm.errors[0][2] == 0x00
    assert [k for k, _ in mm.events] == ["start", "stop", "start", "start", "stop"]
    assert {n for _, n in mm.scl_low} == {2 * q}
    # high: a bit, a repeated START (set-up + hold), STOP + bus free + START
    assert {n for _, n in mm.scl_high} == {2 * q, 4 * q, 6 * q}
    assert all(n == 0 for _, n in mm.stretches)
    assert min(n for _, n in mm.setup) == q                 # data changes in the middle of SCL low
    assert {(h, who) for _, h, who in mm.hold} == {(q, "m")}
    starts = [c for k, c in mm.events if k == "start"]
    falls = [c - n for c, n in mm.scl_low]
    assert all(min(f - s for f in falls if f > s) == 2 * q for s in starts)   # START hold time


def test_model_reports_each_violation():
    q = 6
    sda, scl = 1 << SDA, 1 << SCL
    byte = [START, ("write", 0xFF, None), STOP]

    def after_rise(mm, k, lo, hi):          # inside the k-th SCL high phase, lo..hi cycles in
        return len(mm.scl_low) == k and len(mm.scl_high) == k - 1 and lo <= mm.cycle + 1 - mm.scl_low[-1][0] < hi

    # SDA pulled low and let go again in the middle of an SCL high phase
    mm = _bare_bus(byte, q, lambda mm: sda if after_rise(mm, 3, 2, 4) else 0)
    assert [e[0] for e in mm.errors] == ["sda changed while scl high"] * 2
    # SDA held low right through a 1 the master sends
    mm = _bare_bus(byte, q, lambda mm: sda if len(mm.scl_high) == 2 else 0)
    assert [e[0] for e in mm.errors] == ["arbitration lost"]
    # SCL pulled low while it is high
    mm = _bare_bus(byte, q, lambda mm: scl if after_rise(mm, 2, 3, 6) else 0)
    assert [e[0] for e in mm.errors] == ["scl pulled low while high"]
    # SCL never let go: a stretch past the limit (and one within it is honoured)
    mm = _bare_bus(byte, q, lambda mm: scl if len(mm.scl_high) == 2 else 0, stretch_limit=40)
    assert mm.errors and {e[0] for e in mm.errors} == {"stretch limit"}
    # (rogue() runs before the model's on_cycle, so mm.cycle is the cycle before the one it decides)
    mm = _bare_bus(byte, q, lambda mm: scl if len(mm.scl_high) == 2 and mm.cycle + 1 - mm.scl_high[-1][0] < 30 else 0,
                   stretch_limit=40)
    assert mm.errors == [] and max(n for _, n in mm.stretches) == 30 - 2 * q
    assert max(n for _, n in mm.scl_low) == 30
    # an ACK the script forbids, and none where it demands one
    mm = _bare_bus([START, ("write", 0xFF, False), STOP], q, lambda mm: sda if len(mm.scl_high) == 8 else 0)
    assert [e[0] for e in mm.errors] == ["unexpected ack"] and mm.acks == [(0xFF, True)]
    mm = _bare_bus([START, ("write", 0xFF, True), STOP], q)
    assert [e[0] for e in mm.errors] == ["missing ack"] and mm.acks == [(0xFF, False)]
    # SDA pulled low on the idle bus and never let go: no START, no STOP
    mm = _bare_bus([("wait", 4), START, ("write", 0x00, None), STOP], q, lambda mm: sda)
    assert [e[0] for e in mm.errors] == ["sda changed while scl high", "sda low before start", "sda low after stop"]
    assert mm.events == []
    # SDA let go in the very cycle SCL rises (2 q after it fell)
    mm = _bare_bus(byte, q,
                   lambda mm: sda if len(mm.scl_high) == 2 and mm.cycle + 1 - mm.scl_high[-1][0] < 2 * q else 0)
    assert [e[0] for e in mm.errors] == ["sda changed at scl edge"]
