; ser_unit phase 5: Manchester receive with CRC-32, pair 0, half-bit period
; 4, owned by thread 1 (thread 0 runs a period-3 timer, so the receiver's T
; must come from the owner), using the timeout form SERRXT with a far
; deadline (C = 0 on success). Results go to outbox 1.
        ldi    r1, 3
        sett   r1
        halt

        .org   0x40
        ldi    r1, 4
        sett   r1
        ldi    r0, 0x1A         ; Manchester, CRC-32, receiver on, pair 0
        sercfg r0
fh:     setd   255
        serrxt r2
        bcs    fail
        push   r2
        bne    fh
        swap   r2
        push   r2
        set    18
        halt
fail:   set    19
        halt
