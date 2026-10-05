; ser_unit phase 4: NRZI receive, stuffing, receiver on, PID out of the CRC,
; pair 0, symbol period 8. Every SERRX result goes to outbox 0 (the bench
; taps the push port); a frame end pushes the status low then high byte.
; Frames: E (DATA0, CRC-16 good), F (token, CRC-5 good), G (overrun,
; stuffing error, frame not on a byte boundary; read only after it ended).
        ldi    r1, 8
        sett   r1
        ldi    r0, 0x35         ; NRZI, stuffing, CRC-16, receiver on, skip PID, pair 0
        sercfg r0
fe:     serrx  r2
        push   r2
        bne    fe               ; Z = 0: a byte
        swap   r2
        push   r2
ff:     serrx  r2
        push   r2
        bne    ff
        swap   r2
        push   r2
wg:     serst  r3
        andi   r3, 0x10         ; frame ended
        beq    wg
        serrx  r2               ; the byte held since the PID: Z = 0
        beq    fail
        push   r2
        serrx  r2               ; then the frame end: Z = 1
        bne    fail
        push   r2
        swap   r2
        push   r2
        set    18
        halt
fail:   set    19
        halt
