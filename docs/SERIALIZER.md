# The serializer: design note

Status: design of 2026-10-04 (DECISIONS D-036). The cycle-exact rules are
`docs/SEMANTICS.md` section 15; this note says what the engine is for, why
it has this shape, and how firmware uses it. Where the two differ,
SEMANTICS wins.

## 1. Why it exists

Firmware alone tops out at one pin write per two slots, and every bit of
line coding costs instructions. Two stretch protocols do not fit that:

- **USB low speed** (1.5 Mbit/s): NRZI, bit stuffing, CRC-5 and CRC-16, and
  a reply that must start 2 to 6.5 bit times after the host's packet ends.
  At 32 cycles per bit a thread has 16 slots per bit: enough to move bytes,
  not enough to also stuff, code, check a CRC and find the end of packet.
- **10BASE-T** (10 Mbit/s Manchester): a half-bit is 50 ns, two cycles at
  40 MHz. No instruction loop can place those edges.

So the bit level moves into hardware and firmware works in bytes: it hands
the engine a byte at a time and gets bytes, a frame end and a verdict back.

## 2. What it is

One engine, `src/keyer_ser.v`, with:

- a **transmitter**: an 8-bit holding register and an 8-bit shift register,
  least significant bit first; a frame is whatever bytes firmware supplies
  without a gap, and it ends when the holding register is empty at a byte
  boundary (so there is no length register and no "end" instruction);
- a **receiver**: clock recovery from the line's edges, an 8-bit shift
  register, a sync detector that finds the start of a frame, and an 8-bit
  holding register firmware reads;
- **line coding**, selected by the mode: NRZI (a 0 is a transition, a 1 is
  none; USB) or Manchester as in IEEE 802.3 (a 1 is low then high);
- **bit stuffing** (optional): after six consecutive ones the transmitter
  inserts a zero and the receiver removes it; a seventh one is reported;
- **CRCs**: a 5-bit register (USB CRC-5, receive check only) and a 32-bit
  register that is CRC-16 (USB) or CRC-32 (Ethernet) by configuration; the
  transmitter appends the complemented CRC of the bytes firmware marked,
  the receiver compares against the residual at the frame end;
- a **pin pair** `uio[2k]` (P), `uio[2k+1]` (N): driven as J (P low, N
  high), K (P high, N low) or SE0 (both low), or released; the receiver
  reads the same pair through the pin synchroniser;
- **framing status**: transmitter busy, byte available, in a frame, frame
  ended, both CRC verdicts, overrun, stuffing error, a frame that did not
  end on a byte boundary.

The symbol period is **the owning thread's timer period**: the transmitter
advances on that timer's ticks and the receiver measures its half and full
periods from the same `period` register. A thread therefore sets the bit
rate with the `SETT` it already has, its `NOW` counts symbol times, and
`SETD`/`WAITD` and every timeout are in symbol times on the same grid as
the bits on the wire.

| | NRZI mode (USB) | Manchester mode (10BASE-T) |
|---|---|---|
| Ticks per bit | 1 | 2 (half-bits) |
| Idle before a frame | whatever the pins hold (released: the bus pull-ups give J) | whatever the pins hold (firmware drives both low) |
| First symbol | the first data bit, coded from J | first half of the first bit |
| End of frame | SE0 for two ticks, J for one, then both pins released | P high, N low for six ticks (3 bit times: the 10BASE-T start of idle), then both driven low |
| Receive sync | the eight decoded bits `0x80` (KJKJKJKK) | the eight decoded bits `0xD5` (the start frame delimiter) |
| Receive end | SE0 at a sample point | no mid-bit transition for about 1.75 bit times |

## 3. How threads use it

Ten instructions, all in the existing XFER and MISC majors (ISA version 3):

| Instruction | Blocks until | Effect |
|---|---|---|
| `SERCFG rs` | - | configuration `<= rs[7:0]`, engine reset, the executing thread becomes the owner (its timer sets the symbol period) |
| `SERTX rs` / `SERI n` | the holding register is empty | queue a byte that is **not** in the CRC (SYNC, PID, preamble) |
| `SERTXC rs` / `SERIC n` | the holding register is empty | queue a byte that **is** in the CRC |
| `SERRX rd` | a byte is available or a frame has ended | byte: `rd <= byte`, `Z <= 0`; frame end: `rd <= status`, `Z <= 1` |
| `SERST rd` | - | `rd <= status` |
| `SERWT` | the transmitter is idle and empty | nothing (wait for the end of the tail) |

`SERTX SERTXC SERRX SERWT` have timeout forms (`SERTXT SERTXCT SERRXT
SERWTT`) that work like every other wait: `SETD k`, the timed form, then
`BCS handler`; `C = 0` on success, `C = 1` when the deadline came first.
`SERI`/`SERIC` take the byte from the instruction word, so a table of
constant bytes (a descriptor, a MAC header) costs one word per byte instead
of two; the MISC major has no timeout bit, so they have no timed form.

Configuration byte (`SERCFG`): bits 1:0 mode (0 off, 1 NRZI, 2 Manchester),
bit 2 bit stuffing, bit 3 CRC-32 instead of CRC-16, bit 4 receiver enable,
bit 5 leave the first received byte out of the CRC (the USB PID), bits 7:6
pin pair k. It fits one `LDI`.

Status word (`SERST`, and `SERRX` at a frame end): bit 0 holding register
full, 1 transmitter busy, 2 received byte available, 3 receiver in a frame,
4 frame ended, 5 CRC-5 good, 6 CRC-16/32 good, 7 overrun, 8 stuffing error,
9 frame did not end on a byte boundary.

A USB low-speed device answering a token, in outline:

```
        ldi   r0, 0x35          ; NRZI, stuffing, CRC-16, receiver on, skip PID, pair 0
        ldi   r1, 32            ; 48 MHz / 1.5 Mbit/s
        sett  r1
        sercfg r0
wait:   serrx r2                ; PID (the engine has consumed SYNC)
        beq   wait              ; a frame end with no byte: start over
        ...                     ; SERRX the two token bytes, compare the address
        serrx r3                ; frame end: Z = 1, r3 = status, bit 5 = CRC-5 good
        setd  0
        waitd 3                 ; the inter-packet gap, in bit times
        seri  0x80              ; SYNC
        seri  0x4B              ; DATA1
        seric 0x12              ; ... payload bytes, in the CRC
        serwt                   ; CRC-16, EOP and release are the engine's
```

Rules that follow from the design:

- The transmitter and the receiver share the pair, the CRC register and the
  owner's timer, so the engine is **half duplex**: while the transmitter is
  busy the receiver is held in its hunt state. USB is half duplex; 10BASE-T
  transmit never receives on the same pair.
- A frame ends when firmware is late. Each byte must be queued before the
  previous one has left the shift register: 8 symbol times in NRZI mode
  (256 cycles for USB at 48 MHz), 16 in Manchester mode (32 cycles, 16
  slots, at 40 MHz). The blocking `SERTX` paces a loop by itself.
- The engine writes the pair's `uio_out`/`uio_oe` registers the way the
  replay engine does, so the pin unit stays the one place that drives pads
  and the open-drain invariant holds (a pin in open-drain mode ignores the
  engine). When the engine is idle the pins belong to firmware again: link
  pulses, a USB reset check or a pull-up enable are ordinary pin
  instructions.
- A host soft reset of a thread does not touch the engine; `SERCFG` resets
  it.

## 4. Shared or per-thread: shared

One engine, used by either thread (the capture and replay engine set the
precedent). Estimate from the register list of SEMANTICS 15: about 130
flops (6,400 um^2) and 500 to 700 gates, roughly 12,000 to 13,000 um^2,
which takes utilisation from 24.8% to about 26.5%; a second engine would
add the same again (about 28.5%). Both are far below the 40% limit, so
area alone does not decide it. What decides it:

- **Timing.** Each engine adds a 16-bit source to the register write-data
  selection and three writers to the pin registers. The write-data path is
  the one that had +0.76 ns at the slow corner after `waitd-csa`; one more
  source is a risk worth taking, two are not.
- **Need.** USB uses one bus and Ethernet transmit one pair. No planned
  protocol uses two coded lines at once.
- **Verification.** One engine is one set of properties and one set of
  lockstep tests; the second would be a copy that only differs in its
  owner.

The numbers above are estimates until the first hardening run of the
branch; docs/AREA.md gets the measured ones. If the run misses the 20 ns
period at the slow corner, the fallback is to register the engine's read
data (status and received byte) one slot earlier, which changes no rule of
SEMANTICS 15 because all of it is already registered state.

## 5. The system clock each protocol needs

The hardened design is signed off at 20 ns, so the clock may be anything up
to 50 MHz. The symbol period is a whole number of cycles, so the clock must
be a multiple of the symbol rate:

| Protocol | Symbol rate | Clock | `period` | Why |
|---|---|---|---|---|
| USB low speed | 1.5 Mbit/s | **48 MHz** | 32 | The largest multiple of 1.5 MHz under 50 MHz: 16 slots per bit per thread, the most firmware time per byte, and a sample point 16 cycles from each edge. 24 and 12 MHz also work (`period` 16, 8). 50 MHz does not: 33.3 cycles per bit is a 1% rate error at best and the host's tolerance for a low-speed device is 1.5% with nothing left for the crystal. |
| 10BASE-T | 20 M half-bits/s | **40 MHz** | 2 | Manchester half-bits are 50 ns: 2.5 cycles at 50 MHz, which no divider makes. 40 MHz gives two cycles per half-bit and 16 slots per byte per thread; 20 MHz (`period` 1) also works with 8 slots per byte. |

60 MHz would serve both from one clock (40 cycles per USB bit, 3 per
half-bit; PLAN.md's original target), but it is a 16.7 ns period: the
design meets that at the typical corner only, and the rule for this step
is to keep 20 ns clean at the slow corner. So the two protocols run from
two clock settings, both inside the signed-off 50 MHz. That costs nothing
on the Tiny Tapeout demo board, whose RP2350 generates the project clock
at a programmable frequency, and a board runs one of the two firmware
images at a time anyway.

The receiver's Manchester decoder is in the hardware (it is half of the
round-trip proof and is usable at lower rates), but at 40 MHz it has four
samples per bit, which is not a 10BASE-T receiver; receive stays out of
scope for the Ethernet firmware.

## 6. Verification plan

- Golden model and RTL written from SEMANTICS 15 independently (CLAUDE.md
  independence rule); mismatches are settled by quoting SEMANTICS and
  logged in BUGS.md.
- Lockstep: every register of the engine is compared every cycle
  (SEMANTICS 13 lists the names).
- Formal (`formal/ser.sby`): the stuffer never emits seven consecutive
  ones; the CRC registers equal a reference computation for a bounded
  message; a transmitter instance feeding a receiver instance returns the
  bytes that were sent, in both modes; the pin unit's open-drain invariant
  with the engine writing.
- Protocol-only Python models for the firmware of step 3: a USB host and a
  Manchester decoder that know nothing about the engine.
- `src/keyer_ser.v` is in the mutation tool's target list.

## 7. Rejected

- **Per-thread engines**: section 4.
- **A private bit-rate divider**: 16 more flops and a second notion of
  time; with the timer as the source, timeouts and inter-packet gaps are in
  the same unit and on the same grid as the bits.
- **Full duplex** (separate transmit and receive CRC and counters): about
  45 more flops for no planned use.
- **A frame-length register or an end-of-frame instruction**: underrun as
  the end of frame needs neither, and a late loop ends the frame visibly
  instead of repeating a stale byte.
- **Transmitting CRC-5**: tokens are sent by hosts, and a host's tokens are
  constants of its program; the 5-bit register checks received tokens.
- **Firmware-built end of packet**: at two cycles per half-bit no
  instruction can place it, and for USB it must follow the last bit
  exactly.
- **A raw NRZ mode, MSB-first shifting, selectable sync patterns**: not
  needed by either protocol; `REV8` reverses a byte when an MSB-first user
  appears.
