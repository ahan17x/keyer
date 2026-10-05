; PS/2 host: the computer side of a keyboard or mouse port, one thread.
; Receives the device's frames and sends the host's commands.
; CLK and DATA are open-drain with external pull-ups; the device generates
; the clock in both directions.
;
; Host protocol.
;   Outbox: every byte the device sends, as received. One value, PS2_ESC
;   (0xA5, which no keyboard sends), is an escape:
;     ESC ESC   a received byte equal to PS2_ESC
;     ESC 0x01  a frame with a parity error, dropped
;     ESC 0x02  a frame with a framing error (start bit 1 or stop bit 0), dropped
;     ESC 0x03  the device stopped clocking in the middle of a frame, dropped
;     ESC 0x04  command not acknowledged: all eleven clocks came, no ACK bit
;     ESC 0x05  command not clocked: the device did not start within
;               PS2_RTS_TICKS of the request to send, or stopped in the
;               middle; DATA is released
;   Inbox: each byte is sent to the device as one command frame as soon as
;   the bus is idle. A command that goes through reports nothing; the
;   device's own answer (0xFA and so on) arrives as received bytes. Queue the
;   next command byte after that answer, as any PS/2 host does.
;
; Waiting for either side. One thread polls both conditions in a
; two-instruction loop (4 cycles a turn): CLK low starts a receive, a byte in
; the inbox starts a send. Polling is enough because the first CLK low phase
; of a frame lasts 30 us or more and the start bit is on DATA for all of it.
; A receive in progress is always finished before a command is sent. If a
; frame starts while the host is pulling CLK low for a command, the device
; aborts it and sends it again later, as the protocol requires.
;
; Receive. The start bit is sampled 4 to 7 cycles after the first falling
; edge reaches the pad, each later bit 4 or 5 cycles after its falling edge
; (2 of them are the synchroniser). The eleven bits must arrive within the
; frame timeout or the frame is reported with ESC 0x03.
;
; Send. CLK is held low for PS2_INH_TICKS full ticks, DATA is pulled low
; (request to send, also the start bit) and CLK is released two cycles later.
; The device then clocks: each data bit, the parity bit and the stop bit
; (DATA released) go onto DATA 13 to 16 cycles after a falling edge, while CLK
; is low. At the eleventh falling edge the device must be holding DATA low.
;
; Flow control. A byte or report that finds the outbox full is not dropped:
; the firmware holds CLK low (inhibit) until the host has made room and for
; PS2_INH_TICKS more, so the device keeps its data and sends it afterwards.
;
; Timing constants, in cycles of the core clock (defaults for 50 MHz, 20 ns):
;   PS2_TICK       cycles per timer tick: 5000 = 100 us. Must be at least
;                  the device's CLK low time (30 to 50 us), because the wait
;                  for CLK to return high after a frame is one to two ticks.
;   PS2_INH_TICKS  inhibit time before a request to send: 1 tick = 100 us,
;                  the minimum the protocol asks for.
;   PS2_RTS_TICKS  how long the device may take to start clocking a command:
;                  150 ticks = 15 ms.
;   PS2_TMO_TICKS  frame timeout, from the first falling edge to the last:
;                  20 ticks, which is 1.9 to 2.0 ms (the tick phase is free).
;                  A frame is ten clock periods: 1 ms at 10 kHz, 0.6 ms at
;                  16.7 kHz.
;   Tick counts are 1 to 255.
;
; Speed. The device sets the bit rate (10 to 16.7 kHz, a CLK half period of
; 1500 to 2500 cycles at 50 MHz). The firmware follows any clock whose half
; period is at least 20 cycles: when sending, DATA changes up to 16 cycles
; after the falling edge and must be stable before the rising edge on which
; the device samples. Receiving alone works down to a half period of 9
; cycles (the first edge wait is reached up to 19 cycles after the first
; falling edge; the bit loop takes 14). Between frames the firmware is
; polling again at most 71 cycles after the eleventh falling edge; a real
; device leaves the bus idle for 50 us first.
;
; Registers: r0 frame shift register and the byte to push, r1 PS2_TICK,
; r2 bit counter, r3 parity, r4 event code, r5 start bit, r6 PS2_ESC,
; r7 the bit just sampled (0 outside the three instructions that use it, so
; the send path adds it as a zero).

.equ PS2_TICK, 5000
.equ PS2_INH_TICKS, 1
.equ PS2_RTS_TICKS, 150
.equ PS2_TMO_TICKS, 20
.equ PS2_ESC, 0xA5
.equ CLK, uio0
.equ DATA, uio1

.equ EV_PARITY, 1
.equ EV_FRAME, 2
.equ EV_RX_TMO, 3
.equ EV_NOACK, 4
.equ EV_TX_TMO, 5

ps2_init:
        od    CLK               ; open-drain, released
        od    DATA
        ldw   r1, PS2_TICK
        sett  r1                ; the tick timer runs from here on
        ldi   r6, PS2_ESC
        ldi   r7, 0
ready:                          ; every path comes back here
        setd  2                 ; at least one full tick
        wt1t  CLK               ; let CLK return high: the last low phase of a
                                ; frame, or the line rising after an inhibit
idle:
        bp0   CLK, rx           ; the device has started a frame
        bfe   idle              ; nothing to send: look again

; ---- host to device: one command byte
tx:
        clr   CLK               ; inhibit
        sett  r1                ; hold it for full ticks counted from here
        pop   r0                ; the command
        ldi   r4, EV_TX_TMO     ; what a timeout means on this path
        ldi   r3, 1             ; 1 + the number of ones sent: bit 0 is the odd parity bit
        ldi   r2, 8
        waitd PS2_INH_TICKS     ; timing: the POP above cannot block, BFE has just seen a byte in the inbox
        clr   DATA              ; request to send; this is the start bit
        set   CLK               ; release CLK: the device clocks from here
        setd  PS2_RTS_TICKS
        wtft  CLK               ; its first falling edge
        bcs   tmo
        setd  PS2_TMO_TICKS     ; the rest of the frame must follow in time
tx_bit:
        shr   r0                ; data bits, LSB first
        call  tx_put
        djnz  r2, tx_bit
        shr   r3                ; parity bit
        call  tx_put
        setc                    ; stop bit: release DATA
        call  tx_put            ; returns at the eleventh falling edge
        bp0   DATA, ready       ; the device holds DATA low there: acknowledged
        ldi   r4, EV_NOACK
        bra   report

tx_put:                         ; C -> DATA while CLK is low, then wait for the next falling edge
        wrc   DATA              ; 1 releases, 0 pulls low
        adc   r3, r7            ; count the ones (r7 = 0)
        wtft  CLK
        bcs   tmo               ; leaves the subroutine for good
        ret

; ---- device to host: CLK is low, the first clock pulse of a frame
rx:
        setd  PS2_TMO_TICKS     ; one deadline for the whole frame
        inr   r5, DATA          ; start bit
        ldi   r4, EV_RX_TMO
        ldi   r3, 0             ; XOR of the ten bits that follow
        ldi   r0, 0
        ldi   r2, 10            ; 8 data bits, parity, stop
rx_bit:
        wtft  CLK               ; next falling edge
        bcs   tmo
        inr   r7, DATA          ; sample
        xor   r3, r7
        shr   r7                ; the bit into C (r7 is 0 again)
        rcr   r0                ; and into the frame from the top
        djnz  r2, rx_bit
        shl   r0                ; r0 was stop:parity:d7..d0:000000; C = stop bit
        rcl   r5                ; r5 = start:stop
        ldi   r4, EV_FRAME
        cmpi  r5, 1             ; must be 0:1
        bne   report
        ldi   r4, EV_PARITY
        cmpi  r3, 0             ; with stop = 1 the XOR is 0 when the parity is odd
        bne   report
        shl   r0                ; drop the parity bit
        swap  r0                ; the data byte
        cmp   r0, r6
        bne   last              ; an ordinary byte goes out as it is
        mov   r4, r0            ; a byte equal to ESC goes out as ESC ESC
tmo:                            ; a timed wait gave up; r4 says on which path
        set   DATA              ; release DATA (a command may be holding it low)
report:                         ; ESC, then r4
        mov   r0, r6
        call  put
        mov   r0, r4
last:
        call  put
        bra   ready

put:                            ; r0 -> outbox; a full outbox inhibits the device meanwhile
        pushnb r0
        bcs   put_ret           ; C = 1: pushed
        clr   CLK               ; full: hold the device off
        push  r0                ; until the host has made room
        sett  r1
        waitd PS2_INH_TICKS     ; and never for less than the inhibit time
        set   CLK
put_ret:
        ret
