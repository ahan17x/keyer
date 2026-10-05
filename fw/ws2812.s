; WS2812B LED strip driver: single wire, 800 kbit/s, pulse-width coded bits.
; One thread, one push-pull output, idle low. Host: load, run thread 0.
;
; Host protocol on the inbox, one frame:
;   [n_lo] [n_hi] [n bytes]     n = number of bytes in the frame, 1..65535
; The bytes go out in the order given, each MSB first, so for a WS2812B the
; host sends green, red, blue for every LED and n = 3 * LEDs (4 * LEDs for an
; RGBW part). n = 0 is ignored. Every frame with n > 0 answers with one status
; byte in the outbox, pushed when all n bytes have been taken from the inbox:
;   0x00  the whole frame went out
;   0xFF  underrun: the frame was cut short (see below)
; After the status the line is held low for WS_RESET cycles (the strip
; latches) before the next header is read; the same wait runs once at start.
; The host must read the status bytes: with 16 unread the thread waits.
;
; Why a count and not "the frame ends when the inbox runs dry": the inbox is
; 16 bytes deep, so the host refills it while the frame is going out (read
; LEVELS, write at most 16 minus the inbox level), and an inbox that runs dry
; in the middle of a frame would look exactly like the end of the frame. With
; the count the firmware can tell the two apart and report it. The count is
; in bytes and 16 bits wide so that strips longer than 255 LEDs and 4-byte
; pixels need no other firmware.
;
; Underrun. Every byte after the first is fetched in the low phase of the
; last bit of the byte before it, with a timed POP (POPT) whose deadline, the
; timer grid point of that bit's fall, has already passed: it takes the byte
; if one is there and otherwise returns at once with C = 1. Waiting for a late
; byte could not help: the only wait that keeps the bit timing ends with the
; low phase, a few cycles later, and one byte over the host SPI takes 64. So
; an empty inbox at that slot ends the frame on the wire: the line stays low,
; the strip latches the bytes sent so far (a short frame, never a stretched
; bit), the rest of the n bytes are taken from the host and dropped so the
; stream stays in step, and the status is 0xFF. The first byte of a frame is
; a plain POP: nothing is on the wire yet, so the host may take its time.
; To abandon a frame: stop the thread, FIFOCLR, PC0 = ws_init, run.
;
; Timing. The bit timer ticks once per slot (2 cycles) and is started once per
; frame; every edge is a pin write in the slot after a WAITD, so the high
; times and the bit period are exact multiples of 2 cycles and do not depend
; on the instructions in between, byte boundaries included. Constants are in
; core cycles, even (odd values round down), defaults for a 50 MHz clock:
;   WS_T0H    high time of a 0 bit    20 = 0.40 us  (datasheet 0.40 +/- 0.15)
;   WS_T1H    high time of a 1 bit    40 = 0.80 us  (0.80 +/- 0.15)
;   WS_BIT    bit period              62 = 1.24 us  (1.25 +/- 0.60); the low
;             times follow: 42 = 0.84 us after a 0, 22 = 0.44 us after a 1
;   WS_RESET  latch time            3000 = 60 us    (above 50 us; at most
;             65535. Later WS2812B revisions ask for 280 us: use 15000)
; For another clock divide the datasheet times by the clock period.
;
; Limits, in cycles: WS_T0H >= 6, WS_T1H - WS_T0H >= 4, WS_BIT - WS_T1H >= 16,
; and each of the three at most 510. The 16 is the byte boundary: six
; instructions (two DJNZ, POPT, BCS, SWAP, LDI) sit between the fall of a 1
; and the WAITD before the next rise. Below it the first rise of every byte
; is late. Fastest bit: 26 cycles (1.9 Mbit/s at 50 MHz). A WS2812B allows a
; 1 at most 0.60 us of low time, so the core clock must be 26.7 MHz or more.
;
; Registers: r0 shift register, r1 bit timer period (2), r2 bit counter,
; r3 bytes left in the frame, then the status, r7 WS_RESET.

.equ WS_T0H, 20
.equ WS_T1H, 40
.equ WS_BIT, 62
.equ WS_RESET, 3000
.equ DOUT, uo2

.equ WS_K0, WS_T0H / 2                  ; ticks: rise to the fall of a 0
.equ WS_K1, WS_T1H / 2 - WS_K0          ; fall of a 0 to the fall of a 1
.equ WS_K2, WS_BIT / 2 - WS_T1H / 2     ; fall of a 1 to the next rise

ws_init:
        clr   DOUT              ; idle low (also when restarted in mid-frame)
        ldw   r7, WS_RESET
        ldi   r1, 2             ; bit timer: one tick per slot
ws_gap:
        sett  r7                ; one tick of WS_RESET cycles with the line low
        waitd 1
ws_frame:
        pop   r3                ; n, low byte
        pop   r0                ; n, high byte
        swap  r0
        or    r3, r0
        beq   ws_frame          ; n = 0: nothing to send
        pop   r0                ; first byte: wait for it as long as it takes
        sett  r1                ; start the bit grid
        bra   ws_load
ws_byte:
        popt  r0                ; deadline already reached: pop, or C = 1 at once
        bcs   ws_underrun
ws_load:
        swap  r0                ; byte into bits 15:8 so SHL delivers the MSB first
        ldi   r2, 8
ws_bit:
        waitd WS_K2             ; end of the low phase. timing: the POPT at ws_byte cannot block, its deadline has passed
        set   DOUT              ; rise
        shl   r0                ; C = this bit; WAITD and DJNZ leave it alone
        waitd WS_K0
        wrc   DOUT              ; a 0 falls here, a 1 stays high
        waitd WS_K1
        clr   DOUT              ; a 1 falls here
        djnz  r2, ws_bit
        djnz  r3, ws_byte       ; r3 = 0 when the frame is complete
ws_end:
        push  r3                ; status
        bra   ws_gap
ws_underrun:
        pop   r0                ; drop the r3 bytes the host still sends
        djnz  r3, ws_underrun
        ldi   r3, 0xFF
        bra   ws_end
