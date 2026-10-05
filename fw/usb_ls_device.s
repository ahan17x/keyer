; USB low-speed device (1.5 Mbit/s), one thread, on the serializer engine
; (docs/SERIALIZER.md, SEMANTICS 15). It enumerates: bus reset, endpoint-0
; control transfers with SETUP, IN and OUT transactions, DATA0/DATA1
; toggling, ACK/NAK/STALL, GET_DESCRIPTOR (device; configuration with its
; interface and endpoint descriptors) in 8-byte packets with a short last
; packet and wLength honoured, SET_ADDRESS (in force after the status
; stage) and SET_CONFIGURATION. Start thread 0 at address 0; thread 1 is not
; used (but program memory is full: 253 of 256 words).
;
; The device: vendor-specific class (0xFF), USB 1.10, VID 0x1209 PID 0x0001
; (the pid.codes test range: change both for anything real), bcdDevice
; 1.00, no strings, one configuration (value 1, bus powered, 100 mA), one
; interface (class 0xFF, one endpoint), endpoint 1 IN interrupt, 8 bytes,
; 10 ms. Endpoint 1 always NAKs: the device never has a report. The bytes
; are the SERIC immediates between the labels dev, cfg and desc_end, which
; is where the tests read them from.
;
; Requests. Accepted SETUPs are always ACKed. GET_DESCRIPTOR (0x80, 6) of
; type 1 or 2 sends min(wLength, length) bytes; the descriptor index and
; wIndex are not checked. SET_ADDRESS (0x00, 5) and SET_CONFIGURATION (0x00,
; 9, value 0 or 1) have an IN status stage with a zero-length DATA1; the new
; address is taken when the host ACKs it. Every other request (GET_STATUS,
; GET_CONFIGURATION, SET/CLEAR_FEATURE, string or other descriptor types,
; class and vendor requests, SET_CONFIGURATION > 1) gets STALL on its data
; or status stage, and an OUT in that state gets STALL too, until the next
; SETUP. An IN on endpoint 0 with no transfer in progress (after a reset, or
; after a status stage) gets NAK. An IN after the last data packet of a
; GET_DESCRIPTOR gets a zero-length packet. Not implemented: suspend and
; resume (a host that sends keep-alives never suspends the device), remote
; wakeup, GET_STATUS and GET_CONFIGURATION (they need 7 more words than
; there are), a check of the configuration index.
;
; What is ignored (no answer): tokens with a bad CRC-5, a PID whose check
; nibble is wrong, tokens for another address, SETUP/OUT for another
; endpoint, IN for endpoints other than 0 and 1, DATA packets with a bad
; CRC-16 or a stuffing, framing or overrun error (no ACK: the host
; retries), a SETUP whose DATA0 does not come within DATA_TMO.
;
; Pins: D+ uio0, D- uio1 (serializer pair 0; SERCFG USBCFG = 0x35: NRZI,
; bit stuffing, CRC-16, receiver on, PID outside the CRC). Both are inputs
; except while the engine sends; the bus idles in J through the host's
; pull-downs and a 1.5 k pull-up on D- (low speed), which is outside the chip
; and always connected.
;
; Host interface: the outbox gets the bRequest of every accepted SETUP
; (PUSHNB: dropped if the outbox is full; the device never waits for the
; SPI host). The inbox is not used.
;
; Timing constants (ticks of the thread timer, one tick per bit time):
;   BITT      core cycles per bit: 32 at 48 MHz. 16 (24 MHz) works with
;             less firmware slack (below); 8 (12 MHz) is not supported.
;   RESP      4: the response deadline after the end of the host's packet.
;   IDLE_TMO  8: while idle the bus is checked for SE0 this often.
;   RST_TMO   5: SE0 still there 4 to 5 bit times after it was seen is a
;             bus reset. USB lets a device treat SE0 longer than 2.5 us
;             (3.75 bit times) as a reset; an EOP or a keep-alive is SE0 for
;             at most 2.25 bit times (1.5 us). A reset is recognised once
;             SE0 has lasted 4 to 13 bit times when idle; if the device was
;             waiting for the DATA packet of a token, up to DATA_TMO + 13
;             (about 100 us). The host drives reset for 10 ms.
;   TOK_TMO   24: the two token bytes after the PID (16 bits, at most 2
;             stuffed, half a bit to the EOP sample).
;   DATA_TMO  140: the whole DATA packet of a SETUP or OUT after the token
;             (11 bytes and up to 15 stuffed bits = 103, plus the host's gap).
;   ACK_TMO   24: the host's handshake PID after our packet's tail; USB
;             gives the host 6.5 bit times, its PID is complete 15.5 later.
;
; Response timing (the slot and cycle counts are from the listing).
;   The host's SE0 starts at a bit boundary h. The receiver samples it at
;   h + T/2 + 3 (two synchroniser cycles, the edge cycle, half a period),
;   so the blocked SERRXT that reads the frame end completes at
;   h + T/2 + 4 or + 5, and `setd RESP` right after it at s = h + T/2 + 6
;   or + 7. The tick that makes NOW = DEADLINE comes 3T + 1 to 4T cycles
;   after s (the tick phase against the host's bits is arbitrary); `waitd 0`
;   completes in the slot after it, `seri SYNC` queues the SYNC, the engine
;   takes it at the next tick and the first K is on D+/D- T + 1 cycles
;   later. Relative to the host's SE0-to-J transition at h + 2T, the
;   response starts (RESP - 0.5) T + 7 to (RESP + 0.5) T + 7 cycles later:
;   3.72 to 4.72 bit times at T = 32 (USB: 2 to 6.5), 1.72 bit times (27
;   slots) of margin early and 1.78 (28 slots) late. At T = 16: 3.94 to
;   4.94.
;   Firmware budget between `setd RESP` and `waitd 0`: at least 3T + 1
;   cycles, 48 slots at T = 32, 24 at T = 16. The paths, in instructions
;   after the setd: IN to a DATA packet 14 (tok 0x33 to in_data 0x9C), IN to
;   a zero-length status packet 15, IN to NAK or STALL 15 (12 for endpoint
;   1), SETUP to ACK 7, OUT to ACK 10 (STALL 9). Slack: at least 33 slots at
;   T = 32 and 9 at T = 16; the tests measure 34 and 11 over swept phases.
;   At T = 8 the budget is 12 slots, less than the IN path.
;   Between packets: each data byte is queued by SERIC, CALL chk, DJNZ, RET
;   (3 slots after the SERIC completes; the engine needs it within 128).
;   After our packet the receiver runs from the end of the tail's J, one
;   bit after our EOP's SE0-to-J; the host may not start its SYNC before 2.
;
; Structure. The state (r6) is the address of the IN handler: hs_nak (no
; transfer), hs_stall, in_data (control-read data stage) or in_sin (the
; status stage of SET_ADDRESS / SET_CONFIGURATION, a zero-length DATA1).
; Descriptor bytes are a table of `seric b` / `call chk` pairs, 16 words per
; 8-byte packet; r5 points at the next packet's first pair and r0 counts the
; bytes left in the transfer: chk returns while bytes are left and falls into
; pkt_done at zero; the pair that ends each 8-byte packet calls chk8, which
; always ends the packet. On the host's ACK, r5 += 16, the count becomes
; what is left, and the DATA PID toggles; without an ACK nothing advances,
; so the next IN repeats the packet with the same PID.
;
; Registers: r0 token code (0: endpoint 0 at our address, 0x80: endpoint 1),
; wLength, the byte count of a packet; r1 status, handshake PID; r2 PID,
; wValue high; r3 bytes left in the data stage (bmRequestType while a SETUP
; is decoded); r4 PID of the next DATA packet (bRequest while decoding);
; r5 table address of the next packet, or the new address in in_sin (wValue
; low while decoding); r6 state; r7 device address.
;
; Size: 253 words, 85 of them the descriptor table (43 bytes).

.equ BITT,     32       ; core cycles per bit: 48 MHz / 1.5 Mbit/s
.equ USBCFG,   0x35     ; SERCFG: NRZI | stuffing | CRC-16 | receiver | PID outside the CRC | pair 0
.equ RESP,     4        ; response deadline: ticks after the end of the host's packet
.equ IDLE_TMO, 8        ; bus-reset poll interval while idle, ticks
.equ RST_TMO,  5        ; SE0 longer than 4 to 5 bit times is a bus reset
.equ TOK_TMO,  24       ; the two token bytes and the end after the PID
.equ DATA_TMO, 140      ; the DATA packet of a SETUP or OUT, from the end of the token
.equ ACK_TMO,  24       ; the host's handshake PID, from the end of our DATA packet
.equ DP, uio0           ; D+ = pair 0 P
.equ DM, uio1           ; D- = pair 0 N

.equ P_OUT,   0xE1
.equ P_IN,    0x69
.equ P_SETUP, 0x2D
.equ P_DATA0, 0xC3
.equ P_DATA1, 0x4B
.equ P_ACK,   0xD2
.equ P_NAK,   0x5A
.equ P_STALL, 0x1E
.equ SYNC,    0x80
.equ DEV_LEN, 18
.equ CFG_LEN, 25

init:   ldi   r1, BITT
        sett  r1                ; one tick per bit: NOW counts bit times
        ldi   r1, USBCFG
        sercfg r1
        ldi   r6, hs_nak
rstchk: bp1   DM, idle          ; not SE0. D- first: a K-to-J edge between the
        bp1   DP, idle          ; two samples leaves D- high for the WT1T below
        setd  RST_TMO
        wt1t  DM                ; an end of packet or a keep-alive is over in 2 bits
        bcc   idle
busrst: ldi   r7, 0             ; bus reset: address 0, nothing in progress
        ldi   r6, hs_nak
idle:   setd  IDLE_TMO
        serrxt r2               ; PID (the engine has consumed SYNC)
        bcs   rstchk
        beq   idle              ; a frame end with no byte
disp:   cmpi  r2, P_IN
        beq   tok
        cmpi  r2, P_OUT
        beq   tok
        cmpi  r2, P_SETUP
        beq   tok
skip:   serrx r1                ; not for us: drain to the end of the frame
        bne   skip
        jmp   idle

chk:    djnz  r0, chk_r
pkt_done:
        serwt
        setd  ACK_TMO
        serrxt r2
        bcs   idle
        beq   idle
        cmpi  r2, P_ACK
        bne   disp
        cmpi  r6, in_sin
        bne   adv
        mov   r7, r5
        ldi   r6, hs_nak
        jmp   idle
chk_r:  ret
adv:    addi  r5, 16
        mov   r3, r0
        xori  r4, P_DATA0 ^ P_DATA1
        jmp   idle

tok:    setd  TOK_TMO
        serrxt r0               ; {ENDP[0], ADDR}
        serrxt r1               ; {CRC5, ENDP[3:1]}
        andi  r1, 7
        swap  r1
        xor   r0, r1
        xor   r0, r7            ; 0: endpoint 0 at our address, 0x80: endpoint 1
        serrxt r1               ; the end of the token: r1 = status
        setd  RESP              ; the response deadline, from the end of the packet
        bcs   idle
        bne   skip              ; a fourth byte: not a token
        ori   r1, 0x40
        cmpi  r1, 0x70          ; frame end, CRC-5 good, no error
        bne   idle
        cmpi  r2, P_IN
        bne   tok_so
in_tok: cmpi  r0, 0x80
        beq   hs_nak            ; endpoint 1: nothing to report
        cmpi  r0, 0
        bne   idle
        jmpr  r6                ; the IN handler of the current state
tok_so: cmpi  r0, 0
        bne   idle
        cmpi  r2, P_OUT
        beq   out_tok

setup:  setd  DATA_TMO
        serrxt r2
        bcs   idle
        cmpi  r2, P_DATA0
        bne   skip
        serrxt r3               ; bmRequestType
        serrxt r4               ; bRequest
        serrxt r5               ; wValue low
        serrxt r2               ; wValue high
        serrxt r0               ; wIndex low
        serrxt r0               ; wIndex high
        serrxt r0               ; wLength low
        serrxt r1               ; wLength high
        swap  r1
        or    r0, r1            ; r0 = wLength
        serrxt r1               ; CRC-16
        serrxt r1
        serrxt r1               ; the end: r1 = status
        setd  RESP
        bcs   idle
        bne   skip
        ori   r1, 0x20
        cmpi  r1, 0x70          ; frame end, CRC-16 good, no error
        ldi   r6, hs_stall      ; whatever was in progress is over
        bne   idle
        waitd 0
        seri  SYNC
        seri  P_ACK
        pushnb r4               ; tell the SPI host: bRequest of every accepted SETUP
        cmpi  r3, 0x80
        bne   setreq
        cmpi  r4, 6             ; GET_DESCRIPTOR
        bne   s_end
        ldi   r5, dev
        ldi   r3, DEV_LEN
        cmpi  r2, 1
        beq   gd_len
        ldi   r5, cfg
        ldi   r3, CFG_LEN
        cmpi  r2, 2
        bne   s_end
gd_len: cmp   r0, r3            ; C = (wLength < length)
        bcc   gd_set
        mov   r3, r0
gd_set: ldi   r6, in_data
        bra   s_d1
setreq: cmpi  r3, 0
        bne   s_end
        andi  r5, 0x7F
        cmpi  r4, 5             ; SET_ADDRESS
        beq   s_sin
        cmpi  r4, 9             ; SET_CONFIGURATION
        bne   s_end
        cmpi  r5, 2
        bcc   s_end
        mov   r5, r7            ; "new" address = the current one
s_sin:  ldi   r6, in_sin
s_d1:   ldi   r4, P_DATA1
s_end:  jmp   hs_wt

out_tok: setd  DATA_TMO
        serrxt r2
        bcs   idle
        cmpi  r2, P_DATA1
        beq   outl
        cmpi  r2, P_DATA0
        bne   skip
outl:   serrx r1
        setd  RESP
        bne   outl
        ori   r1, 0x20
        cmpi  r1, 0x70
        bne   idle
        cmpi  r6, hs_stall
        beq   hs_stall
        ldi   r6, hs_nak
        bra   hs_ack

hs_stall: ldi r1, P_STALL
        bra   hsend
hs_nak: ldi   r1, P_NAK
        bra   hsend
hs_ack: ldi   r1, P_ACK
hsend:  waitd 0
        seri  SYNC
        sertx r1
hs_wt:  serwt
        jmp   idle

in_sin: ldi   r3, 0
in_data:
        mov   r0, r3
        waitd 0
        seri  SYNC
        sertx r4
        cmpi  r0, 0
        beq   zlp
        jmpr  r5
zlp:    seri  0                 ; the CRC-16 of no bytes
        seri  0
        jmp   pkt_done
chk8:   dec   r0
        jmp   pkt_done

dev:    seric 0x12
        call  chk
        seric 0x01
        call  chk
        seric 0x10
        call  chk
        seric 0x01
        call  chk
        seric 0xFF
        call  chk
        seric 0x00
        call  chk
        seric 0x00
        call  chk
        seric 0x08
        call  chk8
        seric 0x09
        call  chk
        seric 0x12
        call  chk
        seric 0x01
        call  chk
        seric 0x00
        call  chk
        seric 0x00
        call  chk
        seric 0x01
        call  chk
        seric 0x00
        call  chk
        seric 0x00
        call  chk8
        seric 0x00
        call  chk
        seric 0x01
        call  chk
cfg:    seric 0x09
        call  chk
        seric 0x02
        call  chk
        seric 0x19
        call  chk
        seric 0x00
        call  chk
        seric 0x01
        call  chk
        seric 0x01
        call  chk
        seric 0x00
        call  chk
        seric 0x80
        call  chk8
        seric 0x32
        call  chk
        seric 0x09
        call  chk
        seric 0x04
        call  chk
        seric 0x00
        call  chk
        seric 0x00
        call  chk
        seric 0x01
        call  chk
        seric 0xFF
        call  chk
        seric 0x00
        call  chk8
        seric 0x00
        call  chk
        seric 0x00
        call  chk
        seric 0x07
        call  chk
        seric 0x05
        call  chk
        seric 0x81
        call  chk
        seric 0x03
        call  chk
        seric 0x08
        call  chk
        seric 0x00
        call  chk8
        seric 0x0A
        call  chk
desc_end:
