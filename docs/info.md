<!---
This file is used to generate your project datasheet.
-->

## How it works

Keyer is a small processor built for bit-banging wire protocols, named after
the telegraph keyer: a program becomes precisely timed marks and spaces on a
wire. A host loads a program over SPI, then two hardware threads run it with
cycle-exact timing against the chip's 24 I/O pins. UART, SPI and I2C are
firmware, and so is anything else that fits the timing: the chip stays
reprogrammable after fabrication. A capture-and-replay unit turns the same
pins into a small logic analyser and waveform generator.

**Processor**

- Two barrel-interleaved threads: thread 0 executes on even core clocks,
  thread 1 on odd. Every instruction takes exactly one slot of its thread,
  taken branches cost nothing extra, and there are no stalls, caches or
  interrupts, so timing can be read off the listing. A full-duplex protocol
  is two straight-line programs.
- 16-bit datapath, eight registers per thread, 86 instructions in one
  16-bit format. Pin instructions set, clear, read, branch on and wait for
  any of the 24 pins, by level or by edge; inputs pass a two-flop
  synchroniser (two clocks of latency, exactly).
- Program memory: 256 words of 16 bits in an SRAM macro. Each thread has a
  16-byte inbox and a 16-byte outbox to the host.
- Bidirectional pins have a per-pin open-drain mode in which the pad can
  never be driven high (proved formally), which makes I2C and similar buses
  safe by construction.

**Deadline timer and timeouts**

- Each thread has a tick timer: `SETT` sets the tick period in clocks and
  restarts it, `NOW` counts ticks, `DEADLINE` is the next instant the
  program cares about. `WAITD k` waits until `NOW` reaches `DEADLINE + k` and
  advances the deadline by k, so one `WAITD 1` per bit puts every bit edge
  on the tick grid however many instructions the loop has. A loop that runs
  late completes its wait at once and still advances the deadline: it
  catches up without losing a tick or shifting the phase.
- Every blocking instruction has a timeout form (`WT0T WT1T WTRT WTFT POPT
  PUSHT`) that also completes when the deadline is reached and reports it
  in the carry flag (`SETD k`, the timed wait, `BCS handler`). A stuck bus
  cannot hang a thread: the I2C master gives up on a clock stretched beyond
  25 ms and reports status 0xFF to the host.

**Capture and replay** (`docs/CAPTURE.md`)

- Capture records timestamped edges on a group of four pins (under a watch
  mask) from a trigger condition on, as 16-bit entries `{delta[11:0],
  pins[3:0]}` in program-memory words the program does not use: 120 entries
  next to the I2C firmware, 255 with no firmware. The trigger is "the group
  matches a pattern under a mask after not matching", so an I2C START is
  one setting. An edge is never lost silently: if the two-entry queue
  overflows, a sticky flag is set and recording stops with a consistent
  prefix.
- Replay drives a recorded or host-written waveform on the masked pins of a
  group; entry k is applied exactly `delta` clocks after entry k-1, or the
  replay stops with a sticky underrun flag. Replay uses the normal pin
  rules, so open-drain pins stay safe.
- Both engines use the program memory only in the slots of a thread that is
  not running, so the firmware under test keeps its exact timing while it
  is being recorded. A thread can arm, stop and start them itself (`CAPC`),
  and the host can read the buffer back over SPI.

**Host interface**

SPI slave, mode 0, MSB first, up to clk/8, on `ui[0]` (SCK), `ui[1]` (MOSI),
`ui[2]` (CS_n) and `uo[0]` (MISO), with an interrupt output on `uo[1]`. A
transaction is a command byte (bit 7 = write, bits 6:0 = register) and data
bytes. Registers: thread run/stop and soft reset, status (running, halted,
blocked), PCs, program memory address and data, the four FIFOs and their
levels, open-drain mask, pin read-back, interrupt enables, and the capture
and replay configuration, control, status and counts. The full map is in
`docs/isa.md` section 6.

**Pins**

`uio[7:0]` are firmware pins 0-7 (bidirectional, push-pull or open-drain),
`ui[7:3]` firmware inputs 11-15 (`ui[2:0]` are readable as 8-10 but carry
the host SPI), `uo[7:2]` firmware outputs 18-23.

**Firmware in `fw/`**

| File | What it does | Pins |
|---|---|---|
| `uart.s` | full-duplex 8N1 UART, one thread per direction | TX `uo[2]`, RX `ui[3]` |
| `spi_master.s` | SPI master, mode 0, frames from the host | SCK `uo[3]`, MOSI `uo[4]`, CS_n `uo[5]`, MISO `ui[4]` |
| `i2c_master.s` | I2C master: start, repeated start, stop, write with ACK check, read, clock stretching with timeout | SCL `uio[2]`, SDA `uio[3]` (open-drain) |
| `capture_demo.s` | thread 1 records thread 0's I2C transaction and replays it | same I2C pins |

**Verification**

The behaviour is defined cycle by cycle in `docs/SEMANTICS.md`. A Python
model and the RTL were written from it independently and run in lockstep
from reset in simulation, every cycle compared, with all host traffic
mirrored; the firmware is checked against independent UART, SPI and I2C
protocol models; the FIFO, the pin unit, the timer and the capture and
replay engines carry formal proofs; and the host-interface and pads-only
firmware tests also run on the gate-level netlist.

## How to test

The host driver `tools/keyerhost.py` does everything below. It runs under
MicroPython on the demo board's RP2350 and bit-bangs the host SPI on
`ui[0..2]` and `uo[0]`; from a PC, `python3 tools/keyerhost.py ...` forwards
each command to the board with `mpremote` (`pip install mpremote`), and at
the board's prompt the same commands are `keyerhost.main([...])`. The
driver's SPI code is the code the simulation exercises (`cd test && make`
runs it against the design); the pin numbers in `BoardTransport` follow the
v3 demo board (SCK GP17, MOSI GP18, CS_n GP19, MISO GP33) and can be
overridden. Select the project and set the clock to 50 MHz first; the
driver does this through the board's SDK when it is present.

1. Self-test, nothing connected to the pins:

   ```sh
   python3 tools/keyerhost.py selftest
   ```

   It checks the ID register, writes and reads back all 256 program words,
   runs an echo program through both FIFOs with more data than they hold,
   and replays a waveform on `uo[2]`/`uo[3]` while capturing it. Expected:
   `selftest passed, ISA version 2`.

2. UART loopback: wire `uo[2]` to `ui[3]`.

   ```sh
   python3 tools/keyerhost.py load fw/uart.s     # assembles and loads (115200 baud at 60 MHz; edit BAUD_DIV)
   python3 tools/keyerhost.py pc 1 0F            # thread 1 starts at rx_init
   python3 tools/keyerhost.py run 3              # both threads
   python3 tools/keyerhost.py push 0 48 65 6C 6C 6F
   python3 tools/keyerhost.py pop 1              # 48 65 6C 6C 6F
   python3 tools/keyerhost.py status
   ```

   A USB-UART adapter on `uo[2]`/`ui[3]` shows the same bytes on a terminal.

3. Capture an I2C transaction and replay it: pull-ups on `uio[2]` (SCL) and
   `uio[3]` (SDA), an I2C device at address 0x50 optional (without one the
   write is NACKed, which is captured just the same). In Python, on the
   board or in a script using the library:

   ```python
   from keyerhost import *
   host = KeyerHost(BoardTransport())            # after BoardTransport().setup()
   run_sync(host.load_program(image))            # i2c_master.s at 0, capture_demo.s at 0x90
   run_sync(host.pinmode(0x0C))                  # SCL, SDA open-drain
   run_sync(host.capture_config(group=0, mask=0xC, trigger_pattern=0x4,
                                trigger_mask=0xC, base=0xA4, length=92))
   run_sync(host.replay_config(group=0, mask=0xC, base=0xA4, length=0))
   run_sync(host.set_pc(1, 0x90))
   run_sync(host.push(0, [0x01, 0x03, 2, 0xA0, 0x10, 0x02, 0x05]))   # START, write 2 bytes, STOP, wake thread 1
   run_sync(host.run(0b10))                      # thread 1 arms the capture and starts thread 0
   run_sync(host.wait_replay_done())             # recorded, then replayed on the same pins
   run_sync(host.stop())
   for delta, pins in run_sync(host.capture_drain(0xA4)):
       print(delta, bin(pins))                   # SCL is bit 2, SDA bit 3
   ```

   A logic analyser on SCL/SDA shows the transaction twice with identical
   edge spacing; `image` is the two firmware files assembled into one
   256-word list (`test/test.py`, `fw_merge`, shows how).

4. In simulation, without hardware: `bash scripts/check_all.sh` runs the
   model's tests, the cocotb suite (the driver's self-tests among them) and
   the formal proofs; `cd test && make` runs the cocotb suite alone.

## External hardware

None is needed for the self-test. For the firmware: pull-up resistors
(4.7 kOhm) on any bidirectional pin used in open-drain mode (I2C on
`uio[2]`/`uio[3]`); a wire or a USB-UART adapter on `uo[2]`/`ui[3]`; an SPI
flash or sensor on `uo[3..5]`/`ui[4]`; an I2C device on `uio[2]`/`uio[3]`. A
logic analyser is useful to see the replayed waveforms.
