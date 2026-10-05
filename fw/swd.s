; ARM Serial Wire Debug host (ADIv5 SW-DP), one thread.
; Connects to a target (line reset, JTAG-to-SWD select, line reset, idle) and
; runs any DP or AP read or write transaction: request, turnaround, ACK, data
; with parity, turnaround. The data phase is skipped when the ACK is not OK.
;
; Pins: SWCLK = uo3 (pin 19), output. SWDIO = uio0 (pin 0), push-pull,
; driven with OEN and released with OEF for the turnarounds; it needs an
; external pull-up (so a silent target reads as ACK = 111).
;
; Host protocol on the inbox, a small bytecode:
;   0x01            CONNECT: 64 clocks with SWDIO high (line reset), 0xE79E
;                   LSB first, 64 clocks high, 8 clocks low. Pushes nothing.
;   0x02 req [d0 d1 d2 d3]
;                   TRANSFER. req is the 8-bit SWD request as it goes on the
;                   wire, LSB first: bit 0 start (1), bit 1 APnDP, bit 2 RnW,
;                   bits 3,4 A[2],A[3], bit 5 parity of bits 1..4, bit 6 stop
;                   (0), bit 7 park (1). 0xA5 reads DPIDR. If RnW = 0 (a
;                   write) four data bytes follow, little-endian; the firmware
;                   pops them before the first clock and computes the parity.
;                   Outbox: one byte ACK (1 OK, 2 WAIT, 4 FAULT, 7 no
;                   response; ACK[0] in bit 0). After a read with ACK = OK,
;                   five more bytes: the data d0 d1 d2 d3 little-endian, then
;                   the parity check, 0 = good, 1 = parity error.
;   0x03            IDLE: 8 clocks with SWDIO low (flushes a write through
;                   the target, or pads between transactions).
; Any other byte is ignored. The request byte is not checked: a request with
; bad parity or framing is sent as given, and the target stays silent (ACK 7).
; After an ACK of 7 send IDLE (or CONNECT) before the next TRANSFER: the
; released line read as ones for five clocks, and a target finds the next
; start bit only after it has seen SWDIO low.
;
; Wire sequence of a transfer, one loop pass (swd_bits) per SWCLK cycle:
; 8 request bits driven; SWDIO released; 1 turnaround cycle and 3 ACK cycles;
; then read: 32 data cycles, parity, 1 turnaround cycle, SWDIO driven low;
; or write: 1 turnaround cycle, SWDIO driven, 32 data cycles, parity; or, ACK
; not OK: 1 turnaround cycle, SWDIO driven low. 46 clocks for a full transfer.
;
; Timing. SWD_HALF = half an SWCLK period in core cycles (an even value,
; at least 12; 26 at 50 MHz gives 962 kHz inside a run of bits). The timer
; is started once and never restarted, and every SWCLK edge is made one slot
; after a timer tick, so all edges of all commands lie on one grid of
; SWD_HALF cycles. swd_bits begins with SETD 0 directly before its first
; WAITD 1: whenever the caller arrives, the next edge is on the next tick,
; so a low or high phase is never shorter than SWD_HALF. Every low phase is
; exactly SWD_HALF; inside one swd_bits call so is every high phase;
; between two calls (after the request, the ACK, each 16 data bits) SWCLK
; stays high for a whole number of half periods, two or more, while the
; thread prepares the next bits; between commands it rests high.
; The target samples SWDIO on the rising edge and drives its own bits from
; the rising edge. The host
;   - changes SWDIO 4 cycles after the falling edge (CLR SWCLK, SHR, WRC):
;     setup to the rising edge is SWD_HALF - 4 cycles, hold after it is
;     SWD_HALF + 4 cycles;
;   - samples with RDC in the slot right after SET SWCLK. Pin reads lag the
;     pad by the two-cycle synchroniser, so that RDC sees the pad in the
;     cycle before SWCLK rises on the pad: the bit the target has driven
;     since the previous rising edge, 2 * SWD_HALF - 1 cycles earlier or
;     more. The target's output delay may be anything shorter than that;
;   - releases SWDIO (OEF) after the rising edge that clocks the park bit
;     and before the falling edge of the turnaround cycle; the target starts
;     driving at the turnaround's rising edge. It drives again (OEN) only
;     after the rising edge of the turnaround cycle that follows the
;     target's last bit, a full clock after the target let go; after the
;     last clock of a transfer it drives SWDIO low about 14 cycles after
;     the rising edge.
; What limits the rate: the bit loop's high half is five slots (SET, RDC,
; RCR, XOR, DJNZ) before the next WAITD, 10 cycles; below SWD_HALF = 12 a
; WAITD inside the loop is late and phases become shorter than SWD_HALF.
;
; Registers: r0 bits to send (LSB first), r1 data high word, r2 bit count,
; r3 ACK (and scratch for the operands), r4 bits received (newest in bit
; 15), r5 running parity in bit 15, r6 timer period, then the request's RnW
; bit, r7 data low word (pass counter in CONNECT).

.equ SWD_HALF, 26
.equ SWCLK, uo3
.equ SWDIO, uio0

swd_init:
        ldw   r6, SWD_HALF
        sett  r6                ; the one tick grid for every clock edge
        oen   SWDIO             ; drive SWDIO, low
        waitd 1                 ; (a half period of setup for that first level)
        set   SWCLK             ; SWCLK rests high
swd_cmd:
        pop   r0
        cmpi  r0, 1
        beq   swd_connect
        cmpi  r0, 2
        beq   swd_xfer
        cmpi  r0, 3
        bne   swd_cmd
swd_idle:                       ; 0x03 IDLE
        ldi   r0, 0
        ldi   r2, 8
        call  swd_bits
        bra   swd_cmd

swd_connect:
        ldi   r7, 2
swd_reset:
        ldi   r3, 4             ; line reset: 4 * 16 clocks with SWDIO high
swd_ones:
        ldw   r0, 0xFFFF
        ldi   r2, 16
        call  swd_bits
        djnz  r3, swd_ones
        dec   r7
        beq   swd_idle          ; after the second reset: idle cycles, done
        ldw   r0, 0xE79E        ; JTAG-to-SWD select, LSB first
        ldi   r2, 16
        call  swd_bits
        bra   swd_reset

swd_xfer:
        pop   r0                ; the request byte
        mov   r6, r0
        andi  r6, 4             ; r6 = 4 for a read, 0 for a write
        bne   swd_req
        pop   r7                ; a write: the data, before any clock
        pop   r3
        swap  r3
        or    r7, r3
        pop   r1
        pop   r3
        swap  r3
        or    r1, r3
swd_req:
        ldi   r2, 8
        call  swd_bits          ; request, SWDIO driven
        oef   SWDIO             ; turnaround: release after the park bit's edge
        ldi   r2, 4
        call  swd_bits          ; turnaround cycle, then ACK[0..2]
        swap  r4
        andi  r4, 0xE0          ; ACK in bits 7:5
        mov   r3, r4
        cmpi  r3, 0x20
        bne   swd_trn           ; WAIT, FAULT or silence: no data phase
        ldi   r5, 0             ; parity accumulates in r5[15]
        cmpi  r6, 4
        beq   swd_read
        ldi   r2, 1             ; write: turnaround cycle, then drive
        call  swd_bits
        oen   SWDIO
        ldi   r5, 0             ; the parity starts after the turnaround sample
        mov   r0, r7
        ldi   r2, 16
        call  swd_bits
        mov   r0, r1
        ldi   r2, 16
        call  swd_bits
        shl   r5                ; C = parity of the 32 bits as read back
        ldi   r0, 0
        rcl   r0
        ldi   r2, 1
        call  swd_bits          ; parity bit
        bra   swd_fin
swd_read:
        ldi   r2, 16
        call  swd_bits
        mov   r7, r4
        ldi   r2, 16
        call  swd_bits
        mov   r1, r4
        ldi   r2, 2
        call  swd_bits          ; parity bit, turnaround cycle
        xor   r5, r4            ; take the turnaround sample out again
        shl   r5                ; C = data parity ^ parity bit
        ldi   r5, 0
        rcl   r5                ; r5 = 1 on a parity error
        bra   swd_fin
swd_trn:
        ldi   r2, 1
        call  swd_bits          ; the turnaround cycle after the ACK
swd_fin:
        clr   SWDIO
        oen   SWDIO             ; SWDIO driven low until the next command
        ldi   r2, 5
swd_ackshift:
        shr   r3
        djnz  r2, swd_ackshift
        push  r3                ; the ACK
        cmpi  r3, 1
        bne   swd_cmd
        cmpi  r6, 4
        bne   swd_cmd
        push  r7                ; read data, little-endian
        swap  r7
        push  r7
        push  r1
        swap  r1
        push  r1
        push  r5                ; parity check
        bra   swd_cmd

; ---- r2 clock cycles (1..16). Each: SWCLK falls on the tick, the next bit
;      of r0 goes to SWDIO (it reaches the pad only while SWDIO is driven),
;      SWCLK rises on the next tick, and SWDIO as it was just before that
;      edge is shifted into r4[15] and folded into r5[15].
swd_bits:
        setd  0                 ; the next tick, whenever the caller arrives
swd_bit:
        waitd 1
        clr   SWCLK
        shr   r0
        wrc   SWDIO
        waitd 1
        set   SWCLK
        rdc   SWDIO             ; the pad one cycle before SWCLK rises
        rcr   r4
        xor   r5, r4
        djnz  r2, swd_bit
        ret
