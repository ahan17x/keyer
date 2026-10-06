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
  the program does not use. The I2C master firmware is 139 words, which
  leaves 117 entries: a pointer write, a repeated START and a one-byte read
  with STOP (four bytes on the bus) take about 100 (section 5); a chip
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
- Reading (section 5): `tools/test_keyerhost.py` checks the listing and
  every decoder on entries made from synthetic waveforms (with
  `entries_from_wave`), the passive I2C decoder, and the commands against a
  fake chip; `test/test_host.py` captures the I2C master's transaction and
  the UART transmitter on the chip and decodes them (pads only, so also at
  gate level).

## 5. Reading a capture with the driver

`tools/keyerhost.py` reads the buffer back and turns it into a timing
listing or a protocol decode. The commands are the same on the demo board,
from a PC (forwarded through `mpremote`) and in the simulation, which calls
the same `command()` function (`test/test_host.py`,
`test_host_capture_decode_i2c` and `test_host_capture_decode_uart`):

```
capture GROUP MASK TPAT TMASK BASE LEN   configure and arm (hex, as before)
capture status                           status bits and counts
capture disarm                           stop recording; the entries so far stay
capture read [BASE]                      the entries: a "# capture group G mask M entries N"
                                         line, then one "delta pins" line each
capture listing [BASE] [--clock HZ] [--names 2=SCL,3=SDA]
capture decode PROTO [BASE] [--clock HZ] [--KEY VALUE ...]
```

All three reading commands need both threads stopped (the memory port rule
of section 1; the driver refuses otherwise). BASE (hex) defaults to the
configured capture base and the number of entries is CR_COUNT's count of
entries written. The group and the watch mask are read back from CAP_CFG;
`--group G` overrides the group. Option values are decimal: bit numbers
are 0 to 3 inside the group's nibble, periods are core cycles, `--clock` is
the core clock in Hz and only adds times in us to the output. `drain` is
still there and prints the entry lines without the header.

**Listing.** A header with the group, its pins, the number of entries and
the total duration (the sum of the deltas: the cycle of the last entry,
counted from the trigger), then one line per entry: index, cycle, time (with
`--clock`), delta, the four bits under their pin numbers (with `--names`
labels; a bit outside the watch mask prints `-`), and what changed:
`trigger` for entry 0, `idle` for an idle entry, otherwise the bits that
rose or fell.

**Decoders.** `capture decode PROTO` runs one of the protocol models of
`tools/protomodels*.py` over the entries expanded to one pad vector per
cycle (the nibble at its real pins, so the models see pin numbers):

| PROTO | Parameters (default) | Reports | Model |
|---|---|---|---|
| `uart` | `--bit` (2), `--period` cycles per bit, or `--baud` with `--clock` | 8N1 bytes with the cycle of each start bit, framing errors | `UartDecoder` |
| `spi` | `--sck` (0), `--mosi` (1), `--csn` (2), `--miso` (none) | mode 0: the bytes of every CS_n frame, MOSI and MISO, partial bytes | `SpiSlaveModel`, one per data line |
| `i2c` | `--scl` (2), `--sda` (3) | START, repeated START, STOP, the address and R/W, every byte with its ACK bit (0 = ACK), a STOP or START inside a byte | `I2cDecoder` (passive) |
| `usb` | `--dp` (0), `--dm` (1), `--bit_cycles` (32) | low-speed packets: PID, address and endpoint or frame number, payload, CRC-5 or CRC-16 verdict, NRZI, stuffing, EOP and bit-timing errors; keep-alives and resets | `UsbLsHost._analyse` |
| `manchester` | `--p` (0), `--n` (1), `--clk_ns` (25.0) | 10BASE-T link pulses (start, width) and frames (destination, source, type, payload length, FCS), and the receiver's errors | `Eth10BTReceiver` |

The defaults of `uart` are the transmitter of `fw/uart.s` in group 4 (TX is
`uo[2]`, pin 18, bit 2), those of `i2c` the bus of `fw/i2c_master.s` in
group 0, those of `usb` and `manchester` the pairs of the serializer
firmware in group 0. Two more options apply to every decoder. `--initial V`
is the group's level in the cycle before the trigger: the capture starts at
the trigger, so a decoder that needs an edge to begin (a START, a CS_n fall,
a start bit) would miss the first one; by default it is the protocol's idle
level (both I2C lines high, CS_n high, the UART line high). `--tail N` is
how long the last level is held after the last entry: the entries do not
say how long it lasted, so by default it is 11 bit periods for UART (so a
last character whose final edge is a data bit, 0xF0 for example, still
reaches its stop bit), two bit times for USB and 1 cycle otherwise. A
decoder bit outside the watch mask reads 0; the command prints a warning.

**Library.** The same functions without a chip: `expand_capture(entries,
group)` (per-cycle pad vectors), `capture_listing(entries, group,
clock_hz, names, mask)`, `decode_capture(entries, group, proto, **params)`
(returns the result as a dict and the text), `entries_from_wave(levels)`
(the entries the engine records for a per-cycle nibble sequence, the
inverse of `expand_capture`), and `parse_capture(text)` for the text of
`capture read`. `KeyerHost.capture_read()` returns the entries as `(delta,
pins)` pairs (`capture_drain()` is the same).

**Files.** From a PC, `capture listing` and `capture decode` run on the PC:
the driver forwards `capture read` to the board and decodes the printed
entries locally, so the board needs no decoder. `capture read --save F`
also writes the entries to F, and `--file F` (in place of the board) lists
or decodes a saved capture, or the output of `drain` with `--group G`.

**Limits.**

- One nibble per capture: every line a decoder needs must be in one group
  and in its watch mask. I2C (two lines) and UART (one per direction) fit
  anywhere. SPI needs SCK, MOSI and CS_n (and MISO) in one group: no pin
  map of the firmware in `fw/` has that (`spi_master.s` has SCK on pin 19,
  group 4, and MOSI and CS_n in group 5), so the SPI decoder is for a bus
  wired to one group, for instance `uio[3:0]` of a chip used as an analyser.
- 255 entries are about 250 edges. A low-speed USB SETUP transaction
  (token, DATA0 with 8 bytes, ACK) is about 120 entries, and a capture next
  to `fw/usb_ls_device.s` (253 words) has 3. A 10BASE-T frame has one or
  two edges per bit, so a capture holds the preamble and the first bytes
  of a frame (the decoder then reports the truncated frame's errors), or
  link pulses: at 40 MHz the 16 ms between two pulses are 156 idle entries.
- Idle entries cost one entry per 4095 cycles of silence; the listing marks
  them and the decoders see the level held across them. At 9600 baud and
  60 MHz every bit of a UART character is at least one entry.
- The I2C test's transaction (pointer write, repeated START, one-byte read)
  is 102 entries in simulation; with a two-byte read it would need 116 to
  128 depending on the data, more than the 117 words above the I2C
  firmware. Each SDA change of the slave model comes a cycle after the SCL
  fall it answers and is an entry of its own; a real device's timing
  differs, so leave a margin.

**Example** (the simulation test `test_host_capture_decode_i2c`, 50 MHz,
`I2C_Q` = 30; the I2C slave model at 0x50 holds 0x5A at address 0x10):

```
> keyerhost load fw/i2c_master.s -D I2C_Q=30          (from a PC; 139 words)
loaded 139 words
> keyerhost capture 0 C 4 C 8B 75                     (group 0, SCL/SDA, START trigger, words 8B..FF)
armed
> keyerhost run 1
running [1, 0]
> keyerhost push 0 01 03 02 A0 10 01 03 01 A1 04 01 02 03 00
pushed 14
> keyerhost pop 0
00 00 5A 00
> keyerhost capture disarm
disarmed
> keyerhost stop
stopped
> keyerhost capture status
capture idle triggered done | replay idle | recorded 102 applied 0
> keyerhost capture read
# capture group 0 mask C entries 102
   0 4
  36 0
  86 8
  36 C
...
> keyerhost capture listing --clock 50000000 --names 2=SCL,3=SDA
capture: group 0 (pins 0-3), 102 entries, 4988 cycles = 99.760 us at 50 MHz
  idx    cycle         us delta  p3=SDA p2=SCL p1 p0  change
    0        0      0.000     0       0      1  -  -  trigger
    1       36      0.720    36       0      0  -  -  SCL fall
    2      122      2.440    86       1      0  -  -  SDA rise
    3      158      3.160    36       1      1  -  -  SCL rise
    4      202      4.040    44       1      0  -  -  SCL fall
...
   46     2335     46.700     1       1      0  -  -  SDA rise
   47     2434     48.680    99       1      1  -  -  SCL rise
   48     2478     49.560    44       0      1  -  -  SDA fall
   49     2514     50.280    36       0      0  -  -  SCL fall
...
   99     4908     98.160    52       0      0  -  -  SDA fall
  100     4944     98.880    36       0      1  -  -  SCL rise
  101     4988     99.760    44       1      1  -  -  SDA rise
> keyerhost capture decode i2c --clock 50000000
i2c (scl bit 2 = pin 2, sda bit 3 = pin 3): 2 transfers, 0 errors
  @0 (0.000 us)          START   0x50 W ACK : 10 ACK
  @2478 (49.560 us)      RESTART 0x50 R ACK : 5A NACK
  @4988 (99.760 us)      STOP
```

The bytecode is the one in the header of `fw/i2c_master.s`: START, WRITE
of `A0 10` (address 0x50 write, pointer 0x10), START (repeated), WRITE of
`A1` (address 0x50 read), READ of one byte (NACKed, the last), STOP, and a
WRITE of no bytes, which only pushes a status byte: when the host has
popped it, the STOP is on the bus. The outbox holds the two write statuses
(0: all ACKed), the byte read and that last status. Entry 48 is the
repeated START (SDA falls while SCL is high); entry 47 is the clock pulse
that carries it, which the decoder does not count as a data bit.
