; SPI master, mode 0 (CPOL=0, CPHA=0), MSB first, one thread.
; Host protocol on the inbox: [N] [N bytes]  -> CS_n low, N transfers, CS_n high.
; Every received byte is pushed to the outbox.
;
; SPI_HALF = half an SCK period in core cycles. Minimum 10 (the low half of
; the loop is 5 slots), so SCK <= clk / 20.

.equ SPI_HALF, 15
.equ SCK,  uo3
.equ MOSI, uo4
.equ CSN,  uo5
.equ MISO, ui4

spi_init:
        ldw   r1, SPI_HALF
        sett  r1
        set   CSN
        clr   SCK
        clr   MOSI
spi_frame:
        pop   r3                ; N
        cmpi  r3, 0
        beq   spi_frame
        clr   CSN
        sett  r1                ; fresh timer phase for the frame
spi_byte:
        pop   r0
        swap  r0                ; byte into bits 15:8 so RCL delivers the MSB first
        ldi   r2, 8
        ldi   r4, 0
        sett  r1                ; restart the half-period timer so the first
spi_bit:                        ; high phase is a full half period
        rcl   r0                ; C = next bit out
        wrc   MOSI
        waitd 1                   ; half period with SCK low
        set   SCK
        rdc   MISO              ; sample on the rising edge
        rcl   r4                ; collect, MSB first
        waitd 1                   ; half period with SCK high
        clr   SCK
        djnz  r2, spi_bit
        push  r4
        dec   r3
        bne   spi_byte
        waitd 1
        set   CSN
        bra   spi_frame
