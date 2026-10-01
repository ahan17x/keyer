; I2C master with clock stretching, one thread. Open-drain pins, external pull-ups.
;
; Host protocol on the inbox, a small bytecode:
;   0x01            START (also serves as a repeated START)
;   0x02            STOP
;   0x03 n b0..bn-1 WRITE n bytes; pushes one status byte: 0 = all ACKed,
;                   k = byte k (1-based) was NACKed (remaining bytes are drained)
;   0x04 n          READ n bytes (ACK all but the last); pushes the n bytes
; Any other byte is ignored.
;
; Timing: every phase is a fresh SETT + WAITT of I2C_Q cycles, so each phase
; lasts at least I2C_Q cycles no matter how long the thread was blocked on
; the host. One bit is 3 * I2C_Q + ~14 cycles. I2C_Q = 200 at 60 MHz gives
; roughly 96 kHz; 50 gives ~330 kHz.
;
; Registers: r0 data, r1 I2C_Q, r2 bit counter, r3 byte count, r4 status,
; r5 shift register, r6 saved LR.

.equ I2C_Q, 200
.equ SCL, uio2
.equ SDA, uio3

i2c_init:
        od    SCL               ; open-drain, released
        od    SDA
        ldw   r1, I2C_Q
i2c_cmd:
        pop   r0
        cmpi  r0, 1
        beq   i2c_start
        cmpi  r0, 2
        beq   i2c_stop
        cmpi  r0, 3
        beq   i2c_write
        cmpi  r0, 4
        beq   i2c_read
        bra   i2c_cmd

i2c_start:                      ; valid from idle and as a repeated START
        set   SDA
        sett  r1
        waitt
        set   SCL
        wt1   SCL               ; honour stretching
        sett  r1
        waitt
        clr   SDA               ; SDA falls while SCL is high
        sett  r1
        waitt
        clr   SCL
        sett  r1
        waitt
        bra   i2c_cmd

i2c_stop:
        clr   SDA
        sett  r1
        waitt
        set   SCL
        wt1   SCL
        sett  r1
        waitt
        set   SDA               ; SDA rises while SCL is high
        sett  r1
        waitt
        bra   i2c_cmd

i2c_write:
        pop   r3                ; n
        ldi   r4, 0
i2c_wloop:
        cmpi  r3, 0
        beq   i2c_wdone
        inc   r4
        pop   r0
        call  tx_byte           ; C = 1 if NACK
        bcs   i2c_wnack
        dec   r3
        bra   i2c_wloop
i2c_wnack:
        dec   r3                ; drain what the host already queued
        beq   i2c_wdrained
        pop   r0
        bra   i2c_wnack
i2c_wdrained:
        push  r4
        bra   i2c_cmd
i2c_wdone:
        ldi   r4, 0
        push  r4                ; status 0: everything ACKed
        bra   i2c_cmd

i2c_read:
        pop   r3                ; n
i2c_rloop:
        cmpi  r3, 0
        beq   i2c_cmd
        call  rx_byte           ; r0 = byte
        push  r0
        dec   r3
        beq   i2c_rlast
        clc                     ; ACK
        call  bit_out
        bra   i2c_rloop
i2c_rlast:
        setc                    ; NACK the last byte
        call  bit_out
        bra   i2c_cmd

; ---- byte level. tx_byte: r0 -> bus, returns C = ACK bit (1 = NACK).
;      rx_byte: bus -> r0. Both save LR in r6 because they call bit routines.
tx_byte:
        rdlr  r6
        mov   r5, r0
        swap  r5                ; MSB first via RCL
        ldi   r2, 8
tx_bit:
        rcl   r5
        call  bit_out
        djnz  r2, tx_bit
        call  bit_in            ; ACK bit -> C
        jmpr  r6

rx_byte:
        rdlr  r6
        ldi   r2, 8
        ldi   r0, 0
rx_bit:
        call  bit_in
        rcl   r0
        djnz  r2, rx_bit
        jmpr  r6

; ---- bit level. SCL low on entry and exit. Leaf routines.
bit_out:                        ; C -> SDA, one clock pulse
        wrc   SDA               ; 1 = release, 0 = drive low
        sett  r1
        waitt                   ; setup
        set   SCL
        wt1   SCL               ; clock stretching
        sett  r1
        waitt                   ; high
        clr   SCL
        sett  r1
        waitt                   ; low
        ret
bit_in:                         ; one clock pulse, C = SDA sampled while high
        set   SDA               ; release so the slave can drive
        sett  r1
        waitt
        set   SCL
        wt1   SCL
        sett  r1
        waitt
        rdc   SDA
        clr   SCL
        sett  r1
        waitt
        ret
