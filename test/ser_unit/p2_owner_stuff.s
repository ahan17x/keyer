; ser_unit phase 2: thread 1 owns the engine (its period 5 is the symbol
; period, thread 0 runs a period-7 timer), pair 2 (uio4, uio5). Two NRZI
; frames without CRC: 80 FF 7E (stuffed zeros inside the frame) and 80 FC
; (six ones end the data: a stuffed zero precedes the tail).
        ldi    r1, 7
        sett   r1
        halt

        .org   0x40
        ldi    r1, 5
        sett   r1
        ldi    r0, 0x85         ; NRZI, stuffing, CRC-16, receiver off, pair 2
        sercfg r0
        seri   0x80
        seri   0xFF
        seri   0x7E
        serwt
        seri   0x80
        seri   0xFC
        serwt
        set    18
        halt
