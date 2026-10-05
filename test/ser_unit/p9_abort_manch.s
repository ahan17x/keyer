; ser_unit phase 9: SERCFG aborting a Manchester frame (DECISIONS D-039).
; Period 2 from a SETT at an even slot: every thread-0 slot is a tick.
; The abort writes idle(2, P, N) of the old configuration (pair 1): both
; pins driven low (15.1, 15.2). The second SERCFG finds the transmitter
; IDLE and writes no pin, although it selects NRZI on the same pair.
        ldi    r1, 2
        sett   r1
        ldi    r0, 0x42         ; Manchester, receiver off, pair 1
        sercfg r0
        seri   0xFF             ; every bit J K
        seri   0xFF
        nop
        nop
        nop
        sercfg r0               ; mid-frame: abort
        delay  10
        ldi    r2, 0x41         ; NRZI, pair 1
        sercfg r2               ; transmitter IDLE: no pin write
        delay  10
        set    18
        halt
