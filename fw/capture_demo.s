; Capture-and-replay demo, thread 1 (docs/CAPTURE.md). Thread 0 runs
; fw/i2c_master.s at address 0; this program is loaded above it (CAP_DEMO_ORG)
; and the host sets PC1 to cap_demo.
;
; Host setup before RUN: CAP_CFG = group 0 (uio0..3), watch mask SCL|SDA
; (uio2, uio3 -> 0xC), trigger "SDA falls while SCL high" (pattern 0x4 under
; mask 0xC, the I2C START); CAP_BUF = (CAP_BASE, CAP_LEN); REP_CFG = group 0,
; drive mask 0xC; REP_BUF = (CAP_BASE, 0): replay as many entries as were
; recorded. PINMODE: uio2, uio3 open-drain. Then: PC1 = cap_demo, RUN1 only,
; and feed the I2C bytecode to INBOX0 ending with 0x05 (start the other
; thread).
;
; Flow: arm the capture, start thread 0, halt (so the capture engine owns
; this thread's fetch slots while the master runs). The master's 0x05
; command restarts this thread after the transaction: stop the master,
; disarm (the capture finishes), start the replay of the recording on the
; same pins, wait for it, halt. The host reads CR_COUNT and the entries.

.equ CAP_DEMO_ORG, 0x90
.org CAP_DEMO_ORG
cap_demo:
        ldi   r0, 1
        capc  r0                ; ARM the capture (status bit 0 = active)
        start                   ; thread 0: the I2C master
        halt                    ; free our fetch slots; thread 0 restarts us
        stop                    ; thread 0 done: stop it (its slots are now free)
        ldi   r0, 2
        capc  r0                ; DISARM: the capture drains and completes
cap_wait:
        rds   r1
        andi  r1, 0x80          ; bit 7: capture active
        bne   cap_wait
        ldi   r0, 4
        capc  r0                ; START the replay
rep_wait:
        rds   r1
        swap  r1
        andi  r1, 1             ; bit 8: replay active
        bne   rep_wait
        halt
