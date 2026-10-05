; ser_unit phase 1: NRZI transmit with stuffing and CRC-16 (SEMANTICS 15.3).
; SYNC 0x80 outside the CRC, "123456789" inside it (CRC-16/USB check value
; 0xB4C8, sent bit 0 first). Symbol period 4 cycles, pair 0 (uio0, uio1).
        ldi    r1, 4
        sett   r1
        ldi    r0, 0x05         ; NRZI, stuffing, CRC-16, receiver off, pair 0
        sercfg r0
        seri   0x80
        seric  0x31
        seric  0x32
        seric  0x33
        seric  0x34
        seric  0x35
        seric  0x36
        seric  0x37
        seric  0x38
        seric  0x39
        serwt                   ; until the release
        set    18               ; marker: uo[2]
        serst  r3               ; status 0
        halt
