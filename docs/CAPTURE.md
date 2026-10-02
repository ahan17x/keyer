# Capture and replay (DECISIONS D-020, D-024)

Keyer's second half: a logic-analyser-style **capture** of timestamped edges
on a group of pins, armed by the host or by firmware and started by a
trigger condition, into a buffer the host drains; and **replay** of a
recorded or host-written waveform on output pins with the recorded timing,
cycle for cycle. Together they let a protocol be recorded from a real device,
inspected, edited and played back at the device, or played back at the
chip's own firmware under test. Cycle-exact rules: `docs/SEMANTICS.md`
section 14. This file is the programmer's view and the sizing decision.

## 1. What the user sees

- **Pin group.** Capture and replay work on one **nibble** of the 24-pin
  space at a time: group g = 0..5 is pins 4g..4g+3 (0-1: `uio`, 2-3: `ui`,
  4-5: `uo`). A 4-bit **mask** selects the pins that are watched (capture)
  or driven (replay) within the group; the others are ignored. I2C needs two
  pins, SPI four, UART one per direction. Replay drives through the normal
  `pinwrite` rules, so open-drain pins stay safe and `ui` pins are simply
  not driven.
- **Entry.** One 16-bit word per event: `{delta[11:0], pins[3:0]}`.
  `pins` is the masked value of the group after the event, `delta` the
  number of core cycles since the previous entry (0 for the first). A gap
  longer than 4095 cycles is bridged by an **idle entry** that repeats the
  previous value with `delta = 4095`, so no timestamp is ever lost to the
  width of the field. At 60 MHz an idle entry is 68 us; a 100 kHz I2C bus
  never needs one, 9600-baud UART needs one per bit.
- **Buffer.** The entries live in the **program memory**: the host (or the
  firmware's loader) assigns a base word and a length (1..255 entries) that
  the program does not use. The I2C master firmware is 136 words, which
  leaves 120 entries, roughly two I2C bytes with START and STOP; a chip
  used purely as an analyser has 255. The host drains the buffer through
  the existing `IMEM_DATA` read and writes a waveform to replay through the
  existing `IMEM_DATA` write.
- **Trigger.** Arming starts the engine watching. It triggers at the first
  cycle at which the group's masked level matches a 4-bit pattern under a
  4-bit trigger mask and did not match in the previous cycle, or at once if
  the trigger mask is 0. "SDA falls while SCL is high" (an I2C START) is
  pattern SCL = 1, SDA = 0 under a mask of both pins.
- **Recording** begins at the trigger with an entry of delta 0 and continues
  until the buffer holds `length` entries (status **done**), the host or
  firmware disarms it, or an **overflow** happens. Overflow is the only way
  an edge is lost, and it is never silent: the sticky overflow flag is set
  and recording stops, so the buffer is a consistent prefix of the signal.
- **Replay** drives the masked pins of its group with the entries from its
  own base and length: entry 0 sets the initial levels, entry k is applied
  exactly `delta_k` cycles after entry k-1. It ends after the last entry
  (status **done**) or when an entry was not fetched in time (**underrun**,
  sticky, replay stops).
- **Memory port.** Both engines use the program memory's single port in the
  slots of a thread that is **not running**; a running thread's fetch is
  never disturbed, so the timing of the firmware under test never changes.
  With both threads running, capture overflows after two queued entries and
  replay underruns; the intended use is one thread running the firmware
  under test and the other stopped (or halted), or both stopped for a pure
  analyser or generator. Host memory traffic always has priority, so
  draining or loading the buffer is done with both threads stopped, as the
  `IMEM_DATA` register already requires.
- **Firmware access.** `CAPC rs` applies the same control byte the host
  writes to `CR_CTRL`, so a thread can arm a capture, start a replay, or
  stop either. `RDS` reports whether a capture or a replay is active, so a
  thread can wait for one to finish (`fw/capture_demo.s` does both).
- **Interrupts.** IRQEN gains "capture done" and "replay done".

## 2. Register and instruction interface

Host registers (SPI, `docs/isa.md` section 6 conventions; new addresses):

| Reg | Name | Write | Read |
|---|---|---|---|
| 0x11 | CR_CTRL | control byte: bit 0 ARM capture, bit 1 DISARM capture, bit 2 START replay, bit 3 STOP replay (each bit is an action; 1 = do it) | status byte: bit 0 capture active, 1 triggered, 2 capture done, 3 overflow, 4 replay active, 5 replay done, 6 underrun |
| 0x12 | CAP_CFG | byte 0: bits 2:0 group, bits 7:4 watch mask; byte 1: bits 3:0 trigger pattern, bits 7:4 trigger mask | same two bytes |
| 0x13 | CAP_BUF | byte 0 base word, byte 1 length (1..255) | same |
| 0x14 | REP_CFG | byte 0: bits 2:0 group, bits 7:4 drive mask | same |
| 0x15 | REP_BUF | byte 0 base word, byte 1 length (1..255; 0 = the number of entries the last capture recorded) | same |
| 0x16 | CR_COUNT | - | byte 0 entries recorded, byte 1 entries applied |

IRQEN: bit 6 capture done, bit 7 replay done.

Instruction: `CAPC rs` (XFER major, one new sub-opcode): writes `rs[3:0]` as
the CR_CTRL control byte. `RDS` bit 7 = capture active, bit 8 = replay
active.

## 3. Sizing options

Costs use the CMOS5L numbers in `docs/AREA.md`: a reset flop is 48.99 um^2,
a flop-based memory came out at about 75 um^2 per bit including its muxes,
and the engines (configuration, counters, a two-entry queue, a two-entry
prefetch, comparators) are about 185 flops plus logic, roughly 20,000 um^2.
The design before this feature is 121,796 um^2 of cells plus the 28,127 um^2
program-memory macro, 149,923 um^2, 16.6% of the 902,417 um^2 core. The
limit set for this feature is 25% of the core, 225,604 um^2.

| Option | Buffer | Extra flops | Extra area | Design total | Of core |
|---|---|---|---|---|---|
| A | dedicated flop buffer, 32 x 16 | 512 + 185 | about 58,000 um^2 | 208,000 um^2 | 23.0% |
| B | dedicated flop buffer, 64 x 16 | 1,024 + 185 | about 97,000 um^2 | 247,000 um^2 | 27.4% |
| C | entries in the program memory (chosen) | 185 | about 20,000 um^2 | 170,000 um^2 | 18.8% |

A fits but holds one I2C byte. B does not fit. C holds 120 entries next to
the I2C firmware and 255 on its own, costs the least, keeps the design
small (D-020's second half), and grows for free if the program memory moves
to the 512 x 16 macro. Its price is the rule that the engines only use the
slots of a stopped thread, which is exactly the analyser's natural setup
(one thread under test, one idle) and which keeps the firmware under test
cycle-exact. A second 256 x 16 macro for the buffer would also fit (about
28,000 um^2) but doubles the macro-flow risk before the first macro is even
through the flow; it stays a later option.

## 4. Test plan

- ISS tests (`tools/test_iss.py`): trigger forms, deltas, idle entries,
  done, disarm, overflow with both threads running, replay timing, underrun,
  open-drain replay.
- Lockstep (`test/test.py`): the capture of thread 0's own I2C transaction
  with thread 1 halted, drained over SPI; a replay of a host-written
  waveform checked edge by edge against the recorded deltas; capture during
  replay (loopback: replay on `uo` pins wired externally to `ui` pins);
  `CAPC` stays out of the random streams (a random capture would overwrite
  the running program).
- Formal (`formal/capture.sby`): an edge seen while the queue is full sets
  the overflow flag in the next cycle and nothing is written past the
  recorded prefix; an applied replay entry is applied exactly `delta` cycles
  after the previous one whenever the replay has not underrun; recording
  never writes outside `[base, base + length)`.
- Demo: `fw/capture_demo.s` on thread 1 arms a capture of SCL/SDA, starts
  thread 0's I2C master, halts; the master runs the transaction and restarts
  thread 1 (bytecode 0x05); thread 1 stops thread 0, waits for the capture
  to finish, starts a replay of the recording on the same pins and waits for
  it. The test checks that the I2C slave model sees the same transaction
  twice with identical edge timing.
