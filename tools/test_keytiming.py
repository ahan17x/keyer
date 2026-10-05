"""pytest for tools/keytiming.py, the static timing check of
`keyerasm.py --check-timing`: slot counts on small programs written for
the purpose, every status, the annotations, a comparison with what the
golden model actually executes, and the firmware in fw/ (no fault)."""

import glob
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyer_isa as isa  # noqa: E402
import keyerasm  # noqa: E402
import keyersim  # noqa: E402
import keytiming as kt  # noqa: E402

FW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fw")


def check(src, symbols=None):
    """{source line of the WAITD: its worst Result}, the program, the report."""
    words, _, _, lines = keyerasm.assemble_lines(src, symbols=symbols)
    prog, results = kt.analyse(words, lines)
    text, faults = kt.report(prog, results, verbose=True)
    return {prog.line[r.waitd]: r for r in kt.worst(results) if r.waitd is not None}, text, faults


def test_straight_line_and_budget():
    r, text, faults = check("""
        ldi   r1, 20
        sett  r1
        waitd 1             ; line 4: the slot after the SETT
        nop
        nop
        waitd 2             ; line 7: three slots after the first WAITD
        halt
    """)
    assert (r[4].slots, r[4].budget, r[4].status) == (1, 10, "ok")
    assert (r[7].slots, r[7].budget, r[7].status) == (3, 20, "ok")
    assert faults == 0 and "2 WAITD checked, 0 faults" in text


def test_late_and_exit_status(tmp_path, capsys):
    src = """
        ldi   r1, 6
        sett  r1
    loop:
        waitd 1
        nop
        nop
        nop
        bra   loop          ; five slots from WAITD to WAITD, three fit in a period of 6
    """
    r, text, faults = check(src)
    assert (r[5].slots, r[5].budget, r[5].status) == (5, 3, "LATE") and faults == 1
    f = tmp_path / "late.s"
    f.write_text(src)
    assert keyerasm.main([str(f), "--check-timing"]) == 1
    assert "LATE" in capsys.readouterr().out
    f.write_text(src.replace("ldi   r1, 6", "ldi   r1, 10"))
    assert keyerasm.main([str(f), "--check-timing"]) == 0


def test_djnz_constant_gives_the_loop_bound():
    r, _, faults = check("""
        ldi   r1, 100
        sett  r1
        waitd 1
        ldi   r2, 7
    spin:
        nop
        djnz  r2, spin      ; 7 times round: 14 slots
        waitd 1             ; line 9: LDI + 14 + this = 16
        halt
    """)
    assert (r[9].slots, r[9].status) == (16, "ok") and faults == 0


def test_delay_costs_its_slots_and_calls_are_followed():
    r, _, _ = check("""
        ldi   r1, 200
        sett  r1
        waitd 1
        call  sub           ; 1
        call  sub           ; 1
        waitd 1             ; line 7: 2 calls + 2 * (DELAY 9 = 10, RET = 1) + this = 25
        halt
    sub:
        delay 9
        ret
    """)
    assert r[7].slots == 25


def test_unbounded_loop_needs_a_bound():
    src = """
        ldi   r1, 100
        sett  r1
        waitd 1
    poll:
        inr   r0, ui3
        bne   poll%s
        waitd 1             ; line 8
        halt
    """
    r, text, faults = check(src % "")
    assert r[8].status == "UNBOUNDED" and r[8].slots is None and faults == 1
    assert "no bound: the loop at line 7" in text
    r, _, faults = check(src % "          ; bound 4")
    # taken 4 times: 5 passes of INR, BNE, then the WAITD
    assert (r[8].slots, r[8].status) == (11, "ok") and faults == 0
    # a DJNZ whose count is not a constant
    src = """
        ldi   r1, 100
        sett  r1
        pop   r2
        waitd 1
    again:
        nop
        djnz  r2, again%s
        waitd 1             ; line 9
        halt
    """
    assert check(src % "")[0][9].status == "UNBOUNDED"
    assert check(src % "    ; bound 3")[0][9].slots == 2 * 4 + 1


def test_setd_anchor_budgets():
    src = """
        ldi   r1, 10
        sett  r1
        pop   r0
        setd  %d
        %s
        waitd %d            ; line 7
        halt
    """
    # SETD 2, WAITD 1: 3 ticks, at least 2: 15 slots, 10 if the phase is against it
    r = check(src % (2, "delay 7", 1))[0][7]
    assert (r.slots, r.budget, r.budget_min, r.status) == (9, 15, 10, "ok")
    r = check(src % (2, "delay 11", 1))[0][7]
    assert (r.slots, r.status) == (13, "MARGIN")
    r = check(src % (2, "delay 20", 1))[0][7]
    assert (r.slots, r.status) == (22, "LATE")
    # SETD 0 directly before WAITD 1 waits 0 to 1 tick by design
    r = check(src % (0, "nop", 1))[0][7]
    assert (r.slots, r.budget, r.budget_min, r.status) == (2, 5, 0, "ok")


def test_blocking_instruction_is_named_and_can_be_waived():
    src = """
        ldi   r1, 50
        sett  r1
        waitd 1
        wt1   ui3
        waitd 1%s
        halt
    """
    r, text, faults = check(src % "")
    assert (r[6].status, r[6].slots, faults) == ("WAITS", 2, 1) and "waits at line 5 (wt1 ui3)" in text
    r, text, faults = check(src % "         ; timing: the peer holds the line at most a bit time")
    assert r[6].status == "WAITS" and faults == 0
    assert "WAITS (waived)" in text and "waiver: the peer holds the line at most a bit time" in text


def test_unknown_period_reports_the_period_needed():
    r, text, faults = check("""
        pop   r1
        sett  r1
    loop:
        waitd 1
        nop
        nop
        bra   loop
    """)
    assert (r[5].slots, r[5].period, r[5].status, r[5].need) == (4, None, "period?", 8) and faults == 0
    assert "on time for a period of at least 8 cycles" in text


def test_flags_prune_and_jmpr_follows_loaded_labels():
    r, _, _ = check("""
        ldi   r1, 100
        sett  r1
        waitd 1
        ldi   r0, 3
        cmpi  r0, 3
        bne   slow          ; never taken: Z is known
        ldi   r5, fast
        jmpr  r5
    slow:
        delay 40
    fast:
        waitd 1             ; line 13
        halt
    """)
    assert r[13].slots == 6
    # an unknown register: every label loaded into that register
    r, _, _ = check("""
        ldi   r1, 100
        sett  r1
        pop   r0
        cmpi  r0, 0
        ldi   r5, a
        beq   go
        ldi   r5, b
    go: waitd 1
        jmpr  r5
    a:  delay 3
    b:  waitd 1             ; line 12: JMPR, DELAY 3 (4 slots), this
        halt
    """)
    assert r[12].slots == 6


# ---------------------------------------------------------------- against the golden model

def _observed(src, symbols, cycles, feed=()):
    """Run thread 0 on the golden model and measure, for every WAITD, the
    largest number of slots from the completion of the previous deadline
    instruction to the WAITD's first evaluation. {address: slots}."""
    words, _, _, lines = keyerasm.assemble_lines(src, symbols=symbols)
    m = keyersim.Machine(trace=True)
    m.load(keyerasm.to_list(words))
    for b in feed:
        m.host_inbox_push(0, b)
    m.host_run(0, True)
    m.run(cycles)
    worst, anchor_cycle, waiting = {}, None, None
    for rec in m.trace:
        if rec.tid != 0:
            continue
        ins, _ = isa.decode(rec.word)
        name = ins.name if ins else None
        if name == "WAITD" and anchor_cycle is not None and waiting != rec.pc:
            n = (rec.cycle - anchor_cycle) // 2
            worst[rec.pc] = max(worst.get(rec.pc, 0), n)
            waiting = rec.pc                      # later evaluations of a blocked WAITD are not first ones
        if name in kt.ANCHORS and rec.done:
            anchor_cycle, waiting = rec.cycle, None
    return worst, words, lines


@pytest.mark.parametrize("name,symbols,feed", [
    ("uart.s", {"BAUD_DIV": 40}, [0x55, 0xA3, 0x00]),
    ("spi_master.s", {}, [2, 0x12, 0x34]),
    ("ws2812.s", {}, [2, 0, 0xF0, 0x0F]),
])
def test_static_bound_covers_what_the_model_executes(name, symbols, feed):
    """The static count is an upper bound on what runs, and for these
    programs' bit loops it is exact."""
    src = open(os.path.join(FW, name)).read()
    seen, words, lines = _observed(src, symbols, 6000, feed)
    prog, results = kt.analyse(words, lines)
    static = {r.waitd: r for r in kt.worst(results) if r.waitd is not None}
    assert seen, "the program never reached a WAITD"
    for pc, n in seen.items():
        assert static[pc].slots is not None and n <= static[pc].slots, (prog.line[pc], n, static[pc].slots)
    assert any(n == static[pc].slots for pc, n in seen.items())


@pytest.mark.parametrize("path", sorted(glob.glob(os.path.join(FW, "*.s"))))
def test_firmware_has_no_timing_fault(path, capsys):
    assert keyerasm.main([path, "--check-timing"]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "LATE" not in out and "UNBOUNDED" not in out and "LIMIT" not in out
