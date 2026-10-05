"""fw/swd.s on the golden model against an SW-DP target that knows only the
protocol (tools/protomodels_swd.py).
Run: python3 -m pytest tools/test_fw_swd.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyerasm  # noqa: E402
import protomodels as pm  # noqa: E402
from keyersim import Machine  # noqa: E402
from protomodels_swd import ACK_FAULT, ACK_OK, ACK_WAIT, SwdTargetModel  # noqa: E402

FW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fw", "swd.s")
DPIDR = 0x2BA01477
CONNECT, XFER, IDLE = 0x01, 0x02, 0x03
NO_RESPONSE = 7


def req(ap, rnw, addr):
    """The request byte as it goes on the wire, LSB first."""
    a2, a3 = (addr >> 2) & 1, (addr >> 3) & 1
    par = (ap + rnw + a2 + a3) & 1
    return 1 | (ap << 1) | (rnw << 2) | (a2 << 3) | (a3 << 4) | (par << 5) | (1 << 7)


def le32(v):
    return [(v >> (8 * i)) & 0xFF for i in range(4)]


RD_DPIDR, RD_CTRL, WR_CTRL, WR_SELECT, RD_AP0 = req(0, 1, 0), req(0, 1, 4), req(0, 0, 4), req(0, 0, 8), req(1, 1, 0)


def run(cmds, half=26, cycles=None, target=None, **kw):
    words, syms, _ = keyerasm.assemble(open(FW).read(), symbols={"SWD_HALF": half})
    m = Machine()
    m.load(keyerasm.to_list(words))
    kw.setdefault("setup", half - 5)          # the header: SWDIO changes 4 cycles after the falling edge
    kw.setdefault("hold", 8)                 # the last turnaround ends 14 cycles after its edge
    kw.setdefault("min_high", half)
    kw.setdefault("min_low", half)
    t = target or SwdTargetModel(swclk=19, swdio=0, dpidr=DPIDR, **kw)
    host = pm.HostFeeder(0, cmds)
    m.host_run(0, True)
    out = []
    clocks = 152 * cmds.count(CONNECT) + 60 * len(cmds)
    for _ in range(cycles or 2 * half * clocks + 400):
        host.on_cycle(m)
        t.on_cycle(m)
        m.step()
        b = m.host_outbox_pop(0)
        if b is not None:
            out.append(b)
    assert not host.pending
    th = m.threads[0]
    assert th.running and th.blocked and th.pc == syms["swd_cmd"], th      # back at the command loop
    return m, t, out


def test_request_byte():
    assert RD_DPIDR == 0xA5 and req(0, 1, 4) == 0x8D and req(0, 0, 8) == 0xB1 and req(1, 1, 0xC) == 0x9F


@pytest.mark.parametrize("half", [12, 14, 26, 32, 40, 64])
def test_connect_then_dpidr(half):
    m, t, out = run([CONNECT, XFER, RD_DPIDR], half=half)
    assert out == [ACK_OK] + le32(DPIDR) + [0], out
    assert [e[0] for e in t.events] == ["line reset", "swd selected", "line reset"]
    assert len(t.transactions) == 1 and t.transactions[0]["data"] == DPIDR
    assert t.errors == [], t.errors[:3]
    assert m.uio_oe & 1 and m.uio_out & 1 == 0 and (m.uo_out >> 3) & 1      # SWDIO driven low, SWCLK resting high


def test_clock_is_on_one_grid_and_counts_are_exact():
    half = 26
    m, t, out = run([CONNECT, XFER, RD_DPIDR, IDLE, XFER, RD_CTRL], half=half)
    assert t.errors == []
    # connect: 64 + 16 + 64 + 8 clocks; a transfer: 46; idle: 8
    assert len(t.rises) == 1 + 152 + 46 + 8 + 46
    grid = {c % half for c in t.rises + t.falls}
    assert len(grid) == 1, grid
    lows = [r - f for f, r in zip(t.falls, t.rises[1:])]      # rises[0] is the start-up edge to the rest level
    highs = [f - r for r, f in zip(t.rises[1:], t.falls[1:])]
    assert set(lows) == {half}                               # every low phase is exactly one half period
    assert min(highs) == half and all(h % half == 0 for h in highs)


def test_no_connect_no_answer():
    """Without the connect sequence the target stays in JTAG: no ACK is
    driven, the host reads the pull-up (7), skips the data phase and is back
    at its command loop."""
    m, t, out = run([XFER, RD_DPIDR])
    assert out == [NO_RESPONSE] and t.transactions == [] and t.errors == []
    assert len(t.rises) == 1 + 13                            # request, turnaround, ACK, turnaround


def test_first_request_after_reset_must_be_dpidr():
    m, t, out = run([CONNECT, XFER, RD_CTRL, IDLE, XFER, RD_DPIDR, XFER, RD_CTRL])
    assert out == [NO_RESPONSE] + [ACK_OK] + le32(DPIDR) + [0] + [ACK_OK] + le32(0) + [0]
    assert (RD_CTRL, "not DPIDR after a line reset") in [(r, why) for r, why, _ in t.ignored]


@pytest.mark.parametrize("ack", [ACK_WAIT, ACK_FAULT])
def test_wait_and_fault_skip_the_data_phase(ack):
    t = SwdTargetModel(swclk=19, swdio=0, dpidr=DPIDR, selected=True, setup=21, hold=8, min_high=26, min_low=26)
    t.next_ack = ack
    m, t, out = run([XFER, RD_DPIDR, XFER, RD_DPIDR], target=t)
    assert out == [ack] + [ACK_OK] + le32(DPIDR) + [0], out
    assert [tr["ack"] for tr in t.transactions] == [ack, ACK_OK] and t.errors == []
    assert len(t.rises) == 1 + 13 + 46


def test_data_parity_error_is_reported():
    t = SwdTargetModel(swclk=19, swdio=0, dpidr=DPIDR, selected=True, setup=21, hold=8, min_high=26, min_low=26)
    t.corrupt_parity = True
    m, t, out = run([XFER, RD_DPIDR, XFER, RD_DPIDR], target=t)
    assert out == [ACK_OK] + le32(DPIDR) + [1] + [ACK_OK] + le32(DPIDR) + [0], out


@pytest.mark.parametrize("value", [0x00000000, 0xFFFFFFFF, 0x50000001, 0x80000000, 0x12345678, 0x00010000])
def test_write_then_read_back(value):
    m, t, out = run([CONNECT, XFER, RD_DPIDR, XFER, WR_CTRL] + le32(value) + [IDLE, XFER, RD_CTRL])
    assert out == [ACK_OK] + le32(DPIDR) + [0] + [ACK_OK] + [ACK_OK] + le32(value) + [0], out
    w = t.transactions[1]
    assert (w["kind"], w["addr"], w["data"], w["parity_ok"]) == ("w", 4, value, True)
    assert t.ctrlstat == value and t.errors == [], t.errors[:3]


def test_select_write_and_ap_read():
    m, t, out = run([CONNECT, XFER, RD_DPIDR, XFER, WR_SELECT] + le32(0xA50000F0) + [XFER, RD_AP0])
    assert out == [ACK_OK] + le32(DPIDR) + [0, ACK_OK, ACK_OK, 0, 0, 0, 0, 0]
    assert t.select == 0xA50000F0 and t.transactions[2]["ap"] == 1 and t.errors == []


def test_request_with_bad_parity_gets_no_answer():
    m, t, out = run([CONNECT, XFER, RD_DPIDR, XFER, RD_CTRL ^ 0x20, IDLE, XFER, RD_CTRL])
    assert out == [ACK_OK] + le32(DPIDR) + [0] + [NO_RESPONSE] + [ACK_OK] + le32(0) + [0]
    assert (RD_CTRL ^ 0x20, "parity") in [(r, why) for r, why, _ in t.ignored]


def test_after_no_response_the_target_needs_idle_before_the_next_request():
    """The protocol, not the firmware: after an unanswered request the
    released line read as ones, so a request sent straight after it has no
    start bit the target can find. With IDLE in between it is answered."""
    m, t, out = run([CONNECT, XFER, RD_DPIDR, XFER, RD_CTRL ^ 0x20, XFER, RD_CTRL])
    assert out[6:] == [NO_RESPONSE, NO_RESPONSE]
    m, t, out = run([CONNECT, XFER, RD_DPIDR, XFER, RD_CTRL ^ 0x20, IDLE, XFER, RD_CTRL])
    assert out[6:] == [NO_RESPONSE, ACK_OK, 0, 0, 0, 0, 0]


def test_no_contention_and_turnaround_margins():
    """The host is off SWDIO before the target drives and comes back only
    after the target has let go, also with a target whose output is slow."""
    for delay in (0, 5, 20):
        m, t, out = run([CONNECT, XFER, RD_DPIDR, XFER, WR_CTRL] + le32(0x5A5A0F0F) + [XFER, RD_CTRL], delay=delay)
        assert out[-6:] == [ACK_OK] + le32(0x5A5A0F0F) + [0], (delay, out)
        assert t.errors == [], (delay, t.errors[:3])


def test_sample_point_margin():
    """The host samples one cycle before the next rising edge: a target
    output delay just under a clock period still works, a full period shifts
    every bit."""
    half = 26
    m, t, out = run([CONNECT, XFER, RD_DPIDR], half=half, delay=2 * half - 2)
    assert out == [ACK_OK] + le32(DPIDR) + [0]
    m, t, out = run([CONNECT, XFER, RD_DPIDR], half=half, delay=2 * half)
    assert out[0] != ACK_OK or out[1:5] != le32(DPIDR)


def test_host_that_feeds_slowly_keeps_the_grid():
    """Command bytes arriving late stall the thread between commands only
    (SWCLK resting high), never inside a transfer."""
    half = 26
    words, syms, _ = keyerasm.assemble(open(FW).read(), symbols={"SWD_HALF": half})
    m = Machine()
    m.load(keyerasm.to_list(words))
    t = SwdTargetModel(swclk=19, swdio=0, dpidr=DPIDR, selected=True, setup=half - 5, hold=8, min_high=half, min_low=half)
    m.host_run(0, True)
    feed = {300: XFER, 1777: WR_CTRL, 1900: 0x11, 2500: 0x22, 3333: 0x33, 9001: 0x44, 14000: XFER, 14500: RD_CTRL}
    out = []
    for c in range(22000):
        if c in feed:
            assert m.host_inbox_push(0, feed[c])
        t.on_cycle(m)
        m.step()
        b = m.host_outbox_pop(0)
        if b is not None:
            out.append(b)
    assert out == [ACK_OK, ACK_OK, 0x11, 0x22, 0x33, 0x44, 0], out
    assert t.errors == [] and len({c % half for c in t.rises + t.falls}) == 1
    assert {r - f for f, r in zip(t.falls, t.rises[1:])} == {half}


def test_below_the_fastest_half_period_the_model_objects():
    """The control for the timing checks, and the reason for the documented
    limit: at SWD_HALF = 10 the bit loop cannot keep up and a low phase is
    shorter than the half period."""
    words, syms, _ = keyerasm.assemble(open(FW).read(), symbols={"SWD_HALF": 10})
    m = Machine()
    m.load(keyerasm.to_list(words))
    t = SwdTargetModel(swclk=19, swdio=0, dpidr=DPIDR, selected=True, min_high=10, min_low=10)
    m.host_run(0, True)
    for b in (XFER, RD_DPIDR):
        m.host_inbox_push(0, b)
    for _ in range(3000):
        t.on_cycle(m)
        m.step()
    assert any(e[0].startswith("swclk") for e in t.errors)


def test_image_size():
    words, _, _ = keyerasm.assemble(open(FW).read())
    assert max(words) + 1 == 117
