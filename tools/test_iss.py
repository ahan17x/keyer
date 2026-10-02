"""pytest for the Keyer ISS and assembler. Run: python3 -m pytest tools/ -q"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyer_isa as isa  # noqa: E402
import keyerasm  # noqa: E402
from keyersim import Machine  # noqa: E402


def asm(src):
    words, syms, _ = keyerasm.assemble(src)
    return keyerasm.to_list(words), syms


def boot(src, cycles=0, t1=False, trace=False):
    m = Machine(trace=trace)
    words, syms = asm(src)
    m.load(words)
    m.host_run(0, True)
    if t1:
        m.host_set_pc(1, syms["t1"])
        m.host_run(1, True)
    m.run(cycles)
    return m, syms


def run_until_halt(m, limit=100000):
    for _ in range(limit):
        if not m.threads[0].running:
            return
        m.step()
    raise AssertionError("did not halt")


# ---- encoding / assembler -------------------------------------------------

def test_roundtrip_all():
    for ins in isa.INSTRUCTIONS:
        ops = {}
        for opname, fname in ins.operands:
            lsb, width, signed = isa.FIELDS[ins.major][fname]
            ops[opname] = -1 if signed else (5 if fname == "pin" else (1 << width) - 1)
        w = isa.encode(ins.name, **ops)
        d, dops = isa.decode(w)
        assert d is ins and dops == ops


def test_asm_labels_and_offsets():
    words, syms = asm("""
    start:  ldi r0, 1
    loop:   dec r0
            bne loop
            bra start
            bp0 uio3, loop
            jmp loop
            ldw r1, 0x1234
    """)
    assert syms["loop"] == 1
    assert isa.disasm(words[2]) == "BNE -2"
    assert isa.disasm(words[3]) == "BRA -4"
    assert isa.disasm(words[4]) == "BP0 3, -4"
    assert isa.disasm(words[5]) == "JMP 0x1"
    assert isa.disasm(words[6]) == "LDI r1, 52" and isa.disasm(words[7]) == "LDIH r1, 18"


def test_asm_range_errors():
    import pytest
    with pytest.raises(keyerasm.AsmError):
        asm("ldi r0, 300")
    with pytest.raises(keyerasm.AsmError):
        asm("set 24")
    with pytest.raises(keyerasm.AsmError):
        asm("bp0 uio0, far\n" + ".org 100\nfar: nop")


# ---- ALU and flags ----------------------------------------------------------

def test_alu_flags():
    m, _ = boot("""
        ldw r0, 0xFFFF
        ldi r1, 1
        add r0, r1      ; 0x0000, Z=1 C=1
        halt
    """)
    run_until_halt(m)
    t = m.threads[0]
    assert t.regs[0] == 0 and t.z == 1 and t.c == 1

    m, _ = boot("""
        ldi r0, 3
        ldi r1, 5
        sub r0, r1      ; 0xFFFE, C=1 (borrow)
        cmpi r0, 0xFE   ; 0xFFFE - 0xFE = 0xFF00, Z=0 C=0
        halt
    """)
    run_until_halt(m)
    t = m.threads[0]
    assert t.regs[0] == 0xFFFE and t.c == 0 and t.z == 0

    m, _ = boot("""
        ldi r0, 0x81
        shl r0          ; 0x102, C=0
        shr r0          ; 0x81, C=0
        shr r0          ; 0x40, C=1
        rcl r0          ; 0x81, C=0
        setc
        rcr r0          ; 0x40 | C<<15 = 0x8040, C=1
        swap r0         ; 0x4080
        rev8 r0         ; 0x4001
        halt
    """)
    run_until_halt(m)
    assert m.threads[0].regs[0] == 0x4001 and m.threads[0].c == 1


def test_call_ret_and_branches():
    m, _ = boot("""
        ldi r0, 0
        call sub
        addi r0, 10
        halt
    sub:    addi r0, 1
            ret
    """)
    run_until_halt(m)
    assert m.threads[0].regs[0] == 11


def test_nested_call_with_rdlr_jmpr():
    m, _ = boot("""
        ldi r0, 0
        call outer
        addi r0, 100
        halt
    outer:  rdlr r6
            call inner
            call inner
            jmpr r6
    inner:  addi r0, 1
            ret
    """)
    run_until_halt(m)
    assert m.threads[0].regs[0] == 102


# ---- timing ----------------------------------------------------------------

def test_slot_interleave_and_instruction_cost():
    # T0 runs 4 instructions then halts: they must retire on cycles 0,2,4,6
    m, _ = boot("nop\nnop\nnop\nhalt", trace=True)
    run_until_halt(m)
    assert [r.cycle for r in m.trace] == [0, 2, 4, 6]


def test_delay_slots():
    m, _ = boot("delay 3\nnop\nhalt", trace=True)
    run_until_halt(m)
    done = [r for r in m.trace if r.done]
    # DELAY 3 occupies 1+3 = 4 slots: cycles 0,2,4,6 ; completes at 6; nop at 8; halt at 10
    assert [r.cycle for r in done] == [6, 8, 10]


def _done_cycles(m, prefix):
    return [r.cycle for r in m.trace if r.done and isa.disasm(r.word).startswith(prefix)]


def test_timer_waitd_timing():
    # SEMANTICS 6.3. SETT executes at cycle 2 (second instruction). Period 10:
    # NOW becomes 1 at the end of cycle 12, 2 at 22, 5 at 52. WAITD 1 sees
    # NOW = 1 from cycle 13 on, so it completes at slot 14 (first even cycle
    # >= 13); the second at 24; WAITD 3 (target 5) at 54.
    m, _ = boot("""
        ldi r0, 10
        sett r0
        waitd 1
        waitd 1
        waitd 3
        halt
    """, trace=True)
    run_until_halt(m)
    assert _done_cycles(m, "WAITD") == [14, 24, 54]
    assert m.threads[0].deadline == 5 and m.threads[0].now == 5


def test_setd_and_timed_waits():
    # SETD 3 at slot 4 (NOW = 0): DEADLINE = 3, reached when NOW = 3, i.e.
    # from cycle 33 -> the timed wait gives up at slot 34 with C = 1.
    m, _ = boot("""
        ldi r0, 10
        sett r0
        setd 3
        wt1t ui3
        halt
    """, trace=True)
    m.ext_ui = 0
    run_until_halt(m)
    assert _done_cycles(m, "WT1T") == [34] and m.threads[0].c == 1
    # pin already high: completes at once with C = 0, deadline untouched
    m, _ = boot("""
        ldi r0, 10
        sett r0
        setd 3
        wt1t ui3
        halt
    """, trace=True)
    m.ext_ui = 0x08
    run_until_halt(m)
    assert _done_cycles(m, "WT1T") == [6] and m.threads[0].c == 0 and m.threads[0].deadline == 3
    # POPT on an empty inbox: SETT at slot 4, SETD 2 at slot 6, NOW = 2 from cycle 25 -> slot 26, rd unchanged
    m, _ = boot("""
        ldi r0, 10
        ldi r1, 0x77
        sett r0
        setd 2
        popt r1
        halt
    """, trace=True)
    run_until_halt(m)
    assert _done_cycles(m, "POPT") == [26] and m.threads[0].c == 1 and m.threads[0].regs[1] == 0x77
    # POPT with data: immediate, C = 0
    m, _ = boot("""
        ldi r0, 10
        sett r0
        setd 2
        popt r1
        halt
    """, trace=True)
    m.host_inbox_push(0, 0x42)
    run_until_halt(m)
    assert _done_cycles(m, "POPT") == [6] and m.threads[0].c == 0 and m.threads[0].regs[1] == 0x42
    # PUSHT on a full outbox times out, nothing pushed
    m, _ = boot("""
        ldi r0, 16
    fill: push r0
        djnz r0, fill
        ldi r0, 10
        sett r0
        setd 1
        pusht r0
        halt
    """)
    run_until_halt(m)
    assert m.threads[0].c == 1 and len(m.threads[0].outbox) == 16


def test_rdt_bdr_and_status_bit():
    # SETD 2 at slot 4: DEADLINE = 2. RDT at 6: NOW - DEADLINE = -2. RDS at 8:
    # bit 4 clear. BDR at 10 not taken. WAITD 0 blocks until NOW = 2 (slot 24).
    # Then RDT = 0, RDS bit 4 set, BDR taken.
    m, syms = boot("""
        ldi r0, 10
        sett r0
        setd 2
        rdt r1
        rds r2
        bdr bad
        waitd 0
        rdt r3
        rds r4
        bdr good
    bad: ldi r5, 1
    good: halt
    """, trace=True)
    run_until_halt(m)
    t = m.threads[0]
    assert t.regs[1] == 0xFFFE and not (t.regs[2] & 0x10)
    assert _done_cycles(m, "WAITD") == [24]
    assert t.regs[3] == 0 and (t.regs[4] & 0x10) and t.regs[5] == 0


def test_timer_disabled_freezes_now():
    m, _ = boot("""
        ldi r0, 0
        sett r0
        setd 0
        waitd 1
        halt
    """)
    m.run(200)
    t = m.threads[0]
    assert t.blocked and t.running and t.now == 0 and t.deadline == 0


def test_waitd_edges_on_grid_when_loop_is_on_time():
    # Period 20, two WAITD 1 per iteration, loop shorter than two periods.
    # SETT at cycle 2: NOW = k from cycle 20k + 3; WAITD completes at the
    # even slot 20k + 4, the pin instruction runs at 20k + 6, the pad changes
    # at 20k + 7. Every edge must sit on that grid.
    m, _ = boot("""
        ldi r0, 20
        sett r0
    loop:
        waitd 1
        set uo2
        waitd 1
        clr uo2
        delay 3
        bra loop
    """)
    m.run(600)
    ev = [e for e in m.pin_events if e[0] > 10]
    assert len(ev) >= 20
    assert all((e[0] - 7) % 20 == 0 for e in ev), ev


def test_waitd_catches_up_without_losing_ticks():
    # The loop starts about ten periods late (DELAY 40 = 41 slots = 82 cycles
    # at period 8). Each WAITD 1 then completes at once while DEADLINE is
    # behind NOW, DEADLINE advancing by one per WAITD, until the loop is back
    # on the tick grid. Nothing is lost: the number of completed WAITDs equals
    # the deadline count, and the late edges come out as a burst followed by
    # edges exactly one period apart.
    m, _ = boot("""
        ldi r0, 8
        sett r0
        delay 40
    loop:
        waitd 1
        set uo2
        waitd 1
        clr uo2
        bra loop
    """, trace=True)
    m.run(400)
    t = m.threads[0]
    n_waits = len(_done_cycles(m, "WAITD"))
    assert n_waits == t.deadline, (n_waits, t.deadline)
    assert t.now - t.deadline in (0, 1)          # caught up (within the tick in flight)
    ev = [e[0] for e in m.pin_events if e[0] > 10]
    gaps = [b - a for a, b in zip(ev, ev[1:])]
    assert min(gaps[:6]) < 8, gaps[:6]           # the catch-up burst
    assert all(g == 8 for g in gaps[-6:]), gaps[-6:]   # back on the grid, one edge per tick


def test_setpin_visible_next_cycle_and_sync_latency():
    m, _ = boot("set uo2\nnop\nhalt", trace=True)
    assert m.uo_out == 0
    m.step()                      # SET executes at cycle 0; register updates at its end
    assert m.uo_out & 4           # so the pad shows it during cycle 1
    # synchroniser: a change on ui3 at cycle c is seen by level at c+2
    m = Machine()
    words, _ = asm("""
    loop: inr r0, ui3
          bra loop
    """)
    m.load(words)
    m.host_run(0, True)
    m.ext_ui = 0
    for _ in range(6):
        m.step()
    m.ext_ui = 0x08               # pad changes during cycle 6
    m.step()                      # cycle 6: INR? (cycle 6 is T0 slot) sees pad(4)=0
    assert m.threads[0].regs[0] == 0
    m.step()                      # cycle 7: T1 (idle)
    m.step()                      # cycle 8: INR sees pad(6)=1
    assert m.threads[0].regs[0] == 1


def test_sync_latency_is_exactly_two_cycles():
    """A pad change during cycle c is visible to level() from cycle c+2, on
    both thread parities (this is where a one-cycle error would hide)."""
    for start_cycle in (6, 7):
        m = Machine(trace=True)
        words, syms = asm("""
        t0: inr r0, ui3
            bra t0
        t1: inr r0, ui3
            bra t1
        """)
        m.load(words)
        m.host_set_pc(1, syms["t1"])
        m.host_run(0, True)
        m.host_run(1, True)
        m.ext_ui = 0
        m.run(start_cycle)
        m.ext_ui = 0x08                     # pad high from cycle start_cycle
        seen = {}
        for _ in range(8):
            c = m.cycle
            m.step()
            r = m.trace[-1]
            if isa.disasm(r.word).startswith("INR"):
                seen[c] = m.threads[c & 1].regs[0]
        assert len(seen) >= 3
        # samples at start_cycle and start_cycle+1 still read 0; from +2 on they read 1
        for c, v in seen.items():
            assert v == (1 if c >= start_cycle + 2 else 0), (start_cycle, c, v)


def test_wait_edge():
    m = Machine(trace=True)
    words, _ = asm("""
        wtr ui0
        set uo3
        wtf ui0
        clr uo3
        halt
    """)
    m.load(words)
    m.host_run(0, True)
    m.ext_ui = 0
    m.run(10)
    m.ext_ui = 1                  # rises at cycle 10
    m.run(10)
    m.ext_ui = 0                  # falls at cycle 20
    m.run(10)
    done = [(r.cycle, isa.disasm(r.word)) for r in m.trace if r.done]
    # rise at 10 -> level=1 from cycle 12 -> WTR completes at slot 12, SET at 14
    # fall at 20 -> level=0 from 22 -> WTF at 22, CLR at 24, HALT at 26
    assert done == [(12, "WTR 8"), (14, "SET 19"), (22, "WTF 8"), (24, "CLR 19"), (26, "HALT")]


# ---- pins -------------------------------------------------------------------

def test_open_drain_never_drives_high():
    m, _ = boot("""
        od uio0
        set uio0        ; release
        clr uio0        ; drive low
        set uio0        ; release again
        oen uio0        ; ignored in OD mode
        halt
    """, trace=True)
    seen = []
    while m.threads[0].running:
        m.step()
        seen.append((m.uio_out & 1, m.uio_oe & 1))
    assert all(not (out and oe) for out, oe in seen)
    assert (0, 1) in seen and seen[-1] == (0, 0)


def test_push_pull_and_outb():
    m, _ = boot("""
        ldi r0, 0xA5
        outb r0
        ldi r1, 0xFF
        outoe r1
        oef uio7
        halt
    """)
    run_until_halt(m)
    assert m.uio_out == 0xA5 and m.uio_oe == 0x7F


def test_reserved_outputs_ignored():
    m, _ = boot("set uo0\nset uo1\nset uo2\nhalt")
    run_until_halt(m)
    assert m.uo_out == 0x04


# ---- fifos and threads ------------------------------------------------------

def test_fifo_blocking_pop_and_push():
    m, _ = boot("""
        pop r0
        inc r0
        push r0
        halt
    """, trace=True)
    m.run(20)                      # POP blocks: inbox empty
    assert m.threads[0].blocked and m.threads[0].pc == 0
    m.host_inbox_push(0, 0x41)
    run_until_halt(m)
    assert m.host_outbox_pop(0) == 0x42
    assert m.host_outbox_pop(0) is None


def test_outbox_full_blocks():
    m, _ = boot("""
        ldi r0, 0
    loop: push r0
          inc r0
          bra loop
    """)
    m.run(400)
    assert len(m.threads[0].outbox) == 16 and m.threads[0].blocked


def test_two_threads_and_status():
    m, syms = boot("""
        ldi r0, 1
        start           ; start T1
        push r0
        halt
    t1: ldi r0, 2
        push r0
        halt
    """, trace=True)
    # T1 is stopped at PC 0 until START; set its PC first as the host would
    m.host_set_pc(1, syms["t1"])
    m.run(40)
    assert m.host_outbox_pop(0) == 1 and m.host_outbox_pop(1) == 2
    assert all(t.halted for t in m.threads)
    t1 = [r for r in m.trace if r.tid == 1]
    assert all(r.cycle % 2 == 1 for r in t1)


def test_status_word():
    m, _ = boot("rds r0\nhalt")
    run_until_halt(m)
    s = m.threads[0].regs[0]
    assert s & 1 and s & 4 and not (s & 2) and not (s & 8) and not (s & 0x40)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))


# ---- capture and replay (docs/SEMANTICS.md section 14) ---------------------

def _cap_setup(m, group=0, mask=0xF, tpat=0, tmask=0, base=0x80, length=16):
    m.host_cap_cfg((group & 7) | ((mask & 0xF) << 4) | (((tpat & 0xF) | ((tmask & 0xF) << 4)) << 8))
    m.host_cap_buf((base & 0xFF) | ((length & 0xFF) << 8))


def _entries(m, base, n):
    return [(m.imem[(base + i) & 0xFF] >> 4, m.imem[(base + i) & 0xFF] & 0xF) for i in range(n)]


def test_capture_immediate_trigger_and_deltas():
    # T0 toggles uio0 at known slots; T1 stopped, so every even cycle (whose
    # fetch would serve T1) is a free port cycle. Trigger mask 0: triggers
    # the cycle after ARM.
    # ARM lands at the end of cycle 1 (host call after step 1) -> trigger at
    # cycle 2, entry 0 = {0, s(2)}. SET uio0 at slot 2 -> pad from 3 -> level
    # from 5 -> entry {3, 1} (5 - 2). CLR at slot 8 -> level 11 -> entry {6, 0}.
    m, _ = boot("""
        oen uio0
        set uio0
        nop
        nop
        clr uio0
        halt
    """)
    m.ext_uio = 0
    m.step(); m.step()                       # cycles 0, 1 (OEN at 0)
    _cap_setup(m, mask=0x1, length=8)
    m.host_cr_ctrl(1)                        # ARM at the end of cycle 1
    m.run(60)
    cr = m.cr
    assert cr.cap_trig and not cr.cap_ovf and cr.cap_armed
    assert cr.cap_w == 3 and cr.cap_n == 3
    assert _entries(m, 0x80, 3) == [(0, 0), (3, 1), (6, 0)]
    m.host_cr_ctrl(2)                        # DISARM: completes
    m.run(4)
    assert cr.cap_done and not cr.cap_armed and cr.cap_w == 3


def test_capture_transition_trigger_and_done():
    # Trigger on uio1 going high (pattern 0b10 under mask 0b10) while uio1 is
    # already high at arm time: no trigger until it falls and rises again.
    m, _ = boot("""
        oen uio1
        set uio1
        delay 10
        clr uio1
        delay 10
        set uio1
        delay 10
        clr uio1
        delay 10
        set uio1
        halt
    """)
    m.ext_uio = 0
    m.run(10)                                # SET at slot 2, pad high from 3
    _cap_setup(m, mask=0x2, tpat=0x2, tmask=0x2, length=3)
    m.host_cr_ctrl(1)
    m.run(200)
    cr = m.cr
    assert cr.cap_trig and cr.cap_done and not cr.cap_armed and cr.cap_w == 3
    e = _entries(m, 0x80, 3)
    assert e[0] == (0, 2) and e[1][1] == 0 and e[2][1] == 2        # rise, fall, rise
    assert e[1][0] == 24 and e[2][0] == 24                         # DELAY 10 + CLR/SET = 12 slots


def test_capture_idle_entry_and_length_zero():
    m, _ = boot("""
        oen uio0
    loop: bra loop
    """)
    m.ext_uio = 0
    m.step(); m.step()
    _cap_setup(m, mask=0x1, length=4)
    m.host_cr_ctrl(1)
    m.run(4095 * 2 + 20)
    assert _entries(m, 0x80, 3)[:3] == [(0, 0), (4095, 0), (4095, 0)]
    # length 0: done at once, nothing written
    m2, _ = boot("nop\nhalt")
    m2.step(); m2.step()
    _cap_setup(m2, length=0)
    m2.host_cr_ctrl(1)
    m2.run(6)
    assert m2.cr.cap_done and not m2.cr.cap_armed and m2.cr.cap_w == 0


def test_capture_overflow_when_both_threads_run():
    # Both threads running: no free port cycle. The trigger entry and one
    # change fill the two-entry queue; the next change is lost and flagged.
    m, syms = boot("""
        oen uio0
        oen uio1
        set uio0
        set uio1
        clr uio0
        clr uio1
    spin: bra spin
    t1: bra t1
    """, t1=True)
    m.ext_uio = 0
    m.run(4)
    _cap_setup(m, mask=0x3, length=16)
    m.host_cr_ctrl(1)
    m.run(40)
    cr = m.cr
    assert cr.cap_ovf and not cr.cap_armed and cr.cap_w == 0 and cr.q_count == 2
    m.host_run(1, False)                     # free the port: the queue drains
    m.run(10)
    assert cr.cap_w == 2 and cr.q_count == 0 and cr.cap_done


def test_capture_uses_only_free_slots_and_never_disturbs_the_thread():
    # The same program with and without a capture retires at the same cycles.
    src = """
        oen uio0
    loop: set uio0
        clr uio0
        djnz r1, loop
        halt
    """
    m0, _ = boot("ldi r1, 20\n" + src, trace=True)
    m0.ext_uio = 0
    m0.run(300)
    m1, _ = boot("ldi r1, 20\n" + src, trace=True)
    m1.ext_uio = 0
    m1.step(); m1.step()
    _cap_setup(m1, mask=0x1, length=64)
    m1.host_cr_ctrl(1)
    m1.run(298)
    assert [(r.cycle, r.pc) for r in m0.trace] == [(r.cycle, r.pc) for r in m1.trace]
    assert m1.cr.cap_w == 41 and not m1.cr.cap_ovf


def test_replay_timing_and_done():
    # Host-written entries on group 0, drive mask uio0|uio1 (push-pull, OEN
    # by the host-written firmware first). Entry k applies max(delta,1)
    # cycles after entry k-1; the pad shows it one cycle later.
    m, _ = boot("""
        oen uio0
        oen uio1
        halt
    """)
    m.ext_uio = 0
    run_until_halt(m)
    m.run(4)
    entries = [(0, 0b01), (3, 0b10), (1, 0b11), (0, 0b00), (7, 0b01)]
    for i, (d, p) in enumerate(entries):
        m.imem[0xA0 + i] = (d << 4) | p
    m.host_rep_cfg(0 | (0x3 << 4))
    m.host_rep_buf(0xA0 | (len(entries) << 8))
    m.host_cr_ctrl(4)                        # START at the end of this cycle
    m.run(60)
    cr = m.cr
    assert cr.rep_done and not cr.rep_active and not cr.rep_under and cr.rep_k == 5
    ev = [e for e in m.pin_events if e[0] > 4]
    vals = [e[1] & 0x3 for e in ev]
    assert vals == [0b01, 0b10, 0b11, 0b00, 0b01], ev
    gaps = [b[0] - a[0] for a, b in zip(ev, ev[1:])]
    assert gaps == [3, 1, 1, 7], gaps


def test_replay_length_zero_uses_last_capture_and_open_drain():
    # Capture a waveform on uio2 (OD, driven by the host's external level),
    # then replay it with rep_len = 0 on the same pin, open-drain: the drive
    # is uio_oe toggling with uio_out = 0.
    m, _ = boot("""
    loop: bra loop
    """)
    m.host_pinmode(0x04)                     # uio2 open-drain
    m.ext_uio = 0xFF
    m.step(); m.step()
    _cap_setup(m, mask=0x4, length=8)
    m.host_cr_ctrl(1)
    m.run(5)
    m.ext_uio = 0xFB                         # uio2 low from cycle 7
    m.run(20)
    m.ext_uio = 0xFF                         # high from cycle 27
    m.run(20)
    m.host_cr_ctrl(2)                        # DISARM
    m.run(6)
    assert m.cr.cap_done and m.cr.cap_w == 3
    e = _entries(m, 0x80, 3)
    assert [p for _, p in e] == [0x4, 0x0, 0x4] and e[1][0] == 7 and e[2][0] == 20
    m.host_run(0, False)
    m.run(2)
    m.host_rep_cfg(0 | (0x4 << 4))
    m.host_rep_buf(0x80 | (0 << 8))          # length 0: the capture's 3 entries
    m.host_cr_ctrl(4)
    m.run(80)
    assert m.cr.rep_done and m.cr.rep_k == 3 and m.cr.rep_n == 3 and not m.cr.rep_under
    ev = [e for e in m.pin_events if e[0] > 57]
    # entry 0 (released) changes nothing; entry 1 drives low, entry 2 releases
    assert [(e[2] & 0x4, e[1] & 0x4) for e in ev] == [(4, 0), (0, 0)], ev
    assert ev[1][0] - ev[0][0] == 20


def test_replay_waits_for_a_free_port_then_keeps_time():
    m, syms = boot("""
    loop: bra loop
    t1: bra t1
    """, t1=True)
    m.run(4)
    for i, (d, p) in enumerate([(0, 1), (2, 0), (2, 1)]):
        m.imem[0xA0 + i] = (d << 4) | p
    m.host_rep_cfg(0 | (0x1 << 4))
    m.host_rep_buf(0xA0 | (3 << 8))
    m.host_cr_ctrl(4)
    m.run(40)
    assert m.cr.rep_active and m.cr.rep_k == 0 and m.cr.rep_f == 0      # nothing fetched: no free cycle
    m.host_run(1, False)
    m.run(40)
    # the port frees, entry 0 applies, entries 1 and 2 follow 2 cycles apart
    assert m.cr.rep_done and not m.cr.rep_under and m.cr.rep_k == 3


def test_replay_underrun_with_back_to_back_entries():
    # Entries one cycle apart need one fetch per cycle; with one thread
    # stopped the port is free every other cycle, so the third entry is late:
    # underrun, replay stops, the late entry is not applied.
    m, _ = boot("""
    loop: bra loop
    """)
    m.run(4)
    for i, (d, p) in enumerate([(0, 1), (1, 0), (1, 1), (1, 0)]):
        m.imem[0xA0 + i] = (d << 4) | p
    m.host_rep_cfg(0 | (0x1 << 4))
    m.host_rep_buf(0xA0 | (4 << 8))
    m.host_cr_ctrl(4)
    m.run(30)
    assert m.cr.rep_under and m.cr.rep_done and not m.cr.rep_active and m.cr.rep_k < 4


def test_capc_instruction_and_status_bits():
    m, syms = boot("""
        ldi r0, 4
        capc r0             ; start a replay of the (empty) last capture: done at once
        rds r3
        ldi r0, 1
        capc r0             ; arm
        rds r1
        ldi r0, 2
        capc r0             ; disarm; the one queued entry drains at a free slot
        nop
        rds r2
        halt
    """)
    m.step(); m.step()
    _cap_setup(m, length=4)
    m.host_rep_buf(0x80 | (0 << 8))
    run_until_halt(m)
    t = m.threads[0]
    assert not (t.regs[3] & 0x100) and m.cr.rep_done and m.cr.rep_n == 0   # length 0: done at once
    assert t.regs[1] & 0x80 and not (t.regs[2] & 0x80)      # active after ARM, not after DISARM
    assert m.cr.cap_w == 1 and m.cr.cap_done
