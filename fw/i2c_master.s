; I2C master with clock stretching and a stretch timeout, one thread.
; Open-drain pins, external pull-ups.
;
; Host protocol on the inbox, a small bytecode:
;   0x01            START (also serves as a repeated START)
;   0x02            STOP
;   0x03 n b0..bn-1 WRITE n bytes; pushes one status byte: 0 = all ACKed,
;                   k = byte k (1-based) was NACKed (remaining bytes are drained)
;   0x04 n          READ n bytes (ACK all but the last); pushes the n bytes
;   0x05            START the other thread (used by fw/capture_demo.s)
; Any other byte is ignored.
;
; Clock-stretch timeout: if a slave holds SCL low for longer than
; I2C_TMO_TICKS * I2C_TMO_PERIOD cycles (25 ms at 60 MHz by default) the
; command is abandoned: both lines are released, the bytes of a WRITE still
; queued in the inbox are drained, and the status byte 0xFF is pushed. The
; host decides what to do next (typically STOP once the bus is free).
;
; Timing: every phase is a fresh SETT + WAITD 1 of I2C_Q cycles, so each phase
; lasts at least I2C_Q cycles no matter how long the thread was blocked on
; the host. One bit is 3 * I2C_Q + ~20 cycles. I2C_Q = 200 at 60 MHz gives
; roughly 90 kHz; 50 gives ~300 kHz.
;
; Registers: r0 data, r1 I2C_Q, r2 bit counter, r3 byte count, r4 status
; (non-zero only inside a WRITE, see i2c_timeout), r5 shift register,
; r6 saved LR, r7 timeout timer period.

.equ I2C_Q, 200
.equ I2C_TMO_PERIOD, 60000      ; cycles per timeout tick
.equ I2C_TMO_TICKS, 25          ; 25 ticks of 60000 cycles = 25 ms at 60 MHz
.equ SCL, uio2
.equ SDA, uio3

i2c_init:
        od    SCL               ; open-drain, released
        od    SDA
        ldw   r1, I2C_Q
        ldw   r7, I2C_TMO_PERIOD
i2c_cmd:
        ldi   r4, 0             ; not inside a WRITE
        pop   r0
        cmpi  r0, 1
        beq   i2c_start
        cmpi  r0, 2
        beq   i2c_stop
        cmpi  r0, 3
        beq   i2c_write
        cmpi  r0, 4
        beq   i2c_read
        cmpi  r0, 5
        bne   i2c_cmd
        start                   ; 0x05: wake the other thread (capture demo)
        bra   i2c_cmd

i2c_start:                      ; valid from idle and as a repeated START
        set   SDA
        sett  r1
        waitd 1
        set   SCL
        sett  r7                ; stretch timeout: coarse timer, I2C_TMO_TICKS ticks
        setd  I2C_TMO_TICKS
        wt1t  SCL               ; honour stretching, give up at the deadline
        bcs   i2c_timeout
        sett  r1
        waitd 1
        clr   SDA               ; SDA falls while SCL is high
        sett  r1
        waitd 1
        clr   SCL
        sett  r1
        waitd 1
        bra   i2c_cmd

i2c_stop:
        clr   SDA
        sett  r1
        waitd 1
        set   SCL
        sett  r7
        setd  I2C_TMO_TICKS
        wt1t  SCL
        bcs   i2c_timeout
        sett  r1
        waitd 1
        set   SDA               ; SDA rises while SCL is high
        sett  r1
        waitd 1
        bra   i2c_cmd

i2c_write:
        pop   r3                ; n
        ldi   r4, 0
i2c_wloop:
        cmpi  r3, 0
        beq   i2c_wdone
        inc   r4                ; r4 = index of the byte in flight (1-based)
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

i2c_timeout:                    ; SCL held low past the deadline: abandon the command
        set   SDA               ; release both lines
        set   SCL
        cmpi  r4, 0
        beq   i2c_tmo_push      ; not inside a WRITE: nothing queued to drain
i2c_tmo_drain:                  ; inside a WRITE: r3 - 1 bytes still to come
        dec   r3
        beq   i2c_tmo_push
        pop   r0
        bra   i2c_tmo_drain
i2c_tmo_push:
        ldi   r4, 0xFF
        push  r4                ; status 0xFF: bus timeout
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

; ---- bit level. SCL low on entry and exit. Leaf routines; a timeout jumps
;      straight to i2c_timeout, abandoning the call chain.
bit_out:                        ; C -> SDA, one clock pulse
        wrc   SDA               ; 1 = release, 0 = drive low
        sett  r1
        waitd 1                 ; setup
        set   SCL
        sett  r7
        setd  I2C_TMO_TICKS
        wt1t  SCL               ; clock stretching, with timeout
        bcs   i2c_timeout
        sett  r1
        waitd 1                 ; high
        clr   SCL
        sett  r1
        waitd 1                 ; low
        ret
bit_in:                         ; one clock pulse, C = SDA sampled while high
        set   SDA               ; release so the slave can drive
        sett  r1
        waitd 1
        set   SCL
        sett  r7
        setd  I2C_TMO_TICKS
        wt1t  SCL
        bcs   i2c_timeout
        sett  r1
        waitd 1
        rdc   SDA
        clr   SCL
        sett  r1
        waitd 1
        ret
