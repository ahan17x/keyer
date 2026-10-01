"""pytest for the Loom ISS and assembler. Run: python3 -m pytest tools/ -q"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loom_isa as isa  # noqa: E402
import loomasm  # noqa: E402
from loomsim import Machine  # noqa: E402


def asm(src):
    words, syms, _ = loomasm.assemble(src)
    return loomasm.to_list(words), syms


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
    with pytest.raises(loomasm.AsmError):
        asm("ldi r0, 300")
    with pytest.raises(loomasm.AsmError):
        asm("set 24")
    with pytest.raises(loomasm.AsmError):
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


def test_timer_tick_timing():
    # SETT executes at cycle 2 (second instruction). Period 10 -> ticks at the
    # ends of cycles 12, 22, 32, ... WAITT sees the tick from cycle 13 on, so
    # the first WAITT completes at slot 14 (first even cycle >= 13).
    m, _ = boot("""
        ldi r0, 10
        sett r0
        waitt
        waitt
        halt
    """, trace=True)
    run_until_halt(m)
    done = [r for r in m.trace if r.done and isa.disasm(r.word) == "WAITT"]
    assert [r.cycle for r in done] == [14, 24]


def test_timer_sticky_tick_no_drift():
    # A loop that is sometimes slower than the period: edges still land on ticks.
    m, _ = boot("""
        ldi r0, 8
        sett r0
    loop:
        waitt
        set uo2
        waitt
        clr uo2
        delay 6        ; 7 slots = 14 cycles > period: next tick already pending
        bra loop
    """)
    m.run(400)
    ev = [e for e in m.pin_events if e[0] > 10]
    assert len(ev) >= 10
    # SETT executes at cycle 2 -> ticks at the ends of cycles 10, 18, 26, ...
    # An on-time WAITT completes at the first even cycle >= tick+1, the pin
    # instruction runs one slot later and the pad changes the cycle after that,
    # so an on-grid event cycle e has w = e - 3 even with (w - 11) % 8 in {0, 1}.
    # The first WAITT of each iteration is late (the DELAY is longer than the
    # period) and completes immediately on the sticky tick; the second WAITT,
    # one slot later, must be back on the tick grid: no accumulated drift.
    on_grid = [(e[0] - 3) % 2 == 0 and ((e[0] - 3) - 11) % 8 in (0, 1) for e in ev]
    assert all(on_grid[1::2]), ev          # every CLR edge is on the grid
    assert not all(on_grid[0::2])          # some SET edges were late (sticky tick)


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
