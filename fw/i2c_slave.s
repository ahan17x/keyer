; I2C slave, EEPROM-style (24Cxx behaviour), one thread: entry i2c_init
; (word 0), on thread 0 or, with its PC set there, thread 1. Open-drain pins,
; external pull-ups. The chip is the slave: an external master drives SCL.
;
; On the bus
;   START, I2C_ADDR + W, pointer byte    sets the pointer (ACK, ACK); only
;                                        the low bits of the byte count
;   ... then data bytes                  ACKed and handed to the host (below)
;   START, I2C_ADDR + R, read, read ...  bytes from the pointer, which
;                                        increments with each byte and wraps
;                                        at TABLE_BYTES, until the master NACKs
;   Any other address is ignored: no ACK, SDA never driven, until the next
;   START. A START (repeated START) or a STOP is recognised at any bit,
;   including in the middle of a byte, whenever the slave is not itself
;   holding SDA low. The pointer survives STOP, so a read with no pointer
;   write continues where the last transfer ended (current-address read).
;
; Data lives in program memory (the CPU has no data memory). The host loads
; a jump table at TABLE: entry i is the two words `LDI r0, byte_i` / `RET`,
; and the firmware reads byte i with `CALL tab`, where `tab` is a `JMPR` to
; TABLE + 2 * i. Memory split: the program is 86 words at 0..85 (it may grow
; to TABLE - 1 = 127), the table is TABLE_BYTES * 2 = 128 words at 128..255,
; i.e. 64 bytes. TABLE_BYTES must be a power of two (the wrap is an AND).
;
; Host. Load the program and the table, run the thread; it listens for a
; START from 18 cycles later. Inbox: unused. Outbox: one pair (pointer,
; byte) for every data byte a master writes after the pointer byte; the
; pointer then increments. The thread cannot store the byte (only the host
; writes program memory, and only while both threads are stopped), so the
; host applies it: while the bus is idle (thread blocked at `idle`) stop
; the thread, write `LDI r0, byte` at TABLE + 2 * pointer, run it again; it
; resumes where it was and keeps the pointer. Until then reads return the
; old contents, and while stopped the slave does not answer (like an EEPROM
; during its write cycle). Write buffer: a data byte is ACKed only if its
; pair is certain to fit the 16-entry outbox, that is for at most WBUF = 8
; pairs since the outbox was last seen empty. Otherwise the byte is NACKed
; and dropped, the pointer is not advanced, and the slave ignores the bus
; until the next START. So the pushes never block and a pair is never torn.
;
; Clock stretching (the slave holding SCL low). It pulls SCL low after the
; eighth bit of every byte it receives (the address byte of every transfer
; on the bus, its own pointer and data bytes) while it decides the ACK, and
; after every ACK bit of its own transfers while it prepares the next byte.
; It lets SCL go at most 56 cycles after the falling edge (32 after an
; address byte that is not its own) and never holds it for more than 40
; cycles, so a master whose SCL low time is 56 cycles or more never sees a
; stretch; a faster one must honour it. It never stretches inside a byte
; and never waits for the host while stretching.
;
; Timeout (SMBus style). Each time the slave starts waiting for a clock
; pulse it sets a deadline TMO_TICKS ticks of TMO_PERIOD cycles ahead (so
; the limit is between TMO_TICKS - 1 and TMO_TICKS periods). If by then SCL
; has not risen, or has risen with SDA low and not fallen again, the master
; is taken for dead: the slave releases SDA and waits for the next START
; instead of holding the bus. (With SDA high during SCL high nobody holds
; the bus and there is no limit.) Releasing SDA while SCL is stuck high
; looks like a STOP, which is the point: the bus is free again. Defaults:
; 30 ticks of 50000 cycles, 29 to 30 ms at 50 MHz (SMBus asks for 25 to 35
; ms); TMO_PERIOD at most 65535. Choose it longer than the slowest clock
; pulse (low phase plus high phase) a healthy master may produce.
;
; Timing. The thread sees a pin two cycles late and runs every other cycle.
; During SCL high it polls SCL and SDA (one turn is 4 cycles with SDA high,
; 6 with SDA low), so it acts on a falling edge 2 to 7 cycles after it.
; What the master must provide, in core cycles:
;   SCL low  >= 16   after the eighth bit the stretch begins up to 16 cycles
;                    after the falling edge and has to begin while the master
;                    still holds SCL low; a bit the slave sends is on SDA 9
;                    to 14 cycles after the falling edge
;   SCL high >= 16   SDA is sampled 4 to 5 cycles after the rising edge, up
;                    to 9 when the thread comes late out of a 16-cycle low
;                    phase; a START is acted on within 9 cycles of its SDA
;                    edge, and 13 cycles after a STOP the thread is waiting
;                    for the next START
;   START and STOP set-up and hold, bus free time >= 16
; That is a master with a quarter period of 8 cycles, which the tests run;
; at 7 a bit the slave sends can change in the cycle SCL rises. 32 cycles
; per bit before stretching is 1.56 Mbit/s at 50 MHz, so Standard mode
; (100 kHz), Fast mode (400 kHz) and Fast-mode Plus (1 MHz) are all inside
; it. The limit is the low phase; the 16 cycles for the high phase and for
; START and STOP are what was tested, not their own minimum.
;
; Registers: r0 shift register (received byte, byte being sent), r1 pointer,
; r2 bit counter, r3 scratch (table address, status), r4 what the next
; received byte is (0 address, 1 pointer, 2 data), r5 pairs pushed since the
; outbox was last empty, r6 saved LR, r7 timeout tick period.

.equ SCL, uio2
.equ SDA, uio3
.equ I2C_ADDR, 0x50             ; 7-bit address
.equ TABLE, 128                 ; first word of the data table
.equ TABLE_BYTES, 64            ; bytes in the table, a power of two
.equ WBUF, 8                    ; pairs that fit the outbox (16 entries / 2)
.equ TMO_PERIOD, 50000          ; cycles per timeout tick (1 ms at 50 MHz)
.equ TMO_TICKS, 30              ; ticks before a silent master is given up

i2c_init:
        od    SCL               ; open-drain, released
        od    SDA
        ldw   r7, TMO_PERIOD
        sett  r7                ; free-running tick for the bus timeout
        ldi   r1, 0             ; pointer
        ldi   r5, 0             ; write buffer empty
        bra   idle

; ---- clk: one SCL pulse. Releases a stretch, waits for SCL high, samples
;      SDA into C, then watches both lines until SCL falls and returns with
;      C = the sampled bit. An SDA edge while SCL is high abandons the call:
;      falling = START, rising = STOP. So does the timeout. A START or STOP
;      needs SCL high before the SDA edge (the loop's SCL test) and after it
;      (the test that follows), so a data change close to either SCL edge is
;      never mistaken for one. Level waits, not edge waits: the thread may
;      arrive after SCL has already risen.
clk:
        set   SCL               ; end a stretch (no effect when not stretching)
        setd  TMO_TICKS
        wt1t  SCL
        bcs   bus_timeout       ; SCL never rose
        rdc   SDA
        bcc   clk_h0
clk_h1:                         ; SDA high: nobody is driving it
        bp0   SCL, clk_ret
        bp1   SDA, clk_h1
        bp1   SCL, start        ; SDA fell and SCL is still high: START
clk_ret:                        ; (SDA fell after SCL: a data change)
        ret
clk_h0:                         ; SDA low: the master's bit, or our own drive
        bp0   SCL, clk_ret
        bdr   bus_timeout       ; SCL never fell
        bp0   SDA, clk_h0
        bp0   SCL, clk_ret      ; (SDA rose after SCL fell: a data change)
        call  clk_h1            ; SDA rose and SCL is still high: STOP. Both
                                ; lines are high: watch them for a START

; ---- idle: not addressed. Follow the clock pulses on the bus, which are
;      somebody else's, until clk finds a START. The host restarts the
;      thread at `listen` after it has stopped it.
listen:
bus_timeout:
        set   SDA               ; let go of the bus
idle:
        call  clk
        bra   idle
start:                          ; (repeated) START; neither line is driven
        ldi   r4, 0             ; the address byte comes first
        wt0   SCL

; ---- receive one byte, MSB first, then stretch while deciding what it is
rx_byte:
        ldi   r2, 8
rx_bit:
        call  clk
        rcl   r0
        djnz  r2, rx_bit
        clr   SCL               ; stretch
        andi  r0, 0xFF
        cmpi  r4, 1
        beq   got_ptr
        bhs   got_data

got_addr:
        xori  r0, I2C_ADDR * 2  ; 0 = our address + W, 1 = our address + R
        cmpi  r0, 2
        bhs   idle              ; somebody else's: no ACK; clk ends the stretch
        call  ack
        inc   r4                ; a write continues with the pointer byte
        shr   r0                ; C = R/W
        bcc   rx_byte

; ---- send bytes from the table until the master NACKs. SCL is stretched.
tx_byte:
        mov   r3, r1
        shl   r3
        addi  r3, TABLE         ; table entry = TABLE + 2 * pointer (mod 256)
        call  tab               ; r0 = byte
        rev8  r0                ; MSB into bit 0: OUTR sends bit 0
        inc   r1
        andi  r1, TABLE_BYTES - 1
        ldi   r2, 8
tx_bit:
        outr  SDA, r0           ; first thing after the falling edge
        shr   r0
        call  clk               ; a START/STOP is seen whenever we send a 1
        djnz  r2, tx_bit
        set   SDA               ; release for the master's ACK
        call  clk               ; C = 1: NACK
        bcs   idle              ; done; a STOP or START follows
        clr   SCL               ; ACK: stretch while fetching the next byte
        bra   tx_byte
tab:
        jmpr  r3                ; -> LDI r0, byte / RET

got_ptr:
        andi  r0, TABLE_BYTES - 1
        mov   r1, r0
        inc   r4                ; data bytes follow
        call  ack
        bra   rx_byte

got_data:
        rds   r3
        andi  r3, 4             ; outbox empty?
        beq   gd_room
        ldi   r5, 0             ; yes: the whole write buffer is free again
gd_room:
        cmpi  r5, WBUF
        bhs   idle              ; no room for a pair: no ACK, drop the byte
        inc   r5
        push  r1                ; never blocks: there is room for the pair
        push  r0
        inc   r1
        andi  r1, TABLE_BYTES - 1
        call  ack
        bra   rx_byte

; ---- ack: drive SDA low for the ninth clock pulse, then stretch and
;      release SDA. Saves LR in r6 because it calls clk.
ack:
        rdlr  r6
        clr   SDA
        call  clk
        clr   SCL               ; stretch until the caller's next clk
        set   SDA
        jmpr  r6
