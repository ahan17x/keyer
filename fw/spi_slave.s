; SPI slave, mode 0 (CPOL = 0, CPHA = 0), MSB first, framed by CS_n. One thread.
; The chip is the slave: an external master drives SCK, MOSI and CS_n.
;
; Pins: SCK ui3, MOSI ui4, CS_n ui5 (inputs); MISO uio0, push-pull, driven
; only while CS_n is low and tri-stated (OEF) while it is high, so the pin can
; sit on a shared bus. SCK and MOSI activity while CS_n is high is ignored.
;
; Host protocol (the inbox and outbox of the thread that runs this; start it
; at ss_init):
;   outbox  every complete byte the master clocked in on MOSI, in order.
;           There are no frame markers. If the outbox is full (16 unread
;           bytes) further bytes are dropped: the thread never blocks on the
;           host.
;   inbox   reply bytes. One is taken for every byte the master clocks and is
;           shifted out on MISO while that byte is clocked. A reply byte is
;           taken from the inbox at the latest possible moment: while CS_n is
;           high for the first byte of a frame, and after the seventh SCK
;           falling edge of a byte for the byte that follows it. If the inbox
;           is empty at that moment the filler SS_FILL (0xFF) is sent
;           instead. A filler that was staged while CS_n was high is replaced
;           by the first byte the host queues before CS_n falls, so "queue
;           the reply, then let the master clock the frame" always works. A
;           reply byte that was taken but not clocked at all when CS_n rose
;           (every frame ends with one) is not lost: it is the first byte of
;           the next frame.
;
; Aborted frame (CS_n rises with a partial byte): the received bits of the
; partial byte are discarded and nothing is reported in the outbox; the reply
; byte that was partly shifted out is used up; the next frame starts on a
; byte boundary with the next reply byte (or the filler). A byte counts once
; its eighth SCK falling edge has been seen with CS_n low.
;
; Timing. There are no timing constants: the slave follows the master's
; clock. Nothing here can hang on a dead master: both clock-phase waits poll
; CS_n, so the thread leaves a frame within a few cycles of CS_n rising even
; if SCK is stuck at either level, and a master that simply stops with CS_n
; low is a (very) slow master. What the master must respect, in core clock
; cycles (20 ns at 50 MHz):
;   SCK half period       >= 10   (SCK <= clk / 20: 2.5 MHz at 50 MHz)
;   CS_n low to first SCK rising edge              >= 20   (0.4 us)
;   last SCK falling edge to CS_n high             >= 2
;   CS_n high between frames                       >= 32   (0.64 us)
;   CS_n high after a frame aborted in mid-byte    >= 48   (with less, the
;                    next frame is received whole but its first reply byte
;                    may be the filler; the reply bytes stay in order)
;   SCK low for 2 cycles after CS_n rises (mode 0 idles low anyway)
; What the slave does: MOSI is sampled 2 to 7 cycles after the SCK rising
; edge reaches the pad (two synchroniser cycles, up to three for the poll
; loop, two more on the first and last bit of a byte, where the byte
; bookkeeping runs). MISO changes 5 to 8 cycles after the SCK falling edge
; (two synchroniser cycles, up to three for the poll loop, one slot for WRC,
; one cycle to the pad), so at the minimum half period of 10 it is stable 2
; cycles before the next rising edge. MISO is driven at most 18 cycles after
; CS_n falls and released at most 24 cycles after CS_n rises (10 when the
; frame ended on a byte boundary and the thread was already polling).
; What limits the rate: the two bit periods around a byte boundary carry four
; extra slots each (fetch the next reply byte; push the received byte and
; reload), which delays the MOSI sample of those bits to 7 cycles after the
; edge; at a half period of 9 that is the last cycle before MOSI changes.
;
; Registers: r0 shift register (reply bits leave at the top through RCL,
; received bits enter at the bottom; between bytes {reply[6:0], F, 0x00} with
; reply[7] already on MISO and F = 1 if the staged byte is the filler);
; r1 the next reply byte; r2 bit counter; r4 = 0x0100 (the F bit);
; r5 = {0x80, SS_FILL} (the filler with its F mark).

.equ SS_FILL, 0xFF
.equ SCK,  ui3
.equ MOSI, ui4
.equ CSN,  ui5
.equ MISO, uio0

ss_init:
        ldw   r5, 0x8000 | SS_FILL
        ldw   r4, 0x0100
        wt1   CSN               ; never join a frame that is already running
ss_fill:                        ; nothing is staged: stage the filler
        mov   r0, r5            ; {0x80, FILL}
        swap  r0                ; {FILL, 0x80}
        shl   r0                ; C = FILL[7], r0 = {FILL[6:0], F = 1, 0x00}
ss_stage:
        oef   MISO              ; deselected: off the bus
        wrc   MISO              ; MSB of the staged byte waits in the output register
ss_idle:
        tst   r0, r4            ; is the staged byte the filler?
        beq   ss_idle_real
ss_idle_fill:                   ; yes: replace it if the host queues a byte in time
        bp0   CSN, ss_sel
        popnb r0                ; r0 unchanged if the inbox is empty
        bcc   ss_idle_fill
        swap  r0
        shl   r0                ; C = byte[7], r0 = {byte[6:0], F = 0, 0x00}
        wrc   MISO
ss_idle_real:
        wt0   CSN               ; frame start
ss_sel: oen   MISO
ss_byte:
        ldi   r2, 7
ss_bit:                         ; ---- bits 7..1 of a byte
        bp1   SCK, ss_smp       ; rising edge?
        bp0   CSN, ss_bit       ; still selected: keep polling
        bra   ss_xhi
ss_smp: rdc   MOSI
        rcl   r0                ; received bit in, next reply bit out to C
ss_lo:  bp0   SCK, ss_out       ; falling edge?
        bp0   CSN, ss_lo
        bra   ss_fill           ; CS_n rose while SCK was high: abort
ss_out: wrc   MISO
        djnz  r2, ss_bit
        mov   r1, r5            ; seven bits done: take the next reply byte now
        popnb r1                ; {0x00, byte}, or {0x80, FILL} if the inbox is empty
        swap  r1                ; {byte, 0x00} or {FILL, 0x80}
ss_bit8:                        ; ---- bit 0
        bp1   SCK, ss_smp8
        bp0   CSN, ss_bit8
        bra   ss_x8hi
ss_smp8:
        rdc   MOSI
        rcl   r0                ; r0 = {0x00, received byte}
        shl   r1                ; C = next reply[7], r1 = {next reply[6:0], F, 0x00}
ss_lo8: bp0   SCK, ss_out8
        bp0   CSN, ss_lo8
        bra   ss_x8lo
ss_out8:
        wrc   MISO              ; first bit of the next reply byte
        pushnb r0               ; the received byte (dropped if the outbox is full)
        mov   r0, r1
        bra   ss_byte

; ---- CS_n went high
ss_xhi: oef   MISO              ; while waiting for a rising edge in bits 7..1
        cmpi  r2, 7
        beq   ss_idle           ; no bit of this byte was clocked: the frame ended
                                ; on a byte boundary, the staged byte stays staged
        bra   ss_fill           ; mid-byte: abort, the partial byte is discarded
ss_x8hi:                        ; before bit 0 was clocked: abort, but the reply
        oef   MISO              ; byte already taken for the next byte stays staged
        shl   r1                ; (off the bus first: this path is the longest)
ss_x8lo:                        ; (same after bit 0 was sampled with SCK still high)
        mov   r0, r1
        bra   ss_stage
