# Keyer semantics: the cycle-exact contract

Version 0.2, 2026-10-02. This document is the reference for the golden model
(`tools/keyersim.py`) and the RTL (`src/`). Where it disagrees with
`docs/isa.md`, this document wins; `docs/isa.md` is the programmer's
reference and is kept consistent with it. The encodings are in
`tools/keyer_isa.py` (generated header `src/keyer_isa.vh`); this document
never restates an opcode.

Everything here is stated in terms of core clock cycles. Rules of the form
"at the end of cycle c" mean "registered on the clock edge that ends cycle c,
and therefore observable from cycle c + 1 on".

## 1. Conventions

- **Cycle c.** Cycle 0 is the first clock cycle in which `rst_n` is high.
  During cycle c the free-running counter `CYC` reads c mod 65536.
- **Slot.** Thread 0 owns the even cycles, thread 1 the odd ones. A thread's
  slots are two cycles apart. "Slot c of thread t" means cycle c with
  c mod 2 = t.
- **Observe, then commit.** An instruction evaluated at slot c observes every
  piece of state *as it is during cycle c* (registered values, including
  `level(c)`, FIFO occupancy, `NOW(c)`, the other thread's running flag) and
  every effect it has is registered at the end of cycle c. Nothing an
  instruction does is visible during its own cycle.
- **Arithmetic** is on 16-bit two's-complement values unless stated.
  `reached(a, b)` means `((a - b) mod 65536) < 32768`, i.e. the signed
  16-bit difference a - b is zero or positive.
- **Notation.** `R[t][i]` register i of thread t; `PC[t]`, `LR[t]`, `Z[t]`,
  `C[t]`; `IMEM[a]` for a in 0..255; `level(c)` the 24-bit synchronised pin
  level vector during cycle c (section 5); `x(c)` the value of state x
  during cycle c.

## 2. Execution model

### 2.1 Fetch and execute

- During cycle c the memory port presents `IMEM[PC[t](c)]` for the thread
  t = c mod 2, fetched in cycle c - 1 (the memory has a one-cycle synchronous
  read). Thread t **executes** at slot c if and only if `running[t](c)` is 1
  and the fetch was valid.
- The fetch is valid unless the host used the memory port in cycle c - 1, and
  it is invalid in cycle 0. The host may use the port only while both threads
  are stopped (section 10.4), so a running thread's fetch is always valid;
  the model may therefore equate "executes" with "running".
- Exactly one instruction is evaluated per cycle, for the thread that owns the
  slot. There is no pipeline hazard: a thread's next slot is two cycles after
  its last, and every effect of a slot is registered at its end.

### 2.2 Completion and blocking

- Every instruction either **completes** in the slot in which it is evaluated
  or **blocks**. A blocking instruction (section 7) that does not complete
  leaves all architectural state of its thread unchanged (PC, registers,
  flags, LR, timer, FIFOs, pins) and is evaluated again, from scratch, at the
  thread's next slot. The only exception is `DELAY`, which keeps a private
  count (section 7.6).
- When an instruction completes at slot c: `PC[t] <= next`, where `next` is
  `PC + 1 mod 256` unless the instruction names another target; register,
  flag, LR, pin, FIFO and timer effects are registered at the end of c.
- `blocked[t]` (host STAT bits 4 and 5) is rewritten at every slot of t: it
  becomes 1 if t executed in that slot and did not complete, otherwise 0.
- An undefined encoding (a sub-opcode not in the table) completes with no
  effect other than `PC + 1`. This is not guaranteed for future versions.

### 2.3 Running, halted, stopped

- `running[t]` becomes 1 at the end of the cycle in which the host writes a
  RUN bit of 1 for t (section 10) or the other thread commits `START`.
  `halted[t]` is cleared by the same events.
- `running[t]` becomes 0 at the end of the cycle in which t commits `HALT`
  (which also sets `halted[t]`), the other thread commits `STOP`, or the host
  writes a RUN bit of 0 for t. Any of these also clears the `DELAY` count of
  t.
- If a host RUN write and a core-initiated change land in the same cycle, the
  host's value wins for every thread (a RUN write names both threads).
- A thread that is not running does nothing at its slots and holds all its
  state. When restarted it resumes at `PC[t]`, re-evaluating whatever
  instruction is there, including a blocking one it was stopped in.

### 2.4 Program counter and control flow

- Program memory is 256 words; every address is taken mod 256. Branch
  targets are `PC + 1 + offset` (offset sign-extended), `JMP`/`CALL` use the
  low 8 bits of their 11-bit field, `JMPR`/`RET` use the low 8 bits of the
  register / `LR`.
- `CALL a` registers `LR <= PC + 1` and `PC <= a` in the same cycle.
- Taken and not-taken branches both cost one slot.

## 3. Reset state

### 3.1 Hard reset (`rst_n` low)

Every register is 0: `CYC`, both threads' `R`, `PC`, `LR`, `Z`, `C`, timer
state (`period`, `prescale`, `NOW`, `DEADLINE`), `DELAY` counts; `running`,
`halted`, `blocked` are 0; all four FIFOs are empty; `uio_out = uio_oe =
od_mask = 0` (every bidirectional pin is a push-pull input); firmware
outputs `uo[2..7]` are 0; `IRQEN` is 0; the host SPI state machine is idle.
Program memory contents are preserved across reset in silicon; the
behavioural model and the simulators read unwritten words as 0.

### 3.2 Host soft reset (CTRL bit RST0 / RST1)

For the named thread, at the end of the cycle in which the pulse is applied
(section 10.3): `LR`, `Z`, `C`, `period`, `prescale`, `NOW`, `DEADLINE` and
the `DELAY` count become 0, and the thread's inbox and outbox are emptied.
`PC`, `R[0..7]`, `running`, `halted` are unchanged. Pins are unchanged.

## 4. Registers and flags

- Eight 16-bit registers per thread. A write and a read of the same register
  by one instruction see the old value (reads observe, writes commit).
- `Z` is written by the instructions marked Z in `docs/isa.md` and is 1 when
  the 16-bit result is 0.
- `C` is: the carry out of bit 15 for `ADD ADC INC ADDI`; the borrow
  (`a < b` unsigned, including the borrow-in for `SBC`) for `SUB SBC CMP CMPI
  DEC`; the bit shifted out for `SHL SHR RCL RCR`; `rd != 0` for `NEG`; the
  success or timeout code for the instructions in sections 7.4 and 8.3; the
  pin level for `RDC`; 1 for `SETC`, 0 for `CLC`.
- Instructions not marked with a flag leave it unchanged. `DJNZ` and all
  branches leave both flags unchanged.

## 5. Pin unit

### 5.1 Pad vector

The 24 firmware pins map to the pads as in `docs/isa.md`: 0-7 `uio`, 8-15
`ui`, 16-23 `uo`. The pad level during cycle c is `pad(c)`: for `uio[i]` it is
the driven value if `uio_oe[i](c)` is 1, otherwise whatever the outside world
drives (pull-ups in the tests); for `ui` it is the input; for `uo` it is
`uo_out(c)`.

### 5.2 Synchroniser and level

- For pins 0-15, `level(c)[p] = pad(c - 2)[p]`: a two-flop synchroniser, two
  cycles of latency, no more and no less. After reset `level` reads 0 for
  cycles 0 and 1 regardless of the pads.
- For pins 16-23, `level(c)[p] = uo_out(c)[p - 16]`, the value being driven
  during cycle c (zero latency). Pins 16 and 17 (MISO, IRQ) always read 0.
- `level2(c)[p] = level(c - 2)[p]` for all 24 pins (0 for cycles 0 to 3).
- Every pin read (`RDC TSTP INR INB INW BP0 BP1 WT0 WT1`) observes `level(c)`.
  A **rising edge** is observed at slot c when `level(c)[p] = 1` and
  `level2(c)[p] = 0`, a **falling edge** when the reverse holds. Because a
  thread's slots are two cycles apart, consecutive slots of one thread see
  consecutive samples, so a pad change that lasts at least two cycles is
  observed as an edge by exactly one slot of each thread.

### 5.3 Drive registers and `pinwrite`

The drive state is `uio_out[7:0]`, `uio_oe[7:0]`, `od_mask[7:0]`, `uo_out[7:2]`.
A pin instruction committed at slot c changes them at the end of c, so the
pad shows the new value during cycle c + 1, `level` shows it from cycle c + 3
(pins 0-7) or c + 1 (pins 18-23).

`pinwrite(p, v)` is what `SET` (v = 1), `CLR` (v = 0), `WRC` (v = C),
`OUTR` (v = rs[0]) and `OUTB` (per bit) do:

| p | mode | effect |
|---|---|---|
| 0-7 | push-pull (`od_mask[p] = 0`) | `uio_out[p] <= v`; `uio_oe` unchanged |
| 0-7 | open-drain (`od_mask[p] = 1`) | `uio_out[p] <= 0`; `uio_oe[p] <= ~v` (v = 0 drives low, v = 1 releases) |
| 8-15 | | no effect |
| 16, 17 | | no effect |
| 18-23 | | `uo_out[p-16] <= v` |

Other pin instructions, all on pins 0-7 only (no effect for p >= 8):

- `OEN p`: if push-pull, `uio_oe[p] <= 1`; ignored in open-drain mode.
- `OEF p`: if push-pull, `uio_oe[p] <= 0`; ignored in open-drain mode.
- `OD p`: `od_mask[p] <= 1`, `uio_oe[p] <= 0`, `uio_out[p] <= 0` (the pin is
  released).
- `PP p`: `od_mask[p] <= 0`; `uio_oe` and `uio_out` unchanged.
- `OUTB rs`: `pinwrite(i, rs[i])` for i = 0..7, all at once.
- `OUTOE rs`: for every push-pull pin i, `uio_oe[i] <= rs[i]`; open-drain pins
  unchanged.

**Invariant:** `uio_out & od_mask == 0` at all times, so an open-drain pin is
never driven high (proved in `formal/pins.sby`).

Both threads may write pins; at most one instruction executes per cycle, so
there is no same-cycle conflict. The host `PINMODE` write (section 10) is
applied after the core's pin command of the same cycle: `od_mask <= v`,
`uio_oe <= uio_oe' & ~v`, `uio_out <= uio_out' & ~v`, where the primed values
already include the core's effect.

## 6. Timer: NOW and DEADLINE (DECISIONS D-018)

Each thread has four 16-bit registers: `period`, `prescale`, `NOW` and
`DEADLINE`. `NOW` counts **ticks**; one tick is `period` cycles.

### 6.1 Tick rule

At the end of every cycle c, for each thread t, unless t commits `SETT` or
receives a soft reset in that cycle:

- if `period[t] = 0`: nothing changes (the timer is disabled);
- else if `prescale[t](c) = 0`: `prescale[t] <= period[t] - 1` and
  `NOW[t] <= NOW[t] + 1`;
- else `prescale[t] <= prescale[t] - 1`.

### 6.2 Instructions

| Instruction | At slot c |
|---|---|
| `SETT rs` | `period <= rs`; `prescale <= rs - 1` (0 if rs = 0); `NOW <= 0`; `DEADLINE <= 0`. Completes. |
| `SETD k` (k = 0..255) | `DEADLINE <= NOW(c) + k`. Completes. |
| `WAITD k` (k = 0..255) | `target = DEADLINE(c) + k`. Completes iff `reached(NOW(c), target)`; on completion `DEADLINE <= target`. Flags unchanged. Blocks otherwise. |
| `RDT rd` | `rd <= NOW(c) - DEADLINE(c)` (mod 65536; a value with bit 15 clear means the deadline has been reached). |
| `BDR off` | branch iff `reached(NOW(c), DEADLINE(c))`. |
| `RDS rd` | bit 4 = `reached(NOW(c), DEADLINE(c))` (section 11). |
| `DELAY n` | unchanged, section 7.6; does not involve the timer. |

Consequences, which the tests pin down:

- `SETT rs` at slot s: `NOW` becomes 1 at the end of cycle s + rs, 2 at the
  end of s + 2 rs, and so on (the first tick is `rs` cycles after the
  instruction). With `rs = 1`, `NOW` increments every cycle from the end of
  s + 1.
- A thread that executes `SETT rs` and then one `WAITD 1` per bit places the
  k-th `WAITD 1` completion at the first slot of that thread strictly after
  cycle s + k rs, exactly where the old `WAITT` completed. Existing firmware
  keeps its bit timing with `WAITT` replaced by `WAITD 1`.
- If a loop is late, `WAITD 1` completes immediately (the target is already
  reached) and `DEADLINE` still advances by exactly 1, so later deadlines are
  not shifted and no tick is lost: the loop runs immediately as many times as
  it is behind. `WAITD k` with k > 1 waits several periods in one
  instruction.
- The comparison is signed on 16 bits: a deadline more than 32767 ticks in
  the past reads as not reached. Firmware must not leave a deadline that far
  behind.
- With the timer disabled (`period = 0`), `NOW` is frozen; `WAITD k` with
  k >= 1 blocks until the timer is enabled, and timeouts fire or not
  according to the frozen values. After reset or `SETT`, `NOW = DEADLINE = 0`
  and the deadline counts as reached.

### 6.3 Worked example

`LDI r0, 10` at slot 0, `SETT r0` at slot 2: `NOW` becomes 1 at the end of
cycle 12, 2 at the end of 22, 3 at the end of 32. `WAITD 1` evaluated at slots
4, 6, ..., 12 blocks (`NOW = 0`); at slot 14 `NOW = 1`, it completes,
`DEADLINE = 1`. A second `WAITD 1` at slot 16 blocks until slot 24. A `WAITD
3` after that targets 5 and completes at slot 54 (`NOW` becomes 5 at the end of
cycle 52). `SETD 3` at slot 26 (`NOW = 2`) sets `DEADLINE = 5`; a `WT1T p`
with the pin low then completes at slot 54 with `C = 1`.

## 7. Blocking instructions

### 7.1 General rule

A blocking instruction evaluated at slot c completes iff its condition holds
on the state observed during cycle c. If it does not, the thread's state is
unchanged and the same instruction is evaluated at slot c + 2. There is no
limit on how long a thread may block; the host sees `blocked[t]` in STAT.

### 7.2 Pin waits

| Instruction | completes iff |
|---|---|
| `WT0 p` | `level(c)[p] = 0` |
| `WT1 p` | `level(c)[p] = 1` |
| `WTR p` | `level(c)[p] = 1` and `level2(c)[p] = 0` |
| `WTF p` | `level(c)[p] = 0` and `level2(c)[p] = 1` |

Flags unchanged. A pad change in cycle e (pins 0-15) satisfies `WT0`/`WT1` from
slot e + 2 on and `WTR`/`WTF` at the one slot of each thread in e + 2, e + 3.

### 7.3 FIFO waits

`POP rd` completes iff the thread's inbox is non-empty during cycle c; it then
registers `rd <= zext(head byte)` and removes the head. `PUSH rs` completes
iff the outbox is not full during cycle c; it then appends `rs[7:0]`. Flags
unchanged. Occupancy is observed as of the start of the cycle: a host push
landing at the end of cycle c is seen by a `POP` at slot c + 2, not c.

### 7.4 Timeout forms

`WT0T WT1T WTRT WTFT POPT PUSHT` are the same encodings with the T bit set
(`tools/keyer_isa.py`). Each completes iff its base condition holds **or**
`reached(NOW(c), DEADLINE(c))`. On completion `C <= 0` if the base condition
held (whether or not the deadline had also been reached) and `C <= 1`
otherwise; the base effect (register write, FIFO pop or push) happens only
when the base condition held. `Z` and `DEADLINE` are unchanged. The usual
pattern is `SETD k` followed by the timed wait, then `BCS timeout_handler`.

### 7.5 `WAITD k`

Section 6.2. Flags unchanged.

### 7.6 `DELAY n`

Occupies exactly n + 1 consecutive slots of the thread: first evaluated at
slot c, it completes at slot c + 2n with no other effect. The thread's
private count is cleared whenever an instruction of that thread completes or
the thread stops (`HALT`, `STOP`, host RUN = 0, soft reset), so a `DELAY`
interrupted by a stop starts over when the thread is restarted. `DELAY 0`
takes one slot.

## 8. FIFOs

- Each thread has an inbox (host to thread) and an outbox (thread to host),
  16 entries of 8 bits, first-in first-out, with the head readable before it
  is popped. Occupancy 0..16 is reported in LEVELS; "empty" is occupancy 0,
  "full" is 16.
- A push on a full FIFO and a pop on an empty FIFO are ignored. This applies
  to host inbox writes (bytes are dropped) and to host outbox reads (which
  then return the stale head).
- A push and a pop in the same cycle both take effect (occupancy unchanged)
  provided neither is ignored; the pop returns the old head.
- Thread-side: `POP PUSH POPNB PUSHNB POPT PUSHT` as in sections 7.3, 7.4 and
  `docs/isa.md` (`POPNB`/`PUSHNB` complete always and report success in C =
  1, failure in C = 0; `POPT`/`PUSHT` report success in C = 0, timeout in
  C = 1). FIFO effects commit at the end of the slot like everything else.
- Host-side effects (push to inbox n, pop from outbox n, FIFOCLR, soft reset
  of thread n) take effect at the end of the cycle in which the host
  interface asserts them (section 10.3). FIFOCLR and a soft reset empty the
  named FIFOs; a push in the same cycle is lost.

## 9. Thread control instructions

- `START`: `running[other] <= 1`, `halted[other] <= 0` at the end of the slot.
  The other thread executes from `PC[other]` at its next slot (the very next
  cycle).
- `STOP`: `running[other] <= 0`; the other thread's `DELAY` count is cleared.
  Its `PC` stays where it is.
- `HALT`: `running[self] <= 0`, `halted[self] <= 1`, `PC <= PC + 1`.
- A thread cannot start itself; a stopped thread is started by the host or
  by the other thread.

## 10. Host interface

### 10.1 Protocol

SPI slave, mode 0 (CPOL = 0, CPHA = 0), MSB first: the master drives MOSI
before an SCK rising edge, the slave samples MOSI on the rising edge and
changes MISO after the falling edge. A transaction is CS_n low, one command
byte, zero or more data bytes, CS_n high. Command bit 7 = 1 write, 0 read;
bits 6:0 = register. Multi-byte values are little-endian. Register map:
`docs/isa.md` section 6.

Constraints: each SCK half-period must be at least 4 core cycles (SCK <=
clk / 8); SCK must be low when CS_n falls; CS_n must stay high for at least 4
core cycles between transactions. The interface samples SCK, MOSI and CS_n
through two flops; an edge on the pad during cycle e is acted on in cycle
e + 2. Outside these constraints the behaviour is undefined.

### 10.2 Byte timing

Let e be the cycle of the pad rising edge of the eighth SCK pulse of a byte.
The byte is complete in cycle e + 3. For a write, the register's side effect
is asserted to the core, FIFOs or pin unit during cycle e + 4 and is
registered at the end of e + 4, so it is observable from cycle e + 5. For a
read, data byte k is sampled from the state during the cycle in which the
interface acts on the first SCK falling edge after the last rising edge of
the previous byte (the command byte for k = 0), i.e. two cycles after that
pad edge; MISO carries its MSB from the following cycle and shifts one bit
per falling edge. An outbox read pops the head at the end of e_k + 4, where
e_k is the eighth rising edge of data byte k; with the minimum half-period
the next byte is sampled after that pop.

### 10.3 Writes

| Register | Data byte(s) | Effect, at the end of the cycle in which it is asserted |
|---|---|---|
| CTRL | byte 0 | bits 1:0: `running[t] <= bit t` (1 also clears `halted[t]`, 0 also clears `DELAY[t]`), for both threads; bits 3:2: soft reset of thread t (section 3.2). Further bytes ignored. |
| PC0 / PC1 | byte 0 | `PC[t] <= byte` if thread t is not running during that cycle, else ignored. Byte 1 ignored (must be 0). |
| IMEM_ADDR | byte 0 | address `<= byte`. Byte 1 ignored. |
| IMEM_DATA | pairs (lo, hi) | after each pair, if both threads were stopped when the pair completed: `IMEM[address] <= {hi, lo}` then `address <= address + 1`. A pair completed while a thread runs is dropped. |
| INBOX0 / INBOX1 | each byte | pushed into the inbox of thread 0 / 1 (dropped if full). |
| PINMODE | byte 0 | `od_mask <= byte`, and pins named in it are released (section 5.3). |
| IRQEN | byte 0 | IRQ enable mask (section 11). |
| FIFOCLR | byte 0 | bit 0 inbox 0, bit 1 outbox 0, bit 2 inbox 1, bit 3 outbox 1: emptied. |
| others | | ignored |

### 10.4 Reads

| Register | Bytes returned |
|---|---|
| CTRL | `{6'b0, running[1], running[0]}` |
| STAT | bit 0-1 `running`, 2-3 `halted`, 4-5 `blocked`, rest 0 |
| PC0 / PC1 | `PC[t]`, then 0 |
| IMEM_ADDR | address, then 0 |
| IMEM_DATA | `IMEM[address]` low byte, high byte, then the next word, and so on; the address advances by one per word read; valid only while both threads are stopped (otherwise stale data) |
| OUTBOX0 / OUTBOX1 | head of the outbox, popped after each byte (stale head and no pop when empty) |
| LEVELS | occupancy of inbox 0, outbox 0, inbox 1, outbox 1 |
| PINMODE | `od_mask` |
| IRQEN | mask |
| PINS | `level[7:0]` (uio), `level[15:8]` (ui), firmware `uo_out` (bits 1:0 read 0) |
| ID | 0x4B ('K'), then the ISA version |
| PINOUT | `uio_out`, then `uio_oe` |
| others | 0 |

Reading past the listed bytes returns the pattern given by the byte index
modulo its period (the implementation indexes on the low bits of the byte
count); firmware should not rely on it.

### 10.5 Memory port arbitration

The host owns the program memory port in any cycle in which it asserts a
write or a read request (IMEM_DATA traffic); such requests are generated only
while both threads are stopped. The fetch for the following cycle is invalid
(section 2.1). A write visible to a thread started by a later CTRL write is
always complete, because a CTRL write is a separate transaction.

## 11. Status word and IRQ

`RDS rd` returns, observed during the slot:

| bit | meaning |
|---|---|
| 0 | inbox empty |
| 1 | inbox full |
| 2 | outbox empty |
| 3 | outbox full |
| 4 | `reached(NOW, DEADLINE)` |
| 5 | other thread running |
| 6 | this thread's id |
| 15:7 | 0 |

`IRQ` (`uo[1]`) is the OR, over the bits set in IRQEN, of: bit 0 outbox 0
non-empty, bit 1 outbox 1 non-empty, bit 2 `halted[0]`, bit 3 `halted[1]`,
bit 4 inbox 0 empty, bit 5 inbox 1 empty. It is combinational from registered
state and follows a change by one cycle.

## 12. Per-instruction summary

Operands as in `docs/isa.md`. "slot" = completes in one slot. All effects
are registered at the end of the slot in which the instruction completes.

| Group | Instruction | Effect | Flags |
|---|---|---|---|
| ALU2 | `ADD SUB AND OR XOR MOV ADC SBC rd, rs` | `rd <= rd op rs` (`MOV`: `rd <= rs`) | Z, and C for ADD SUB ADC SBC |
| | `CMP rd, rs` / `TST rd, rs` | flags of `rd - rs` / `rd & rs`, no write | Z C / Z |
| ALU1 | `SHL SHR RCL RCR NOT NEG INC DEC SWAP REV8 rd` | as named; `RCL`/`RCR` rotate through C | Z, and C for SHL SHR RCL RCR NEG INC DEC |
| | `DJNZ rd, off` | `rd <= rd - 1`; if the new value is non-zero `PC <= PC + 1 + off` | none |
| Imm | `ADDI ANDI ORI XORI rd, imm` | `rd <= rd op imm` (ADDI sign-extends, the rest zero-extend) | ADDI: Z C; others: Z |
| | `LDI rd, imm` / `LDIH rd, imm` | `rd <= zext(imm)` / `rd[15:8] <= imm` | none |
| | `CMPI rs, imm` | flags of `rs - zext(imm)` | Z C |
| Branch | `BRA BEQ BNE BCS BCC off` | `PC <= PC + 1 + off` if the condition holds | none |
| | `BFE off` / `BFNE off` | branch iff inbox empty / non-empty | none |
| | `BDR off` | branch iff `reached(NOW, DEADLINE)` | none |
| | `BP0 p, off` / `BP1 p, off` | branch iff `level[p]` = 0 / 1 | none |
| | `JMP a` / `CALL a` | `PC <= a`; CALL also `LR <= PC + 1` | none |
| Pin | `SET CLR WRC p` | `pinwrite(p, 1 / 0 / C)` | none |
| | `OEN OEF OD PP p` | section 5.3 | none |
| | `WT0 WT1 WTR WTF p` | block until the condition (7.2) | none |
| | `WT0T WT1T WTRT WTFT p` | block until the condition or the deadline (7.4) | C |
| | `RDC p` / `TSTP p` | `C <= level[p]` / `Z <= (level[p] = 0)` | C / Z |
| PinR | `OUTR p, rs` / `INR rd, p` | `pinwrite(p, rs[0])` / `rd <= level[p]` | none / Z |
| Xfer | `PUSH rs` / `POP rd` | block until room / data (7.3) | none |
| | `PUSHT rs` / `POPT rd` | same, or the deadline (7.4) | C |
| | `PUSHNB rs` / `POPNB rd` | push / pop if possible, `C <= 1` on success else `C <= 0`; `rd` unchanged on failure | C |
| | `RDS rd` | status word (11) | none |
| | `RDCYC rd` | `rd <= CYC` | none |
| | `SETT rs` / `RDT rd` | section 6.2 | none |
| | `OUTB rs` / `OUTOE rs` | section 5.3 | none |
| | `INB rd` / `INW rd` | `rd <= level[7:0]` / `level[15:0]` | Z |
| | `RDLR rd` / `JMPR rs` | `rd <= LR` / `PC <= rs[7:0]` | none |
| Misc | `NOP` | nothing | none |
| | `HALT` | section 9 | none |
| | `RET` | `PC <= LR` | none |
| | `WAITD k` / `SETD k` | section 6.2 | none |
| | `DELAY n` | section 7.6 | none |
| | `SETC` / `CLC` | `C <= 1` / `C <= 0` | C |
| | `START` / `STOP` | section 9 | none |

## 13. Observability (test contract)

The lockstep harness (`test/keyer_tb.py`) reads these RTL signals by name;
an RTL implementation keeps them, with these meanings, and the golden model
exposes the matching attributes.

| RTL (`u_core` unless noted) | Model | Meaning during cycle c |
|---|---|---|
| `tid`, `exec`, `commit`, `pc_cur`, `ir` | derived from `step()` | slot owner; executes; completes; its PC; its instruction word |
| `pc[t]`, `lr[t]`, `fz[t]`, `fc[t]`, `regs[8t+i]` | `threads[t].pc .lr .z .c .regs[i]` | architectural state |
| `period[t]`, `prescale[t]`, `now[t]`, `deadline[t]` | `threads[t].period .prescale .now .deadline` | timer state (6) |
| `running` | `threads[t].running` | section 2.3 |
| `u_pins.uio_out .uio_oe .uo_out .od_mask` | `uio_out uio_oe uo_out od_mask` | drive state (5.3) |
| `u_imem.mem[a]` | `imem[a]` | program memory |
| `u_host.imem_we .imem_addr .imem_wdata .run_we .run_val .rst_pulse .pc_we .pc_val .inbox_push .inbox_wdata .outbox_pop .pinmode_we .pinmode_val .fifo_clr` | mirrored by the harness through `host_*` calls and `soft_reset()` | host effects asserted during c, landing at its end |

Golden model API (`tools/keyersim.py`), used by `tools/test_*.py` and
`test/`: `Machine(trace=False)` with attributes `imem` (256 ints),
`threads[2]`, `cycle`, `cyc`, `uio_out`, `uio_oe`, `uo_out`, `od_mask`,
`ext_uio` (default 0xFF, the external level on released uio pins), `ext_ui`,
`irq_en`, `trace` (list of `Retire(cycle, tid, pc, word, done)` when
tracing), `pin_events` (list of `(cycle, uio_out, uio_oe, uo_out)`, cycle =
the first cycle the new drive state is on the pads); methods `load(words,
base=0)`, `host_run(tid, run)`, `host_set_pc(tid, pc)`,
`host_inbox_push(tid, byte) -> bool`, `host_outbox_pop(tid) -> int | None`,
`host_pinmode(mask)`, `host_fifo_clear(mask)`, `status()`, `irq()`, `pad()`,
`step() -> done`, `run(cycles, on_cycle=None)`. `Thread`: `tid`, `regs`,
`pc`, `lr`, `z`, `c`, `period`, `prescale`, `now`, `deadline`, `inbox`,
`outbox` (deques, head first), `running`, `halted`, `blocked`, `delay_left`,
`soft_reset()`. Host effects passed through these calls between two `step()`
calls take effect before the next cycle, matching section 10.2 when the
harness calls them in the cycle the RTL asserts the pulse.
