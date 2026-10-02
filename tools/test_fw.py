"""Firmware tests on the ISS with independent protocol models.
Run: python3 -m pytest tools/test_fw.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyer_isa  # noqa: E402
import keyerasm  # noqa: E402
import protomodels as pm  # noqa: E402
from keyersim import Machine  # noqa: E402

FW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fw")


def load_fw(name, symbols=None):
    with open(os.path.join(FW, name)) as f:
        src = f.read()
    words, syms, _ = keyerasm.assemble(src, symbols=symbols)
    assert max(words) < 256, "program too large: %d words" % (max(words) + 1)
    return keyerasm.to_list(words), syms


def run(m, cycles, models):
    for _ in range(cycles):
        for mod in models:
            mod.on_cycle(m)
        m.step()


# ---------------------------------------------------------------- UART

def test_uart_tx_bytes_and_bit_timing():
    P = 52                                  # 60 MHz / 1.15 Mbaud, a fast case
    words, syms = load_fw("uart.s", {"BAUD_DIV": P})
    m = Machine()
    m.load(words)
    data = [0x55, 0x00, 0xFF, 0xA3, 0x7E]
    for b in data:
        m.host_inbox_push(0, b)
    m.host_run(0, True)
    dec = pm.UartDecoder(pin=18, period=P)
    run(m, P * 14 * len(data) + 200, [dec])
    assert dec.bytes == data
    assert dec.errors == []
    # within each byte, every edge must sit on the bit grid of its start edge
    for s0 in dec.starts:
        for e in dec.edges:
            if s0 < e < s0 + 9 * P + P // 2:
                k = (e - s0) / P
                assert abs(k - round(k)) * P <= 2, (s0, e)


def test_uart_rx_sample_point_is_centred():
    """The RX firmware's first sample must land near 1.5 bit periods after the
    start edge, even though edge detection, SETT and the slot grid each add a
    few cycles. The firmware compensates with a calibrated constant."""
    P = 100
    words, syms = load_fw("uart.s", {"BAUD_DIV": P})
    m = Machine(trace=True)
    m.load(words)
    m.host_set_pc(1, syms["rx_init"])
    m.host_run(1, True)
    stim = pm.UartStimulus(ui_bit=3, period=P, data=[0x00], start=21)
    run(m, stim.done_at, [stim])
    edge = 21                                # start bit begins at this cycle
    samples = [r.cycle for r in m.trace if r.done and r.tid == 1
               and keyer_isa.disasm(r.word) == "RDC 11"]
    # RDC reads the level, i.e. the pad two cycles earlier
    for k, c in enumerate(samples[:8]):
        assert abs((c - 2) - (edge + 1.5 * P + k * P)) <= 4, (k, c - 2 - edge)


def test_uart_rx_with_baud_error():
    P = 60
    words, syms = load_fw("uart.s", {"BAUD_DIV": P})
    data = [0x00, 0xFF, 0x5A, 0xA5, 0x01, 0x80]
    for err in (-0.03, 0.0, 0.03):          # 3% baud error either way
        m = Machine()
        m.load(words)
        m.host_set_pc(1, syms["rx_init"])
        m.host_run(1, True)
        stim = pm.UartStimulus(ui_bit=3, period=int(round(P * (1 + err))), data=data)
        run(m, stim.done_at + 100, [stim])
        got = []
        while True:
            b = m.host_outbox_pop(1)
            if b is None:
                break
            got.append(b)
        assert got == data, (err, got)


def test_uart_full_duplex_loopback():
    """T1 receives what T0 transmits (TX wired to RX externally)."""
    P = 40
    words, syms = load_fw("uart.s", {"BAUD_DIV": P})
    m = Machine()
    m.load(words)
    data = list(range(0, 256, 17))
    for b in data:
        m.host_inbox_push(0, b)
    m.host_set_pc(1, syms["rx_init"])
    m.host_run(1, True)
    m.host_run(0, True)

    class Wire:
        def on_cycle(self, mm):
            tx = (mm.pad() >> 18) & 1
            mm.ext_ui = (mm.ext_ui & ~0x08) | (tx << 3)

    run(m, P * 14 * len(data) + 500, [Wire()])
    got = []
    while m.threads[1].outbox:
        got.append(m.host_outbox_pop(1))
    assert got == data


# ---------------------------------------------------------------- SPI

def test_spi_master_mode0():
    H = 15
    words, syms = load_fw("spi_master.s", {"SPI_HALF": H})
    m = Machine()
    m.load(words)
    frames = [[0x9F], [0x03, 0x00, 0x10, 0x00, 0x00, 0x00], [0xAA, 0x55]]
    for fr in frames:
        m.host_inbox_push(0, len(fr))
        for b in fr:
            m.host_inbox_push(0, b)
    m.host_run(0, True)
    slave = pm.SpiSlaveModel(sck=19, mosi=20, csn=21, miso_ui_bit=4,
                             reply=[0xEF, 0x40, 0x18, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66])
    run(m, 2 * H * 8 * 12 + 2000, [slave])
    assert slave.errors == []
    assert slave.frames == frames
    got = []
    while m.threads[0].outbox:
        got.append(m.host_outbox_pop(0))
    total = sum(len(f) for f in frames)
    assert got == (slave.reply * 3)[:total]
    # SCK period within a byte is exactly 2 * H (edges ride the timer grid);
    # between bytes the loop overhead stretches one low phase. Never faster.
    gaps = [b - a for a, b in zip(slave.sck_edges, slave.sck_edges[1:]) if b - a < 10 * H]
    assert gaps and all(g >= 2 * H for g in gaps), gaps
    within = [g for i, g in enumerate(gaps) if i % 8 != 7]
    assert all(g <= 2 * H + 2 for g in within), within


# ---------------------------------------------------------------- I2C

def _i2c_run(cmds, stretch=0, Q=40, cycles=200000):
    words, syms = load_fw("i2c_master.s", {"I2C_Q": Q})
    m = Machine()
    m.load(words)
    host = pm.HostFeeder(0, cmds)
    m.host_run(0, True)
    slave = pm.I2cSlaveModel(scl=2, sda=3, address=0x50, stretch=stretch,
                             min_high=Q, min_low=Q)
    run(m, cycles, [host, slave])
    assert not host.pending
    out = []
    while m.threads[0].outbox:
        out.append(m.host_outbox_pop(0))
    return m, slave, out


def test_i2c_write_then_read_with_repeated_start():
    cmds = ([0x01, 0x03, 4, 0xA0, 0x10, 0x11, 0x22, 0x02]        # START, write addr+ptr(0x10)+2 data, STOP
            + [0x01, 0x03, 2, 0xA0, 0x10]                       # START, write addr+ptr
            + [0x01, 0x03, 1, 0xA1, 0x04, 2, 0x02])             # repeated START, addr|R, read 2, STOP
    m, slave, out = _i2c_run(cmds)
    assert slave.errors == []
    assert [e[0] for e in slave.events] == ["start", "stop", "start", "start", "nack", "stop"]
    assert slave.mem[0x10:0x12] == [0x11, 0x22]
    assert out == [0, 0, 0, 0x11, 0x22]        # three write statuses, then the read data


def test_i2c_nack_on_wrong_address():
    cmds = [0x01, 0x03, 2, 0x42 << 1, 0x99, 0x02]
    m, slave, out = _i2c_run(cmds)
    assert out == [1]                          # first byte (the address) NACKed
    assert [e[0] for e in slave.events] == ["start", "stop"]


def test_i2c_clock_stretching():
    cmds = ([0x01, 0x03, 3, 0xA0, 0x10, 0x5C, 0x02]               # write 0x5C at 0x10
            + [0x01, 0x03, 2, 0xA0, 0x10]                       # set pointer 0x10
            + [0x01, 0x03, 1, 0xA1, 0x04, 1, 0x02])             # repeated start, read 1
    m, slave, out = _i2c_run(cmds, stretch=600)
    assert slave.errors == []
    assert out == [0, 0, 0, 0x5C]
    assert slave.mem[0x10] == 0x5C


def test_i2c_stuck_scl_times_out():
    """A slave that never releases SCL must not hang the thread: the WRITE is
    abandoned with status 0xFF, its queued bytes drained, the lines released,
    and the following STOP (SCL still held) also reports 0xFF. The thread is
    back at the command loop afterwards (DECISIONS D-018)."""
    Q = 40
    words, syms = load_fw("i2c_master.s", {"I2C_Q": Q, "I2C_TMO_PERIOD": 40, "I2C_TMO_TICKS": 10})
    cmds = [0x01, 0x03, 3, 0xA0, 0x10, 0x5C, 0x02]
    m = Machine()
    m.load(words)
    host = pm.HostFeeder(0, cmds)
    m.host_run(0, True)
    slave = pm.I2cSlaveModel(scl=2, sda=3, address=0x50, stretch=10 ** 9, min_high=Q, min_low=Q)
    run(m, 30000, [host, slave])
    assert not host.pending
    out = []
    while m.threads[0].outbox:
        out.append(m.host_outbox_pop(0))
    assert out == [0xFF, 0xFF], out
    t = m.threads[0]
    assert t.running and t.blocked and t.pc == syms["i2c_cmd"] + 1     # waiting for the next command
    assert m.uio_oe & 0x0C == 0                                       # both lines released
    assert slave.mem[0x10] == 0xFF                                    # the write never landed
