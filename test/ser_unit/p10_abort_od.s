; ser_unit phase 10: aborts with N (uio1) of pair 0 open-drain (host
; PINMODE 0x02). idle() respects od_mask' like every engine write (15.2):
; the NRZI abort releases P and leaves J's P = 0 in its output register,
; the Manchester abort drives P low; N is never written (uio_out 0,
; uio_oe 0).
        ldi    r1, 2
        sett   r1
        ldi    r0, 0x01         ; NRZI, pair 0
        sercfg r0
        seri   0x00
        seri   0x00
        nop
        nop
        nop
        ldi    r2, 0x02         ; Manchester, pair 0
        sercfg r2               ; abort NRZI
        seri   0xFF
        seri   0xFF
        nop
        nop
        nop
        sercfg r2               ; abort Manchester
        delay  10
        set    18
        halt
