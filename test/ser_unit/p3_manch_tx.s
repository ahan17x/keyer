; ser_unit phase 3: Manchester transmit with CRC-32 (check value 0xCBF43926
; for "123456789"), pair 1 (uio2, uio3), half-bit period 2 cycles.
        ldi    r1, 2
        sett   r1
        ldi    r0, 0x4A         ; Manchester, no stuffing, CRC-32, receiver off, pair 1
        sercfg r0
        seri   0x55
        seri   0x55
        seri   0xD5
        seric  0x31
        seric  0x32
        seric  0x33
        seric  0x34
        seric  0x35
        seric  0x36
        seric  0x37
        seric  0x38
        seric  0x39
        serwt
        set    18
        halt
