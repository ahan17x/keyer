; ser_unit phase 8: N (uio1) of pair 0 is open-drain (host PINMODE 0x02):
; the engine writes only P; N keeps uio_out = 0 and uio_oe = 0 (15.1).
        ldi    r1, 4
        sett   r1
        ldi    r0, 0x01         ; NRZI, receiver off, pair 0
        sercfg r0
        seri   0x00
        serwt
        set    18
        halt
