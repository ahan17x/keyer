; JTAG (IEEE 1149.1) master, one thread: TAP reset, DR scans and IR scans of
; 1 to 256 bits. Reading IDCODE is a reset followed by a 32-bit DR scan.
;
; Pins: TCK uo3, TMS uo4, TDI uo5 (outputs); TDO ui4 (input, pulled up).
;
; Host protocol on the inbox, a small bytecode. Every command starts and
; ends with the TAP in Run-Test/Idle and TCK low; the first command after
; power-up must be RESET, because the state of the TAP is not known.
;   0x01             RESET: five TCK cycles with TMS high (Test-Logic-Reset
;                    from any state), then one with TMS low (Run-Test/Idle).
;                    Pushes nothing.
;   0x02 n b0 b1 ..  DR scan of n bits (n = 1..255, 0 means 256).
;   0x03 n b0 b1 ..  IR scan of n bits.
;                    ceil(n/8) data bytes follow; they go out on TDI, LSB of
;                    b0 first (the unused high bits of the last byte are
;                    ignored). The last bit is shifted with TMS high, then
;                    Update and back to Run-Test/Idle. The n bits captured
;                    on TDO are pushed as ceil(n/8) bytes, first bit in the
;                    LSB of the first byte; a last, partial byte is
;                    right-aligned with zeros above.
;   Any other byte is ignored.
; IDCODE (selected by the reset):  01  02 20 00 00 00 00  pushes the four
; bytes of the 32-bit code, little-endian. A missing target (TDO pulled up)
; reads FF FF FF FF, which is not a valid IDCODE.
;
; The host may be slow: if the inbox is empty when a scan needs its next
; data byte, or the outbox full when a byte is ready, the thread waits with
; TCK low and TMS/TDI stable and then carries on in step with the timer.
; A scan of more than 128 bits returns more than the outbox holds (16), so
; the host must read while the scan runs. Nothing a target does can hold
; the thread, so there is no timeout: only the host can make it wait.
;
; Timing. JTAG_HALF is half a TCK period in core cycles. The timer is set
; once and never restarted, so every TCK edge of every command is on one
; grid of JTAG_HALF cycles (use an even value: a thread runs every second
; cycle, and with an odd value the edges alternate one cycle early and late).
;   - TCK rises on the first tick after the work of the low half is done
;     (SETD 0 + WAITD 1) and falls exactly one tick later: TCK high is
;     always JTAG_HALF cycles, TCK low a whole number of JTAG_HALF.
;   - TMS and TDI change only while TCK is low: in a scan TDI 2 cycles and
;     TMS 6 cycles after the falling edge, in the TMS-only clocks TMS 2
;     cycles after it. Setup to the rising edge is JTAG_HALF - 6 cycles or
;     more, hold after it JTAG_HALF + 2 or more. The first TMS change of a
;     command is made from idle and is followed by at least one full
;     JTAG_HALF before TCK rises (SETD 1).
;   - TDO is sampled by the RDC in the slot after SET TCK. Because of the
;     two-cycle synchroniser that is the level of the TDO pad one core cycle
;     BEFORE TCK rises at the pad. A TAP changes TDO only on the falling
;     edge, so the bit has been stable since then and stays until the next
;     falling edge: the sample is safe as long as the target's delay from
;     falling TCK to valid TDO, plus the cable both ways, is at most
;     JTAG_HALF - 1 cycles.
; Choosing JTAG_HALF: core clock / (2 * TCK rate); the default 50 gives
; 500 kHz at 50 MHz. From 24 up TCK is a square wave within a command for
; as long as the host keeps up. The fastest supported value is 18 (TCK =
; clk / 36, 1.39 MHz at 50 MHz): the low half of the shift loop is nine
; slots, 18 cycles, from WAITD to WAITD. From 18 to 23 the low half takes
; two ticks at each byte boundary (a byte pushed, or r0 used up: that path
; is longer than one tick). Smaller values break nothing but gain nothing:
; from 12 to 17 every low half of the shift loop takes two ticks, and below
; 12 the loop no longer follows the timer (its high half is six slots, 12
; cycles, whatever the value, and the edges leave the grid).
;
; Registers: r0 TDI shift register, r1 clock count of jtag_tms (and the
; command byte, the timer period at init), r2 TDI bits left in r0, r3 clocks
; left in the shift loop, r4 TDO shift register, r5 TMS bits for jtag_tms,
; r7 TDO samples missing from the current byte. r6 is free.

.equ JTAG_HALF, 50
.equ TCK, uo3
.equ TMS, uo4
.equ TDI, uo5
.equ TDO, ui4

jtag_init:
        ldw   r1, JTAG_HALF
        sett  r1                ; the one timer grid; outputs are low after reset
jtag_cmd:
        pop   r1                ; command; for a scan also the number of clocks
        cmpi  r1, 1             ; from Run-Test/Idle to Capture (DR 2, IR 3)
        beq   jtag_reset
        bcs   jtag_cmd          ; 0x00: ignored
        cmpi  r1, 4
        bcc   jtag_cmd          ; 0x04 and above: ignored
        mov   r5, r1
        andi  r5, 1             ; TMS after the first clock: DR 0, IR 1 then 0
        pop   r3                ; n
        cmpi  r3, 0
        bne   jtag_n
        ldih  r3, 1             ; 0 means 256
jtag_n: inc   r3                ; the shift loop runs n + 1 clocks
        pop   r0                ; first TDI byte
        ldi   r2, 8             ; TDI bits left in r0
        ldi   r7, 9             ; the first TDO sample (taken in Capture) is not data
        set   TMS               ; Run-Test/Idle -> Select-DR-Scan
        setd  1                 ; at least one full tick of setup before TCK rises
        call  jtag_tms1         ; to Capture-DR / Capture-IR, leaves TMS low
        ldi   r1, 2             ; after the shift: Update, then Run-Test/Idle
        call  jtag_shift        ; returns here through the RET of jtag_tms
        cmpi  r7, 8
        beq   jtag_cmd          ; n was a multiple of 8: every byte is out
jtag_pad:
        shr   r4                ; right-align the last, partial byte
        djnz  r7, jtag_pad
        swap  r4
        push  r4
        bra   jtag_cmd

jtag_reset:
        set   TMS
        setd  1
        ldi   r5, 0x0F          ; four more clocks with TMS high, then low
        ldi   r1, 6
        call  jtag_tms1
        bra   jtag_cmd

; ---- shift loop: r3 = n + 1 clocks. The first takes the TAP from Capture
;      to Shift; each one writes the TDI bit for the next and samples the
;      TDO bit of the one before; the last two write TMS high (the last
;      data bit, then Exit1 -> Update). Entered at jtag_shift with TCK low.
jtag_low:
        waitd 1                 ; rest of the high half
        clr   TCK               ; falling edge
        wrc   TDI
        cmpi  r3, 3             ; C = 1 in the last two clocks
        wrc   TMS
        djnz  r7, jtag_nopush
        swap  r4                ; eight samples: a byte of TDO is complete
        push  r4
        ldi   r7, 8
jtag_nopush:
        djnz  r2, jtag_nopop
        ldi   r2, 8             ; r0 is used up
        bcs   jtag_nopop        ; (C still from the CMPI) no data bits left
        pop   r0
jtag_nopop:
        djnz  r3, jtag_shift
; ---- TMS-only clocks: r1 clocks; the TMS level of the first is already on
;      the pin, r5 holds the levels of the following ones, LSB first (then
;      zeros, so TMS is left low). The shift loop falls in here for
;      Exit1 -> Update -> Run-Test/Idle.
jtag_tms:
        setd  0                 ; the next tick, wherever the timer is now
jtag_tms1:
        waitd 1                 ; rest of the low half
        set   TCK               ; rising edge
        waitd 1
        clr   TCK               ; falling edge
        outr  TMS, r5
        shr   r5
        djnz  r1, jtag_tms
        ret

jtag_shift:
        setd  0                 ; the next tick, however long the low half took
        waitd 1
        set   TCK               ; rising edge
        rdc   TDO               ; the TDO pad one cycle before the edge
        rcr   r4                ; collect, LSB first
        shr   r0                ; C = next TDI bit
        bra   jtag_low
