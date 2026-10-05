# Keyer semantics: the cycle-exact contract

Version 0.4, 2026-10-04 (section 15 added: the serializer; ISA version 3. 0.3.1: 10.1 states the idle level of MISO. 0.3: section 14, capture and replay). This document is the reference for the golden model
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
- The fetch is valid unless the memory port was used in cycle c - 1 by the
  host (section 10.5) or by the capture or replay engine (section 14.1),
  and it is invalid in cycle 0. The host uses the port only while both
  threads are stopped and the engines only in a cycle whose fetch serves a
  thread that is not running, so the only way a running thread loses a
  slot is to be started (host RUN or `START`) at the end of a cycle in which
  an engine used its fetch: it then first executes two cycles later. The
  model implements this rule exactly.
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
- If a host RUN write and a core-initiated change land in the same cycle,
  `running` takes the host's value for every thread (a RUN write names both
  threads); `halted` is cleared by a RUN bit of 1 even against a same-cycle
  `HALT`, and otherwise follows the core's change (`HALT` sets it, `START`
  clears it); the `DELAY` count is cleared by a RUN bit of 0 and by `STOP`.
- A thread that is not running does nothing at its slots and holds all its
  state except the timer, which keeps ticking (section 6.1). When restarted
  it resumes at `PC[t]`, re-evaluating whatever instruction is there,
  including a blocking one it was stopped in.

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
outputs `uo[2..7]` are 0; `IRQEN` is 0; the host SPI state machine is idle;
every register of the serializer (section 15) is 0.
Program memory contents are preserved across reset in silicon; the
behavioural model and the simulators read unwritten words as 0.

### 3.2 Host soft reset (CTRL bit RST0 / RST1)

For the named thread, at the end of the cycle in which the pulse is applied
(section 10.3): `LR`, `Z`, `C`, `period`, `prescale`, `NOW`, `DEADLINE` and
the `DELAY` count become 0, and the thread's inbox and outbox are emptied.
`PC`, `R[0..7]`, `running`, `halted` are unchanged. Pins are unchanged. If
the thread commits an instruction in the same cycle (possible when one CTRL
write sets both RSTn and RUNn), the reset wins for everything it clears; the
instruction's other effects (PC, registers, pins) stand.

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
`ui`, 16-23 `uo`. The 5-bit pin field can also encode 24-31: those indices
are reserved, read as 0 and are ignored by every pin write, wait and mode
change (the assembler rejects them). The pad level during cycle c is `pad(c)`: for `uio[i]` it is
the driven value if `uio_oe[i](c)` is 1, otherwise whatever the outside world
drives (pull-ups in the tests); for `ui` it is the input; for `uo` it is
`uo_out(c)`. Pads `uo[0]` and `uo[1]` carry MISO and IRQ, which belong to the
host interface; in the pin unit's view (and the model's `pad()`) they are 0.

### 5.2 Synchroniser and level

- For pins 0-15, `level(c)[p] = pad(c - 2)[p]`: a two-flop synchroniser, two
  cycles of latency, no more and no less. After reset `level` reads 0 for
  cycles 0 and 1 regardless of the pads.
- For pins 16-23, `level(c)[p] = uo_out(c)[p - 16]`, the value being driven
  during cycle c (zero latency). Pins 16 and 17 (MISO, IRQ) always read 0.
- `level2(c)[p] = level(c - 2)[p]` for all 24 pins (0 for cycles 0 and 1,
  and for pins 0-15 also for cycles 2 and 3).
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
there is no same-cycle conflict. Within one cycle the writers are applied in
this order, each on the result of the one before: the core's pin command,
the replay engine (section 14.7), the serializer (section 15.3), the host
`PINMODE` write. The host `PINMODE` write (section 10) is
applied after the others of the same cycle: `od_mask <= v`,
`uio_oe <= uio_oe' & ~v`, `uio_out <= uio_out' & ~v`, where the primed values
already include the effects of the core, the replay engine and the
serializer.

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
(`tools/keyer_isa.py`). The T bit has no meaning for any other instruction of
the PIN and XFER majors and is ignored there. Each completes iff its base condition holds **or**
`reached(NOW(c), DEADLINE(c))`. The serializer waits `SERTXT SERTXCT SERRXT
SERWTT` (section 15.2) follow the same rule. On completion `C <= 0` if the base condition
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
- A push on a full FIFO and a pop on an empty FIFO are ignored, where "full"
  and "empty" are the occupancy during that cycle: a same-cycle pop does not
  make room for a push, nor a same-cycle push data for a pop. This applies
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

MISO (`uo[0]`) is 0 while CS_n is high, as seen through the synchroniser,
and after reset (DECISIONS D-033): a read that ended with ones on the wire
does not leave the line high between transactions.

### 10.2 Byte timing

A transaction may carry any number of data bytes; in particular one
IMEM_DATA transaction can load or read back all 256 words, the low/high
byte phase alternating for the whole transaction. Let e be the cycle of the
pad rising edge of the eighth SCK pulse of a byte. The byte is complete in
cycle e + 3. For a write, the register's side effect
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
| IRQEN | byte 0 | IRQ enable mask, 8 bits (section 11). |
| FIFOCLR | byte 0 | bit 0 inbox 0, bit 1 outbox 0, bit 2 inbox 1, bit 3 outbox 1: emptied. |
| CR_CTRL | byte 0 | capture/replay actions (section 14.2, 14.5). |
| CAP_CFG | bytes 0, 1 | capture group, watch mask, trigger pattern, trigger mask (section 14). |
| CAP_BUF | bytes 0, 1 | capture base word, length. |
| REP_CFG | byte 0 | replay group, drive mask. |
| REP_BUF | bytes 0, 1 | replay base word, length. |
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
| CR_CTRL | capture/replay status byte (section 14.8) |
| CAP_CFG, CAP_BUF, REP_CFG, REP_BUF | the bytes last written |
| CR_COUNT | entries recorded, then entries applied (section 14.8) |
| others | 0 |

Reading past the listed bytes returns the pattern given by the byte index
modulo its period (the implementation indexes on the low bits of the byte
count); firmware should not rely on it.

### 10.5 Memory port arbitration

The host owns the program memory port in any cycle in which it asserts a
write or a read request (IMEM_DATA traffic); such requests are generated only
while both threads are stopped. The fetch for the following cycle is invalid
(section 2.1). A write visible to a thread started by a later CTRL write is
always complete, because a CTRL write is a separate transaction. In a cycle
the host does not use, the capture engine, then the replay engine, may use
the port under the rule of section 14.1; the host always has priority.

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
| 7 | capture active (section 14.8) |
| 8 | replay active (section 14.8) |
| 15:9 | 0 |

`IRQ` (`uo[1]`) is the OR, over the bits set in IRQEN, of: bit 0 outbox 0
non-empty, bit 1 outbox 1 non-empty, bit 2 `halted[0]`, bit 3 `halted[1]`,
bit 4 inbox 0 empty, bit 5 inbox 1 empty, bit 6 capture done, bit 7 replay
done (section 14.8). It is combinational from registered
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
| | `CAPC rs` | applies `rs[3:0]` as the CR_CTRL control byte (14.2, 14.5) | none |
| | `SERCFG rs` | serializer configuration and reset (15.2) | none |
| | `SERTX rs` / `SERTXC rs` | block until the holding register is empty, then queue `rs[7:0]` (15.2) | none |
| | `SERRX rd` | block until a byte or a frame end (15.2) | Z |
| | `SERST rd` | `rd <= ` serializer status (15.7) | none |
| | `SERWT` | block until the transmitter is idle and empty (15.2) | none |
| | `SERTXT SERTXCT SERRXT SERWTT` | the same, or the deadline (7.4) | C (and Z for `SERRXT` when a byte or frame end is taken) |
| Misc | `NOP` | nothing | none |
| | `HALT` | section 9 | none |
| | `RET` | `PC <= LR` | none |
| | `WAITD k` / `SETD k` | section 6.2 | none |
| | `DELAY n` | section 7.6 | none |
| | `SETC` / `CLC` | `C <= 1` / `C <= 0` | C |
| | `START` / `STOP` | section 9 | none |
| | `SERI n` / `SERIC n` | `SERTX` / `SERTXC` with the byte n from the instruction (15.2); no timeout form | none |

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
| `u_host.imem_we .imem_re .imem_addr .imem_wdata .run_we .run_val .rst_pulse .pc_we .pc_val .inbox_push .inbox_wdata .outbox_pop .pinmode_we .pinmode_val .fifo_clr .cr_ctrl_we .cr_ctrl_val .cap_cfg_we .cap_cfg_val .cap_buf_we .cap_buf_val .rep_cfg_we .rep_cfg_val .rep_buf_we .rep_buf_val` | mirrored by the harness through `host_*` calls and `soft_reset()`; `imem_we`/`imem_re` also set `host_port_busy` for the cycle | host effects asserted during c, landing at its end |
| `u_cr.cap_armed .cap_trig .cap_done .cap_ovf .cap_last .cap_prev .cap_dt .cap_n .cap_w .q_count` | `cr.cap_armed` and so on, same names | capture engine state (14) |
| `u_cr.rep_active .rep_done .rep_under .rep_k .rep_f .rep_dt .pf_count` | `cr.rep_active` and so on | replay engine state (14) |
| `u_cr.cap_group .cap_mask .cap_tpat .cap_tmask .cap_base .cap_len .rep_group .rep_mask .rep_base .rep_len` | `cr.cap_group` and so on | configuration (14) |
| `u_ser.cfg .owner .tx_hold .tx_hold_c .tx_full .tx_state .tx_sh .tx_c .tx_app .tx_n .tx_half .tx_bit .tx_ones .tx_line .crc_m .crc5 .rx_state .rx_sh .rx_n .rx_ones .rx_psym .rx_last .rx_cnt .rx_w .rx_first .rx_hold .rx_valid .rx_end .rx_ovr .rx_serr .rx_ferr .rx_c5ok .rx_cok` | `ser.cfg` and so on, same names | serializer state (15.1) |

Golden model API (`tools/keyersim.py`), used by `tools/test_*.py` and
`test/`: `Machine(trace=False)` with attributes `imem` (256 ints),
`threads[2]`, `cycle`, `cyc`, `uio_out`, `uio_oe`, `uo_out`, `od_mask`,
`ext_uio` (default 0xFF, the external level on released uio pins), `ext_ui`,
`irq_en`, `trace` (list of `Retire(cycle, tid, pc, word, done)` when
tracing), `pin_events` (list of `(cycle, uio_out, uio_oe, uo_out)`, cycle =
the first cycle the new drive state is on the pads); methods `load(words,
base=0)`, `host_run(tid, run)`, `host_set_pc(tid, pc)`,
`host_inbox_push(tid, byte) -> bool`, `host_outbox_pop(tid) -> int | None`,
`host_pinmode(mask)`, `host_fifo_clear(mask)`, `host_cr_ctrl(byte)`,
`host_cap_cfg(word)`, `host_cap_buf(word)`, `host_rep_cfg(byte)`,
`host_rep_buf(word)` (the two-byte registers as little-endian 16-bit words),
`status()`, `irq()`, `pad()`, `step() -> done`, `run(cycles, on_cycle=None)`;
attribute `host_port_busy` (set by the harness before a `step()` in whose
cycle the host uses the memory port, cleared by `step()`), attribute
`executed` (set by `step()`: whether the slot's thread executed in that
cycle, section 2.1), `cr`, the capture/replay engine object with the
attributes named above, and `ser`, the serializer object with the
attributes of section 15.1 (integers). `Thread`: `tid`, `regs`,
`pc`, `lr`, `z`, `c`, `period`, `prescale`, `now`, `deadline`, `inbox`,
`outbox` (deques, head first), `running`, `halted`, `blocked`, `delay_left`,
`soft_reset()`. Host effects passed through these calls between two `step()`
calls take effect before the next cycle, matching section 10.2 when the
harness calls them in the cycle the RTL asserts the pulse.

## 14. Capture and replay (DECISIONS D-020, D-024; programmer's view in `docs/CAPTURE.md`)

One engine pair, shared by the host and both threads. All state below is
registered and reset to 0 by `rst_n`; a host soft reset of a thread does not
touch it.

**Configuration** (host registers, section 10.3; read back as written):
`cap_group[2:0]`, `cap_mask[3:0]`, `cap_tpat[3:0]`, `cap_tmask[3:0]`
(CAP_CFG byte 0 bits 2:0 and 7:4, byte 1 bits 3:0 and 7:4); `cap_base[7:0]`,
`cap_len[7:0]` (CAP_BUF bytes 0, 1); `rep_group[2:0]`, `rep_mask[3:0]`
(REP_CFG bits 2:0 and 7:4); `rep_base[7:0]`, `rep_len[7:0]` (REP_BUF). A
group value of 6 or 7 selects pins 24-31: `nib` reads 0 and replay writes
are ignored (section 5.1). Changing a configuration register while its engine is
active gives undefined results.

**Group nibble.** `nib(c) = level(c)[4g+3 : 4g]` for the engine's group g,
and `s(c) = nib(c) & mask` (watch mask for capture). An **entry** is a 16-bit
word `{delta[11:0], pins[3:0]}`.

### 14.1 Memory port

The port is **free** in cycle c when the host does not use it in c (section
10.5) and the thread that would execute in cycle c + 1, thread (c + 1) mod 2,
is not running during cycle c. In a free cycle the capture engine writes its
oldest queued entry if it has one; otherwise the replay engine issues a fetch
if it needs one (14.6). An engine never uses a cycle that is not free. A
cycle used by an engine invalidates the fetch for cycle c + 1 (section 2.1).

### 14.2 Capture control

Actions come from a host CR_CTRL write (bit 0 ARM, bit 1 DISARM) or from a
thread committing `CAPC rs` (`rs[0]` ARM, `rs[1]` DISARM); actions from both
sources in one cycle are all applied, ARM taking precedence over DISARM.
At the end of the cycle in which ARM lands: `cap_armed <= 1`, `cap_trig <= 0`,
`cap_done <= 0`, `cap_ovf <= 0`, `cap_n <= 0`, `cap_w <= 0`, the queue is
emptied, and `cap_was_armed <= 1`. DISARM: `cap_armed <= 0`. `cap_prev` holds
the group nibble of the previous cycle at all times (`cap_prev(c + 1) =
nib(c)`), independent of arming.

### 14.3 Trigger

`match(c) = (nib(c) & cap_tmask) == (cap_tpat & cap_tmask)`. While
`cap_armed` and not `cap_trig`, the engine **triggers** at the first cycle c
after the arm cycle at which `cap_tmask = 0`, or `match(c)` holds and
`(cap_prev(c) & cap_tmask) != (cap_tpat & cap_tmask)` (the nibble did not
match in cycle c - 1). At the end of the trigger cycle: `cap_trig <= 1`, the
entry `{0, s(c)}` is produced, `cap_last <= s(c)`, `cap_dt <= 1`.

### 14.4 Recording

At every cycle c with `cap_armed` and `cap_trig` set during c (so from the
cycle after the trigger on), with `d = cap_dt(c)`, the number of cycles since
the cycle of the last entry:

- if `s(c) != cap_last`: the entry `{d, s(c)}` is produced, `cap_last <= s(c)`,
  `cap_dt <= 1`;
- else if `d = 4095`: the idle entry `{4095, cap_last}` is produced,
  `cap_dt <= 1`;
- else `cap_dt <= d + 1`.

Producing an entry: `cap_n <= cap_n + 1`, and the entry enters a two-entry
**queue** unless the queue is full, in which case the entry is lost and
`cap_ovf <= 1` (the overflow is never silent). The queue is full when it
holds two entries, not counting an entry that is written to memory in the
same cycle (14.1): a departing entry frees its slot at once. When the entry
produced is the `cap_len`-th (`cap_n + 1 = cap_len`), or on overflow,
`cap_armed <= 0` in the same cycle. If `cap_len = 0`, ARM produces no entries:
the engine is done at once. A free cycle (14.1) with a non-empty queue writes
the oldest entry: `IMEM[(cap_base + cap_w) mod 256] <= entry`, `cap_w <=
cap_w + 1`, and the entry leaves the queue. `cap_done <= 1` at the end of any
cycle after which `cap_armed = 0`, the queue is empty and `cap_was_armed = 1`;
ARM clears it. **Capture active** (status, `RDS` bit 7) is
`cap_armed | (queue non-empty)`.

### 14.5 Replay control

Actions: host CR_CTRL bit 2 START, bit 3 STOP, or `CAPC rs` with `rs[2]`,
`rs[3]`; START takes precedence over STOP. At the end of the START cycle:
`rep_active <= 1`, `rep_done <= 0`, `rep_under <= 0`, `rep_k <= 0`,
`rep_f <= 0`, `rep_dt <= 0`, the prefetch buffer is emptied. STOP:
`rep_active <= 0`, `rep_done <= 1`. The replay **length** `n_rep` is
`rep_len` if `rep_len != 0`, else `cap_w` sampled at the START cycle (the
number of entries the last capture recorded), so firmware can replay a
capture whose length it does not know. If `n_rep = 0`, START sets
`rep_done <= 1` and leaves `rep_active` at 0. Sections 14.6 and 14.7 use
`n_rep` where they say `rep_len`.

### 14.6 Fetching entries

The replay engine keeps a two-entry **prefetch** buffer in order. In a free
cycle c not taken by the capture engine, if `rep_active`, `rep_f < n_rep`
and fewer than two entries are held or in flight, not counting an entry
applied in cycle c (14.7), it reads `IMEM[(rep_base + rep_f) mod 256]` and
`rep_f <= rep_f + 1`; the word is available during cycle c + 1 and is held in
the prefetch from the end of c + 1 on (it counts as "in flight" during
c + 1). With the port free every cycle, entries one cycle apart can be
replayed indefinitely. A word in flight when the replay becomes inactive
(STOP, underrun) is discarded on arrival: the prefetch holds nothing while
`rep_active` is 0, and START empties it.

### 14.7 Applying entries

`rep_dt(c)` is the number of cycles since the cycle in which the last entry
was applied (`rep_dt <= 1` at the end of an apply cycle, otherwise
`rep_dt <= rep_dt + 1` while active, saturating at 4095). At every cycle c
with `rep_active`, let the head be the oldest prefetched entry, if any, with
`target = max(delta, 1)`:

- `rep_k = 0` and the head is present: apply it (its delta is ignored);
- `rep_k >= 1`, the head is present and `rep_dt(c) = target`: apply it;
- `rep_k >= 1`, the head is present and `rep_dt(c) > target`: **underrun**:
  `rep_under <= 1`, `rep_active <= 0`, `rep_done <= 1`, nothing is applied;
- `rep_k >= 1`, no head is present and `rep_dt(c) = 4095`: **underrun** as
  above (the counter saturates at 4095, so an entry that is not present by
  then can no longer be applied on time, whatever its delta);
- otherwise nothing happens this cycle.

Applying the head at cycle c: for each i in 0..3 with `rep_mask[i]` set,
`pinwrite(4 rep_group + i, pins[i])` with the rules of section 5.3, applied
after the core's pin command of the same cycle and before a host PINMODE
write; the pads show the result during c + 1. Then `rep_k <= rep_k + 1`,
`rep_dt <= 1`, the head leaves the prefetch, and if `rep_k + 1 = n_rep`:
`rep_active <= 0`, `rep_done <= 1`. Consequently, when no underrun occurs,
entry k is applied exactly `max(delta_k, 1)` cycles after entry k - 1 for
every k >= 1, which is the timing a capture recorded (14.4).

### 14.8 Status, counts, interrupts

CR_CTRL read: bit 0 capture active, bit 1 `cap_trig`, bit 2 `cap_done`,
bit 3 `cap_ovf`, bit 4 `rep_active`, bit 5 `rep_done`, bit 6 `rep_under`,
bit 7 0. CR_COUNT read: `cap_w`, then `rep_k`. `RDS`: bit 7 capture active,
bit 8 `rep_active`. IRQ conditions: bit 6 `cap_done`, bit 7 `rep_done`.

## 15. Serializer (DECISIONS D-036; design note in `docs/SERIALIZER.md`)

One engine, shared by both threads: a transmitter and a receiver for one
pin pair, with NRZI or Manchester line coding, optional bit stuffing and
CRC-5, CRC-16 and CRC-32. It is half duplex. All state below is registered
and reset to 0 by `rst_n`; a host soft reset of a thread, a host RUN write,
`HALT` and `STOP` do not touch it.

### 15.1 State and definitions

Configuration `cfg[7:0]` (written by `SERCFG`): `mode = cfg[1:0]` (1 NRZI,
2 Manchester; 0 and 3 are **off**), `stuff = cfg[2]`, `crc32 = cfg[3]`,
`rxen = cfg[4]`, `rxskip = cfg[5]`, `k = cfg[7:6]`. `owner` (1 bit) is the
thread that committed the last `SERCFG`.

- **Pins.** `P = 2k`, `N = 2k + 1` (pins `uio[2k]`, `uio[2k+1]`).
- **Symbol period.** `T(c) = period[owner](c)`, the owner's timer period
  (section 6). Cycle c is a **symbol tick** iff mode is 1 or 2,
  `period[owner](c) != 0` and `prescale[owner](c) = 0`: the cycles at whose
  end the owner's `NOW` increments, were no `SETT` or soft reset to
  intervene. The definition uses only the registered values during c.
- **Transmitter:** `tx_hold[7:0]`, `tx_hold_c`, `tx_full` (holding register,
  its CRC mark, occupied); `tx_state[1:0]` (0 IDLE, 1 DATA, 2 CRC, 3 TAIL);
  `tx_sh[7:0]`, `tx_c` (the CRC mark of the byte in `tx_sh`), `tx_app` (a
  marked byte was sent in this frame); `tx_n[4:0]`; `tx_half`, `tx_bit`
  (Manchester second half pending, and its bit); `tx_ones[2:0]`; `tx_line`
  (the last NRZI symbol).
- **Receiver:** `rx_state` (0 HUNT, 1 DATA); `rx_sh[7:0]`; `rx_n[2:0]`;
  `rx_ones[2:0]`; `rx_psym`; `rx_last`; `rx_cnt[15:0]`; `rx_w`; `rx_first`;
  `rx_hold[7:0]`, `rx_valid`; `rx_end`, `rx_ovr`, `rx_serr`, `rx_ferr`,
  `rx_c5ok`, `rx_cok`.
- **CRC registers:** `crc_m[31:0]` and `crc5[4:0]`. One step with bit b:
  `step(r, b, POLY)`: `fb = r[0] ^ b`; `r' = r >> 1` (zero fill); if `fb`:
  `r' = r' ^ POLY`. `crc5` uses `POLY = 0x14` (USB CRC-5, x^5 + x^2 + 1,
  reflected). `crc_m` uses `0x0000A001` when `crc32 = 0` (USB CRC-16,
  x^16 + x^15 + x^2 + 1, reflected; bits 31:16 stay 0) and `0xEDB88320`
  when `crc32 = 1` (IEEE 802.3 CRC-32, reflected). `INIT_M` is `0x0000FFFF`
  or `0xFFFFFFFF`, the residual `RES_M` is `0x0000B001` or `0xDEBB20E3`,
  and the append width `W` is 16 or 32, by `crc32`. For `crc5` the initial
  value is `0x1F` and the residual `0x06`.
- **Pin writes.** `write(p, v, e)`: if `od_mask'[p] = 0` then
  `uio_out[p] <= v` and `uio_oe[p] <= e`, otherwise no effect, where
  `od_mask'` is the mask after the core's pin command of the same cycle
  (section 5.3 gives the order of the writers). `line(s)` is
  `write(P, s, 1)` and `write(N, ~s, 1)`. With s = 0 the pair shows J
  (P low, N high), with s = 1 K (P high, N low). `se0()` is
  `write(P, 0, 1)` and `write(N, 0, 1)`. Like every pin write, these are
  registered at the end of the cycle and on the pads during the next.
- `rx_last(c + 1) = level(c)[P]` in every cycle, whatever the mode, with P
  from `cfg(c)`; `SERCFG` and the rule of 15.4 do not alter it.

### 15.2 Instructions

All complete or block by section 2.2 on the state observed during the slot,
and their effects are registered at its end.

| Instruction | Completes iff | Effect on completion |
|---|---|---|
| `SERCFG rs` | always | `cfg <= rs[7:0]`; `owner <= ` the executing thread; every other register of 15.1 `<= 0`, except `rx_sh <= 0xFF`, `rx_w <= 1`, and `rx_last` (15.1). The pin registers are not changed. |
| `SERTX rs` | `tx_full(c) = 0` | `tx_hold <= rs[7:0]`; `tx_hold_c <= 0`; `tx_full <= 1` |
| `SERTXC rs` | `tx_full(c) = 0` | the same with `tx_hold_c <= 1` |
| `SERI n`, `SERIC n` | `tx_full(c) = 0` | `SERTX`, `SERTXC` with the byte n of the instruction word |
| `SERRX rd` | `rx_valid(c) = 1` or `rx_end(c) = 1` | if `rx_valid(c)`: `rd <= zext(rx_hold)`, `rx_valid <= 0`, `Z <= 0`. Otherwise: `rd <= status(c)` (15.7), `rx_end <= 0`, `Z <= 1`. C unchanged. |
| `SERST rd` | always | `rd <= status(c)` (15.7); flags unchanged |
| `SERWT` | `tx_state(c) = 0` and `tx_full(c) = 0` | none |

`SERTXT SERTXCT SERRXT SERWTT` are the timeout forms (section 7.4): they
also complete when `reached(NOW(c), DEADLINE(c))`; `C <= 0` if the base
condition held, else `C <= 1`, and the base effect (for `SERRXT` including
the write of Z) happens only when the base condition held. `SERI` and
`SERIC` have no timeout form. A serializer function code that the encoding
table does not name completes with no effect. The timeout bit is ignored by
`SERCFG` and `SERST`.

Precedence within one cycle: a committing `SERCFG` overrides every engine
update of 15.3 to 15.6 in that cycle (and the engine's pin writes of that
cycle are not made). Otherwise an instruction and the engine never write
different values to one register in one cycle, except `tx_full` and
`rx_valid`/`rx_end`, where the rules above and below cannot both apply (a
`SERTX` completes only when `tx_full(c) = 0`, the engine takes the holding
register only when `tx_full(c) = 1`; a frame start (15.5) clears `rx_valid`
and `rx_end` and a same-cycle `SERRX` clears the same flag).

### 15.3 Transmitter

The transmitter acts only in a symbol tick c (15.1), on the state during c.
Exactly the first rule that applies is carried out:

1. **Second half.** `mode = 2` and `tx_half = 1`: `line(tx_bit)`;
   `tx_half <= 0`.
2. **Start.** `tx_state = IDLE`: if `tx_full = 1`: `tx_sh <= tx_hold`;
   `tx_c <= tx_hold_c`; `tx_app <= tx_hold_c`; `tx_full <= 0`;
   `tx_state <= DATA`; `tx_n <= 0`; `tx_ones <= 0`; `tx_line <= 0`;
   `crc_m <= INIT_M`. No pin is written. If `tx_full = 0` nothing happens.
3. **Stuffed bit.** `stuff = 1` and `tx_ones = 6` (in DATA, CRC or TAIL):
   `emit(0)`; `tx_ones <= 0`. No data is consumed and no counter moves.
4. **Data bit.** `tx_state = DATA`: `b = tx_sh[0]`; `emit(b)`; `count(b)`;
   if `tx_c`: `crc_m <= step(crc_m, b)`. If `tx_n = 7`: `tx_n <= 0`, and if
   `tx_full = 1` the next byte is taken (`tx_sh <= tx_hold`;
   `tx_c <= tx_hold_c`; `tx_app <= tx_app | tx_hold_c`; `tx_full <= 0`),
   otherwise the frame's data ends: `tx_state <= CRC` if `tx_app = 1`, else
   `TAIL`. If `tx_n != 7`: `tx_sh <= tx_sh >> 1`; `tx_n <= tx_n + 1`.
5. **CRC bit.** `tx_state = CRC`: `b = ~crc_m[0]`; `emit(b)`; `count(b)`;
   `crc_m <= crc_m >> 1`. If `tx_n = W - 1`: `tx_n <= 0`;
   `tx_state <= TAIL`; else `tx_n <= tx_n + 1`.
6. **Tail.** `tx_state = TAIL`, by mode and `tx_n`:
   - mode 1: `tx_n = 0, 1`: `se0()`. `tx_n = 2`: `line(0)` (J).
     `tx_n = 3`: both pins are released: for p in {P, N}, if
     `od_mask'[p] = 0` then `uio_oe[p] <= 0` (the engine does not write
     `uio_out` here); `tx_state <= IDLE`.
   - mode 2: `tx_n = 0`: `line(1)`. `tx_n = 1..5`: nothing. `tx_n = 6`:
     `se0()` (both pins driven low); `tx_state <= IDLE`.
   - `tx_n <= tx_n + 1`, or `tx_n <= 0` in the step that returns to IDLE.

`emit(b)`: in mode 1, `s = b ? tx_line : ~tx_line`; `tx_line <= s`;
`line(s)`. In mode 2, `line(~b)`; `tx_bit <= b`; `tx_half <= 1` (the second
half follows at the next tick by rule 1, before anything else).
`count(b)`: if `stuff = 1`: `tx_ones <= b ? tx_ones + 1 : 0`.

Consequences. A frame is the bytes queued without a gap; bits leave least
significant first; the first symbol is on the pads in the cycle after the
second tick at which `tx_full` was seen. In NRZI mode the frame is coded
from J, so the byte `0x80` produces KJKJKJKK. The CRC covers exactly the
marked bytes, before stuffing, and is sent complemented, bit 0 first, only
if at least one byte was marked. A stuffed zero follows six ones even when
they are the last bits before the tail. The NRZI tail is SE0 for two symbol
periods and J for one; the Manchester tail holds P high and N low for six
symbol periods.

### 15.4 Receiver: when it runs

The receiver **runs** in cycle c iff mode is 1 or 2, `rxen = 1` and
`tx_state(c) = IDLE`. In a cycle in which it does not run: `rx_state <=
HUNT`, `rx_sh <= 0xFF`, `rx_n <= 0`, `rx_ones <= 0`, `rx_psym <= 0`,
`rx_cnt <= 0`, `rx_w <= 1`, `rx_first <= 0`; the other receive registers
(`rx_hold`, `rx_valid`, `rx_end`, the error and CRC verdict flags) keep
their values. (So from the end of cycle 0 on, `rx_sh` reads `0xFF` and
`rx_w` 1 while the engine is off.) When it runs, 15.5 and 15.6 apply, with
`sym = level(c)[P]`, `edge = (sym != rx_last(c))` and `T = T(c)`; counter
arithmetic is mod 65536.

### 15.5 Receiver: bits, bytes and frames

`bit(d)` takes a decoded bit:

- If `rx_state = DATA`, `stuff = 1` and `rx_ones = 6`: the bit is a stuffed
  bit and is discarded: `rx_ones <= 0`, and if `d = 1`: `rx_serr <= 1`.
- Otherwise: if `rx_state = DATA` and `stuff = 1`:
  `rx_ones <= d ? rx_ones + 1 : 0`. Let `v = {d, rx_sh[7:1]}`;
  `rx_sh <= v`. Then:
  - `rx_state = HUNT`: if `v = SYNC` (`0x80` in mode 1, `0xD5` in mode 2) a
    frame **starts**: `rx_state <= DATA`; `rx_n <= 0`; `rx_first <= 1`;
    `rx_ones <=` (if `stuff`: 1 in mode 1, 2 in mode 2, the trailing ones
    of the sync byte; else 0); `crc5 <= 0x1F`; `crc_m <= INIT_M`;
    `rx_valid <= 0`; `rx_end <= 0`; `rx_ovr <= 0`; `rx_serr <= 0`;
    `rx_ferr <= 0`.
  - `rx_state = DATA`: unless `rxskip = 1` and `rx_first = 1`:
    `crc5 <= step(crc5, d)` and `crc_m <= step(crc_m, d)`. If `rx_n = 7`
    the byte v is complete: `rx_n <= 0`; `rx_first <= 0`; if
    `rx_valid(c) = 1`: `rx_ovr <= 1` and the byte is lost, else
    `rx_hold <= v`, `rx_valid <= 1`. Else `rx_n <= rx_n + 1`.

`end()` is the end of a frame or of a hunt: if `rx_state = DATA`:
`rx_end <= 1`; `rx_ferr <= (rx_n != 0)`; `rx_c5ok <= (crc5 = 0x06)`;
`rx_cok <= (crc_m = RES_M)`, on the values during c. In both states:
`rx_state <= HUNT`; `rx_sh <= 0xFF`; `rx_n <= 0`; `rx_ones <= 0`;
`rx_first <= 0`.

If the transmitter's start (15.3 rule 2) and a receiver step write `crc_m`
in the same cycle, the transmitter's value stands.

### 15.6 Receiver: clock recovery and decoding

**Mode 1 (NRZI).** With `se0 = (level(c)[P] = 0 and level(c)[N] = 0)`:

- if `edge`: `rx_cnt <= T >> 1`;
- else if `rx_cnt != 0`: `rx_cnt <= rx_cnt - 1`;
- else c is a **sample cycle**: `rx_cnt <= T - 1`, and: if `se0`: `end()`
  and `rx_psym <= 0`; otherwise `d = (sym = rx_psym)`, `rx_psym <= sym`,
  `bit(d)`.

So a symbol change first seen in cycle c is followed by a sample in cycle
c + 1 + (T >> 1) and then every T cycles until the next change; a cycle
with an edge is never a sample cycle.

**Mode 2 (Manchester).**

- if `edge` and `rx_w = 1`: the edge is a mid-bit transition:
  `rx_w <= 0`; `rx_cnt <= T + (T >> 1) - 2`; `bit(sym)` (the level after
  the edge: a rising edge is a 1);
- else if `rx_cnt != 0`: `rx_cnt <= rx_cnt - 1` (an edge while `rx_w = 0`
  is a bit-boundary transition and is ignored);
- else if `rx_w = 0`: `rx_w <= 1`; `rx_cnt <= 2 T - 1`;
- else (`rx_w = 1`, `rx_cnt = 0`): the line is idle: `end()`.

So after a mid-bit edge in cycle c the receiver ignores edges up to and
including cycle c + T + (T >> 1) - 1, accepts the next edge after that as
the next mid-bit transition, and ends the frame if none comes for a further
2 T cycles.

The receiver is meant for `T >= 4` in mode 1 and `T >= 2` in mode 2;
smaller values follow the rules above literally.

### 15.7 Status word

`status(c)`, returned by `SERST` and by `SERRX` at a frame end:

| bit | meaning |
|---|---|
| 0 | `tx_full` |
| 1 | `tx_state != IDLE` |
| 2 | `rx_valid` |
| 3 | `rx_state = DATA` |
| 4 | `rx_end` |
| 5 | `rx_c5ok` |
| 6 | `rx_cok` |
| 7 | `rx_ovr` |
| 8 | `rx_serr` |
| 9 | `rx_ferr` |
| 15:10 | 0 |

`rx_c5ok` and `rx_cok` are written only by `end()` from DATA and by
`SERCFG`: they describe the last frame that ended and a frame start does
not clear them. `rx_ovr`, `rx_serr` and `rx_ferr` are cleared by a frame
start and accumulate until the next one (spec-questions Q21).

