; ser_unit phase 11: rx_drop through the core (SEMANTICS 15.2, 15.5, 15.7):
; NRZI receive, no stuffing, pair 0, period 8. The bench sends frames A and
; B with nobody reading (B's start drops A's byte and frame end), raises
; ui[4]; the firmware reads the status twice (SERST clears rx_drop, the
; first read returns it set). The bench lowers ui[4], sends C and D
; (D's start drops C's), raises ui[4]; SERRX returns D's byte (rx_drop
; kept), SERRX returns the status at the frame end (bit 10 set, cleared),
; SERST shows it clear. Every word goes to outbox 0, low byte first.
        ldi    r1, 8
        sett   r1
        ldi    r0, 0x11         ; NRZI, receiver on, pair 0
        sercfg r0
w1:     bp0    12, w1
        serst  r3
        push   r3
        swap   r3
        push   r3
        serst  r3
        push   r3
        swap   r3
        push   r3
w2:     bp1    12, w2
w3:     bp0    12, w3
        serrx  r2               ; D's byte
        beq    fail
        push   r2
        serrx  r2               ; D's frame end: the status
        bne    fail
        push   r2
        swap   r2
        push   r2
        serst  r3
        push   r3
        swap   r3
        push   r3
        set    18
        halt
fail:   set    19
        halt
