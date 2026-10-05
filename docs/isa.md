# Keyer ISA, version 0.3 (draft, not yet frozen)

Keyer is a two-thread, 16-bit, pin-oriented processor. This document is the
programmer's reference. The cycle-exact contract between the instruction-set
simulator (ISS) and the RTL is `docs/SEMANTICS.md`, which wins where the two
disagree. The encoding table in `tools/keyer_isa.py` is the single source of
truth for opcodes; `src/keyer_isa.vh` is generated from it.

## 1. Machine model

- Two hardware threads, T0 and T1. The core alternates: T0 executes on even
  core clock cycles, T1 on odd. Each thread therefore runs one instruction per
  two core clocks. One such opportunity is called a **slot**.
- Every instruction completes in exactly one slot, except the **blocking**
  instructions (`WT0 WT1 WTR WTF POP PUSH`, their timeout forms, `WAITD` and
  `DELAY`), which hold the thread at the same PC, re-evaluating every slot,
  until their condition is met.
  There are no stalls, caches or interrupts. Taken branches cost nothing extra.
- Per thread: eight 16-bit registers `r0`-`r7`, program counter `PC`, link
  register `LR`, flags `Z` and `C`, a tick timer with `NOW` and `DEADLINE`
  registers, an **inbox** FIFO (host to thread, 8-bit) and an **outbox** FIFO
  (thread to host, 8-bit).
- Shared: 256 x 16-bit program memory (`IMEM`), the 24-pin space, a 16-bit
  free-running cycle counter `CYC`, and the per-pin mode (push-pull or
  open-drain) for the bidirectional pins.
- Reset: all registers 0, flags 0, both threads stopped with PC = 0, timers
  disabled, FIFOs empty, all bidirectional pins in input (OE = 0) push-pull
  mode, all outputs 0.

### Pin space

| Pin index | Pad | Readable | Writable | Notes |
|---|---|---|---|---|
| 0-7 | `uio[0..7]` | yes (pad level) | yes | Bidirectional. Per-pin mode: push-pull (PP, default) or open-drain (OD). |
| 8-15 | `ui[0..7]` | yes | no (writes ignored) | `ui[0..2]` carry the host SPI (SCK, MOSI, CS_n); firmware may still read them. |
| 16-23 | `uo[0..7]` | reads driven value | yes | `uo[0]` (MISO) and `uo[1]` (IRQ) belong to the host interface; firmware writes to them are ignored. |

Pin writes. `SET p` / `CLR p` (and the register forms) perform
`pinwrite(p, v)`:

- PP mode: `uio_out[p] <= v`. Direction is separate: `OEN p` drives, `OEF p` tri-states.
- OD mode: `v = 0` drives the pad low (`uio_oe = 1, uio_out = 0`); `v = 1` releases it (`uio_oe = 0`). `uio_out` is forced to 0 in OD mode, so the pad is never driven high. `OEN`/`OEF` are ignored in OD mode.

Pin reads return the pad level after a two-flop synchroniser (two core cycles
of latency). For pins 16-23 the read value is the driven output register.
Both threads may write any pin; the later write wins. A pin write becomes
visible on the pad on the clock edge that ends the instruction's slot.

Edges. The pin unit keeps the level from two core cycles ago. A **rising edge
at pin p** is observed at a slot when `level[p] == 1` and the level two cycles
earlier was 0 (falling: the reverse). Each edge whose new level lasts at least
two cycles is observed by exactly one slot of each thread.

### Timer (one per thread)

Registers `period`, `prescale`, `NOW` and `DEADLINE`, all 16-bit. Every
`period` core cycles the timer **ticks**: `NOW` increments by one. `period = 0`
disables the timer (`NOW` is frozen).

- `SETT rs` restarts the timer: `period = rs`, `NOW = DEADLINE = 0`; the first
  tick is `rs` cycles after the instruction, then every `rs` cycles.
- `SETD k` sets `DEADLINE = NOW + k` (k = 0..255 ticks from now).
- `WAITD k` blocks until `NOW` has reached `DEADLINE + k`, then advances
  `DEADLINE` by k. One `WAITD 1` per bit puts every bit edge on the tick grid
  no matter how many instructions the loop has. A loop that falls behind
  completes its `WAITD` immediately and `DEADLINE` still advances, so later
  deadlines keep their phase and no tick is lost; `WAITD k` waits k periods
  in one instruction.
- "Deadline reached" means `NOW - DEADLINE`, as a signed 16-bit value, is
  zero or positive. `RDT rd` reads that difference, `BDR` branches on it and
  status bit 4 reports it. Every blocking instruction has a **timeout form**
  (`WT0T WT1T WTRT WTFT POPT PUSHT`) that also completes when the deadline is
  reached and reports `C = 0` on success, `C = 1` on timeout; the usual
  pattern is `SETD k`, the timed wait, then `BCS handler`.

Exact cycle rules and a worked example: `docs/SEMANTICS.md` section 6.

### FIFOs

Inbox and outbox are 16 entries of 8 bits. `POP rd` blocks while the inbox is
empty; `PUSH rs` blocks while the outbox is full. `POPNB`/`PUSHNB` are the
non-blocking forms and report success in `C`.

### Status word (`RDS rd`)

| bit | meaning |
|---|---|
| 0 | inbox empty |
| 1 | inbox full |
| 2 | outbox empty |
| 3 | outbox full |
| 4 | deadline reached |
| 5 | other thread running |
| 6 | this thread's id (0 or 1) |
| 7 | capture active |
| 8 | replay active |
| 15:9 | 0 |

## 2. Flags

`Z` is set when the 16-bit result is zero. `C` is defined per instruction:
carry out for `ADD ADC INC ADDI`; borrow (`a < b` unsigned) for
`SUB SBC CMP CMPI DEC`; the bit shifted out for `SHL SHR RCL RCR`;
`rd != 0` for `NEG`. Instructions marked `Z` leave `C` unchanged; instructions
marked `-` leave both flags unchanged.

## 3. Encoding

All instructions are 16 bits. `[15:12]` is the major opcode. Field letters:
`d` = destination register, `s` = source register, `i` = immediate, `o` =
signed branch offset (relative to the address of the next instruction),
`p` = pin index (0-23), `a` = absolute address, `f` = sub-opcode, `c` =
condition, `l` = level, `t` = timeout bit, `x` = must be 0.

The register field is always at `[11:9]` (a second register, when present, at
`[8:6]`), so the decoder extracts register indices from one place.

| Major | Format | Fields |
|---|---|---|
| 0 ALU2 | `0000 ddd sss ffff xx` | rd op= rs |
| 1 ALU1 | `0001 ddd ffff ooooo` | rd = op rd (o used by DJNZ only) |
| 2 ADDI | `0010 ddd x iiiiiiii` | rd += sext(i) |
| 3 ANDI | `0011 ddd x iiiiiiii` | rd &= zext(i) |
| 4 ORI | `0100 ddd x iiiiiiii` | rd \|= zext(i) |
| 5 XORI | `0101 ddd x iiiiiiii` | rd ^= zext(i) |
| 6 LDI | `0110 ddd x iiiiiiii` | rd = zext(i) |
| 7 LDIH | `0111 ddd x iiiiiiii` | rd[15:8] = i |
| 8 CMPI | `1000 sss x iiiiiiii` | flags(rs - zext(i)) |
| 9 Bcc | `1001 ccc x oooooooo` | if cond: PC = PC + 1 + sext(o) |
| A BPIN | `1010 l ppppp oooooo` | if level[p] == l: PC = PC + 1 + sext(o) |
| B JMP/CALL | `1011 c aaaaaaaaaaa` | c=0: PC = a. c=1: LR = PC + 1; PC = a |
| C PIN | `1100 ffff t xx ppppp` | pin op on p; t = timeout form of a wait |
| D PINR | `1101 rrr fff x ppppp` | pin op with register |
| E XFER | `1110 rrr ffff t gggg` | register <-> unit; t = timeout form of PUSH/POP and the serializer waits; g = serializer function when f = 15, otherwise must be 0 |
| F MISC | `1111 ffff iiiiiiii` | control |

## 4. Instruction list

Column "slots" is 1 unless blocking. Column "flags" lists the flags written.

### ALU2 (major 0), `rd op= rs`

| f | mnemonic | operation | flags |
|---|---|---|---|
| 0 | `ADD rd, rs` | rd = rd + rs | Z C |
| 1 | `SUB rd, rs` | rd = rd - rs | Z C |
| 2 | `AND rd, rs` | rd = rd & rs | Z |
| 3 | `OR rd, rs` | rd = rd \| rs | Z |
| 4 | `XOR rd, rs` | rd = rd ^ rs | Z |
| 5 | `MOV rd, rs` | rd = rs | Z |
| 6 | `CMP rd, rs` | flags(rd - rs), rd unchanged | Z C |
| 7 | `TST rd, rs` | flags(rd & rs), rd unchanged | Z |
| 8 | `ADC rd, rs` | rd = rd + rs + C | Z C |
| 9 | `SBC rd, rs` | rd = rd - rs - C | Z C |

### ALU1 (major 1), `rd = op rd`

| f | mnemonic | operation | flags |
|---|---|---|---|
| 0 | `SHL rd` | C = rd[15]; rd = rd << 1 | Z C |
| 1 | `SHR rd` | C = rd[0]; rd = rd >> 1 | Z C |
| 2 | `RCL rd` | {C, rd} = {rd, C} (rotate left through carry) | Z C |
| 3 | `RCR rd` | {rd, C} = {C, rd} (rotate right through carry) | Z C |
| 4 | `NOT rd` | rd = ~rd | Z |
| 5 | `NEG rd` | rd = -rd | Z C |
| 6 | `INC rd` | rd = rd + 1 | Z C |
| 7 | `DEC rd` | rd = rd - 1 | Z C |
| 8 | `SWAP rd` | rd = {rd[7:0], rd[15:8]} | Z |
| 9 | `REV8 rd` | rd[7:0] = bit-reverse(rd[7:0]), high byte unchanged | Z |
| 10 | `DJNZ rd, off` | rd = rd - 1; if rd != 0: PC = PC + 1 + sext(off5). Flags unchanged. Offset range -16..+15 | - |

Note on the carry flag: `INC`, `DEC`, `ADDI` and the other arithmetic
instructions write `C`. In a loop that carries the next bit in `C` for `WRC`,
shift the bit into `C` immediately before `WRC`, or count the loop with
`DJNZ`, which leaves the flags alone.

### Immediate (majors 2-8)

| mnemonic | operation | flags |
|---|---|---|
| `ADDI rd, simm8` | rd = rd + sext(imm) | Z C |
| `ANDI rd, imm8` | rd = rd & zext(imm) | Z |
| `ORI rd, imm8` | rd = rd \| zext(imm) | Z |
| `XORI rd, imm8` | rd = rd ^ zext(imm) | Z |
| `LDI rd, imm8` | rd = zext(imm) | - |
| `LDIH rd, imm8` | rd[15:8] = imm | - |
| `CMPI rs, imm8` | flags(rs - zext(imm)) | Z C |

The assembler accepts `LDW rd, imm16` and expands it to `LDI` + `LDIH`.

### Branches (majors 9, A, B)

| mnemonic | condition |
|---|---|
| `BRA off` | always |
| `BEQ off` / `BZ` | Z = 1 |
| `BNE off` / `BNZ` | Z = 0 |
| `BCS off` / `BLO` | C = 1 |
| `BCC off` / `BHS` | C = 0 |
| `BFE off` | inbox empty |
| `BFNE off` | inbox not empty |
| `BDR off` | deadline reached (`NOW - DEADLINE >= 0`, signed) |
| `BP0 p, off` | level[p] = 0 (offset range -32..+31) |
| `BP1 p, off` | level[p] = 1 (offset range -32..+31) |
| `JMP addr` | PC = addr |
| `CALL addr` | LR = PC + 1; PC = addr |

Branch offsets are in words relative to the following instruction; the
assembler computes them from labels. `Bcc` reaches -128..+127, `BPn` reaches
-32..+31. `JMP`/`CALL` take an 11-bit absolute address (256 words used).
Flags are not modified by branches. `BP0`/`BP1` observe the synchronised pin
level.

### Pin (major C), operand is an immediate pin index

| f | mnemonic | operation | slots |
|---|---|---|---|
| 0 | `SET p` | pinwrite(p, 1) | 1 |
| 1 | `CLR p` | pinwrite(p, 0) | 1 |
| 2 | `OEN p` | PP mode: uio_oe[p] = 1 (drive) | 1 |
| 3 | `OEF p` | PP mode: uio_oe[p] = 0 (tri-state) | 1 |
| 4 | `OD p` | pin mode = open-drain (also releases the pin) | 1 |
| 5 | `PP p` | pin mode = push-pull | 1 |
| 6 | `WT0 p` | block until level[p] = 0 | blocking |
| 7 | `WT1 p` | block until level[p] = 1 | blocking |
| 8 | `WTR p` | block until a rising edge at p is observed | blocking |
| 9 | `WTF p` | block until a falling edge at p is observed | blocking |
| 6-9, t=1 | `WT0T WT1T WTRT WTFT p` | as above, or until the deadline is reached; C = 0 if the pin condition held, C = 1 on timeout | blocking |
| 10 | `WRC p` | pinwrite(p, C) | 1 |
| 11 | `RDC p` | C = level[p] | 1 |
| 12 | `TSTP p` | Z = (level[p] == 0) | 1 |

### Pin with register (major D)

| f | mnemonic | operation |
|---|---|---|
| 0 | `OUTR p, rs` | pinwrite(p, rs[0]) |
| 1 | `INR rd, p` | rd = level[p] (0 or 1); Z set |

### Transfer (major E)

| f | mnemonic | operation | flags | slots |
|---|---|---|---|---|
| 0 | `PUSH rs` | outbox <- rs[7:0] | - | blocking (outbox full) |
| 1 | `POP rd` | rd = zext(inbox byte) | - | blocking (inbox empty) |
| 0, t=1 | `PUSHT rs` | push, or give up when the deadline is reached; C = 0 pushed, C = 1 timeout | C | blocking |
| 1, t=1 | `POPT rd` | pop, or give up when the deadline is reached; C = 0 popped, C = 1 timeout (rd unchanged) | C | blocking |
| 2 | `RDS rd` | rd = status word | - | 1 |
| 3 | `RDCYC rd` | rd = CYC | - | 1 |
| 4 | `SETT rs` | timer period = rs (0 disables); restart, NOW = DEADLINE = 0 | - | 1 |
| 5 | `RDT rd` | rd = NOW - DEADLINE (signed; >= 0 means reached) | - | 1 |
| 6 | `PUSHNB rs` | if outbox not full: push, C = 1; else C = 0 | C | 1 |
| 7 | `POPNB rd` | if inbox not empty: rd = byte, C = 1; else C = 0, rd unchanged | C | 1 |
| 8 | `OUTB rs` | pinwrite(i, rs[i]) for i in 0..7 (all uio pins at once) | - | 1 |
| 9 | `INB rd` | rd = zext(level[7:0]) | Z | 1 |
| 10 | `INW rd` | rd = {level[15:8], level[7:0]} (ui then uio) | Z | 1 |
| 11 | `OUTOE rs` | PP-mode pins: uio_oe[i] = rs[i] | - | 1 |
| 12 | `RDLR rd` | rd = LR (save the return address before a nested CALL) | - | 1 |
| 13 | `JMPR rs` | PC = rs (return through a saved LR, or a jump table) | - | 1 |
| 14 | `CAPC rs` | capture/replay control: rs[0] arm capture, rs[1] disarm, rs[2] start replay, rs[3] stop replay (docs/CAPTURE.md) | - | 1 |
| 15, g=0 | `SERCFG rs` | serializer configuration = rs[7:0], engine reset, this thread's timer sets the symbol period (docs/SERIALIZER.md) | - | 1 |
| 15, g=1 | `SERTX rs` | queue rs[7:0] for transmission, not in the CRC | - | blocking (holding register full) |
| 15, g=2 | `SERTXC rs` | queue rs[7:0] for transmission, in the CRC | - | blocking (holding register full) |
| 15, g=3 | `SERRX rd` | rd = received byte, Z = 0; or at a frame end rd = serializer status, Z = 1 | Z | blocking (no byte and no frame end) |
| 15, g=4 | `SERST rd` | rd = serializer status | - | 1 |
| 15, g=5 | `SERWT` | wait until the transmitter is idle and its holding register empty | - | blocking |
| 15, g=1,2,3,5, t=1 | `SERTXT rs` `SERTXCT rs` `SERRXT rd` `SERWTT` | as above, or give up when the deadline is reached; C = 0 done, C = 1 timeout | C (and Z for `SERRXT`) | blocking |

### Misc (major F)

| f | mnemonic | operation | slots |
|---|---|---|---|
| 0 | `NOP` | nothing | 1 |
| 1 | `HALT` | thread stops; `halted` status set; host may restart | 1 |
| 2 | `RET` | PC = LR | 1 |
| 3 | `WAITD k` | block until NOW has reached DEADLINE + k, then DEADLINE += k (k = 0..255) | blocking |
| 4 | `DELAY n` | occupy 1 + n slots (n = 0..255) | 1 + n |
| 5 | `SETC` | C = 1 | 1 |
| 6 | `CLC` | C = 0 | 1 |
| 7 | `START` | start the other thread at its current PC | 1 |
| 8 | `STOP` | stop the other thread | 1 |
| 9 | `SETD k` | DEADLINE = NOW + k (k = 0..255) | 1 |
| 10 | `SERI n` | `SERTX` with the byte n (0..255) from the instruction word | blocking (holding register full) |
| 11 | `SERIC n` | `SERTXC` with the byte n | blocking (holding register full) |

## 5. Timing recipes

UART transmit at B baud with core clock F: `period = F / B` (for 115200 at
60 MHz, 521; error 0.03%). `SETT` once per byte, then per bit: `WRC tx` then
`WAITD 1`. The bit edge lands on the timer tick regardless of how many
instructions prepared the next bit.

UART receive: `WTF rx` (start bit), `SETT` with 1.5 bit periods, `WAITD 1`,
then `SETT` with 1 period and sample 8 times with `RDC rx` + `RCR r0` +
`WAITD 1`.

Timeout: `SETT` a coarse period (say 60000 cycles), `SETD 25`, then `WT1T scl`
and `BCS handler`: gives up after 25 ticks (25 ms at 60 MHz) if the slave never
releases the clock. `fw/i2c_master.s` does this around every clock-stretch
wait.

Fastest software bit rate per thread is one bit every two slots (`WRC` +
`WAITD 1`), i.e. F / 4 = 15 Mbit/s at 60 MHz, with the edge timing set by the
timer rather than the loop.

## 5a. The serializer

One shared engine does the bit level of coded serial lines: a shift
register clocked by the owning thread's timer, NRZI or Manchester coding,
bit stuffing, CRC-5, CRC-16 and CRC-32, SE0/J/K on the pin pair `uio[2k]`,
`uio[2k+1]`, and framing status. `docs/SERIALIZER.md` is the programmer's
guide (configuration byte, status word, an example); the cycle-exact rules
are `docs/SEMANTICS.md` section 15. In short: `SETT` the symbol period,
`SERCFG` once, then `SERTX`/`SERI` bytes outside the CRC and
`SERTXC`/`SERIC` bytes inside it; the frame, its CRC and its end of packet
are sent when the bytes stop coming. `SERRX` returns received bytes and
then, with `Z = 1`, the status of the frame that ended.

## 6. Host interface

SPI slave, mode 0 (CPOL = 0, CPHA = 0), MSB first, CS_n framed. Pins:
`ui[0]` = SCK, `ui[1]` = MOSI, `ui[2]` = CS_n, `uo[0]` = MISO, `uo[1]` = IRQ.
SCK must be at most core clock / 8 (each half period at least 4 cycles);
CS_n high for at least 4 cycles between transactions, and from two cycles
before `rst_n` rises (no transaction across the release of reset). MISO is
0 except while a read data byte is shifted out. Exact timing of when a
write takes effect: `docs/SEMANTICS.md` section 10.

A transaction is a command byte followed by data bytes until CS_n rises.
Command: bit 7 = 1 for write, 0 for read; bits 6:0 = register. Multi-byte
values are little-endian. For a read, the first data byte is shifted out
during the byte following the command.

| Reg | Name | Write | Read |
|---|---|---|---|
| 0x00 | CTRL | bit0 RUN0, bit1 RUN1, bit2 RST0, bit3 RST1 (RSTn clears thread n's timer, flags, LR and FIFOs; PC unchanged) | RUN bits |
| 0x01 | STAT | - | bit0 RUN0, bit1 RUN1, bit2 HALTED0, bit3 HALTED1, bit4 BLOCKED0, bit5 BLOCKED1 |
| 0x02 | PC0 | 2 bytes (low byte used); accepted only while T0 stopped | 2 bytes: PC, 0 |
| 0x03 | PC1 | same for T1 | |
| 0x04 | IMEM_ADDR | 2 bytes (low byte used) | 2 bytes: address, 0 |
| 0x05 | IMEM_DATA | stream of 16-bit words, address auto-increments; accepted only while both threads are stopped | stream, same rule |
| 0x06 | INBOX0 | stream of bytes into T0's inbox | - |
| 0x07 | OUTBOX0 | - | stream of bytes from T0's outbox (one pop per byte clocked out) |
| 0x08 | INBOX1 | stream into T1's inbox | - |
| 0x09 | OUTBOX1 | - | stream from T1's outbox |
| 0x0A | LEVELS | - | 4 bytes: inbox0, outbox0, inbox1, outbox1 counts |
| 0x0B | PINMODE | OD mask for uio[7:0] | current mask |
| 0x0C | IRQEN | bit0 outbox0 non-empty, bit1 outbox1 non-empty, bit2 HALTED0, bit3 HALTED1, bit4 inbox0 empty, bit5 inbox1 empty, bit6 capture done, bit7 replay done | mask |
| 0x0D | PINS | - | 3 bytes: uio pad levels, ui levels, uo driven values; a fourth byte reads 0 |
| 0x0E | FIFOCLR | bit0 inbox0, bit1 outbox0, bit2 inbox1, bit3 outbox1 | - |
| 0x0F | ID | - | 2 bytes: 0x4B ('K'), ISA version |
| 0x10 | PINOUT | - | 2 bytes: uio_out, uio_oe |
| 0x11 | CR_CTRL | bit0 ARM capture, bit1 DISARM, bit2 START replay, bit3 STOP replay (actions) | status: bit0 capture active, bit1 triggered, bit2 capture done, bit3 overflow, bit4 replay active, bit5 replay done, bit6 underrun |
| 0x12 | CAP_CFG | byte 0: bits 2:0 pin group (4g..4g+3), bits 7:4 watch mask; byte 1: bits 3:0 trigger pattern, bits 7:4 trigger mask (0 = trigger at once) | same |
| 0x13 | CAP_BUF | base word, length in entries (0 = none) | same |
| 0x14 | REP_CFG | bits 2:0 pin group, bits 7:4 drive mask | same |
| 0x15 | REP_BUF | base word, length in entries | same |
| 0x16 | CR_COUNT | - | 2 bytes: entries recorded, entries applied |

IRQ (`uo[1]`) is the OR of the enabled conditions.

Capture and replay (entries `{delta[11:0], pins[3:0]}` in program memory,
trigger, overflow and underrun rules): `docs/CAPTURE.md` and
`docs/SEMANTICS.md` section 14.

## 7. Open questions before freezing v1.0

- 256 vs 512 words of IMEM (the 512 x 16 macro is 45,300 um^2). The encoding already allows 11-bit addresses.
- Whether `DELAY` should count core cycles instead of slots.
- A `WTA mask` (wait for any change on a pin mask) instruction for the capture feature.
- XFER sub-opcode 15 now holds the serializer (function codes 6 to 15 free); MISC sub-opcodes 12 to 15 are free.
- Side-set style "write pin and wait" fusion, if the UART/SPI loops turn out to need it.
