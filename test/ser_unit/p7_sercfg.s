; ser_unit phase 7: SERCFG in the middle of a frame, in a symbol tick
; (period 2 from a SETT at an even slot: every thread-0 slot is a tick).
; It resets the engine and its pin write of that cycle is not made.
        ldi    r1, 2
        sett   r1
        ldi    r0, 0x01         ; NRZI, no stuffing, receiver off, pair 0
        sercfg r0
        seri   0x00             ; every bit a transition
        seri   0x00
        nop
        nop
        nop
        sercfg r0               ; mid-frame
        delay  20
        set    18
        halt
