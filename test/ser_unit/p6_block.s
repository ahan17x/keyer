; ser_unit phase 6: blocking and timeout forms (SEMANTICS 7.4, 15.2).
; NRZI, no stuffing, no CRC, symbol period 20, pair 0: frame A5 5A 77.
        ldi    r1, 20
        sett   r1
        ldi    r0, 0x01         ; NRZI, receiver off, pair 0
        sercfg r0
        seri   0xA5             ; queued at once
        seri   0x5A             ; blocks until the first tick takes 0xA5
        setd   0
        ldi    r4, 0x77
        sertxt r4               ; holding register full, deadline reached: C = 1, nothing queued
        bcc    fail
        setd   3
        sertxt r4               ; full for 8 ticks more: times out after 3, C = 1
        bcc    fail
        setd   30
        sertxt r4               ; completes when 0x5A is taken: C = 0, 0x77 queued
        bcs    fail
        setd   1
        serwtt                  ; transmitter busy: times out, C = 1
        bcc    fail
        serwt                   ; blocks until idle and empty
        serst  r7               ; status 0
        cmpi   r6, 0            ; r6 = 0: Z = 1, C = 0
        setd   2
        serrxt r5               ; receiver off: times out, C = 1, r5 and Z unchanged
        bcc    fail
        bne    fail
        .word  0xE1F6           ; serializer function 6 with the T bit: no effect
        bcc    fail
        bne    fail
        clc
        .word  0xEFF4           ; SERST r7 with the T bit (ignored): C unchanged
        bcs    fail
        ldi    r5, 1
        cmpi   r5, 0            ; Z = 0
        serst  r6               ; flags unchanged
        beq    fail
        set    18
        halt
fail:   set    19
        halt
