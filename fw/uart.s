; Full-duplex UART, 8N1.
;   Thread 0: transmits bytes from its inbox on TX.
;   Thread 1: receives bytes on RX into its outbox.
; Host: load, set PC1 = rx_init, run both threads.
;
; BAUD_DIV = core clock / baud, e.g. 521 for 115200 at 60 MHz (minimum 12).
; RX samples each bit 1.5 periods after the start edge, then every period.
;
; Note on pins: push-pull outputs drive 0 after reset, so an active-low
; signal (a chip select, for instance) should live on a uio pin with an
; external pull-up, which is tri-stated at reset. TX idles high only once
; thread 0 has started.

.equ BAUD_DIV, 521
.equ TX, uo2
.equ RX, ui3

; ---------------------------------------------------------------- thread 0
tx_init:
        ldw   r1, BAUD_DIV
        set   TX                ; idle high
tx_loop:
        pop   r0                ; next byte from the host
        ldih  r0, 0xFF          ; ones above the data: stop bit and idle
        shl   r0                ; bit 0 = start bit (0), bits 8:1 = data, bit 9 = stop
        ldi   r2, 10            ; start + 8 data + stop
        sett  r1                ; restart the bit timer
        waitt                   ; align with the tick grid
        rcr   r0                ; first bit (the start bit) into C
tx_bit:
        wrc   TX                ; drive it: always two slots after a tick
        rcr   r0                ; prepare the next bit (LSB first); DJNZ keeps C
        waitt
        djnz  r2, tx_bit
        bra   tx_loop

; ---------------------------------------------------------------- thread 1
rx_init:
        ldw   r1, BAUD_DIV
        ldw   r3, BAUD_DIV + BAUD_DIV / 2 - 8   ; -8 compensates edge-detect, SETT and slot skew
rx_loop:
        wtf   RX                ; start bit edge
        sett  r3                ; 1.5 periods to the middle of bit 0
        ldi   r2, 8
        ldi   r0, 0
        waitt
        sett  r1                ; one period per bit from here
rx_bit:
        rdc   RX                ; sample
        rcr   r0                ; shift in at the top; after 8 bits the byte is in r0[15:8]
        waitt
        djnz  r2, rx_bit
        swap  r0
        rdc   RX                ; stop bit should be 1
        bcc   rx_frame_error
        push  r0
        bra   rx_loop
rx_frame_error:
        ori   r0, 0             ; (placeholder: a framing-error counter could go here)
        push  r0                ; still deliver the byte
        bra   rx_loop
