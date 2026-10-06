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
- 16-bit datapath, eight registers per thread, 98 instructions in one
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
  pins[3:0]}` in program-memory words the program does not use: 117 entries
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
- The host driver reads a capture back as a timing listing and decodes it
  as UART, SPI, I2C, low-speed USB or 10BASE-T (`docs/CAPTURE.md` section
  5).

**Serializer**

One shared engine does the bit level of coded serial lines, so firmware
works in bytes: a shift register clocked by the owning thread's timer, NRZI
or Manchester coding, bit stuffing, CRC-5 (receive check), CRC-16 and
CRC-32, J, K and SE0 on a pin pair `uio[2k]`, `uio[2k+1]`, sync and
end-of-frame detection and framing status. A frame is the bytes firmware
queues without a gap; the engine appends the CRC and the end of packet.
Every frame starts from the idle line: reconfiguring the engine in the
middle of a frame (an abort) returns the pair to idle in the same clock
cycle. The status word has a sticky overrun bit for a received byte or
frame end that firmware had not taken when the next frame started; it
clears when the status is read. A frame being received is abandoned,
without a flag, when firmware starts the transmitter (half duplex).
Every wait on it has a timeout form. `docs/SERIALIZER.md` is the guide and
`docs/SEMANTICS.md` section 15 the cycle-exact rules. The two protocols
that use it need their own clock setting, both inside the signed-off
50 MHz: 48 MHz for low-speed USB (32 cycles per bit) and 40 MHz for
10BASE-T (2 cycles per half-bit; 50 MHz would need 2.5).

**Host interface**

SPI slave, mode 0, MSB first, up to clk/8, on `ui[0]` (SCK), `ui[1]` (MOSI),
`ui[2]` (CS_n) and `uo[0]` (MISO), with an interrupt output on `uo[1]`. A
transaction is a command byte (bit 7 = write, bits 6:0 = register) and data
bytes. Registers: thread run/stop and soft reset, status (running, halted,
blocked), PCs, program memory address and data, the four FIFOs and their
levels, open-drain mask, pin read-back, interrupt enables, and the capture
and replay configuration, control, status and counts. The full map is in
`docs/isa.md` section 6.

Three rules a board or a host program can rely on: MISO is 0 at all times
except while a read data byte is being shifted out (after reset, between
transactions, during every command byte and during writes), so the line can
be shared or probed; no transaction may be in progress when reset is
released (CS_n high from two clock cycles before `rst_n` rises; the host
driver `tools/keyerhost.py` parks the SPI pins around every reset it makes
and refuses a reset inside a transaction); and a `PINS` read returns the
three pin bytes and then a fourth byte of 0. Program memory is read back
only while both threads are stopped; what `IMEM_DATA` returns while a
thread runs is unspecified.

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
| `spi_slave.s` | SPI slave, mode 0: received bytes to the host, queued reply bytes out, MISO tri-stated when deselected, resynchronises after an aborted frame; SCK up to clk/20 | SCK `ui[3]`, MOSI `ui[4]`, CS_n `ui[5]`, MISO `uio[0]` |
| `i2c_slave.s` | I2C slave behaving like a 24Cxx EEPROM: pointer write, sequential and current-address reads, repeated START, clock stretching, bus timeout. The contents are a table in program memory (up to 64 bytes) that the host loads; written bytes are handed to the host | SCL `uio[2]`, SDA `uio[3]` (open-drain) |
| `jtag_master.s` | JTAG master: TAP reset, IR and DR scans of 1 to 256 bits (IDCODE is a reset and a 32-bit DR scan); TCK up to clk/36 | TCK `uo[3]`, TMS `uo[4]`, TDI `uo[5]`, TDO `ui[4]` |
| `swd.s` | ARM Serial Wire Debug host: connect sequence, any DP or AP read or write with ACK and parity handling (DPIDR is one read); SWCLK up to clk/24 | SWCLK `uo[3]`, SWDIO `uio[0]` (pull-up) |
| `ps2_host.s` | PS/2 host: receives device frames with parity and framing checks and a frame timeout, sends commands with the device's ACK checked | CLK `uio[0]`, DATA `uio[1]` (open-drain) |
| `ws2812.s` | WS2812B LED strip driver at the datasheet's 800 kbit/s timing, frames streamed through the inbox, underrun reported | DOUT `uo[2]` |
| `usb_ls_device.s` | Low-speed USB device on the serializer (clock 48 MHz; 24 MHz also works): bus reset, SETUP/IN/OUT on endpoint 0 with data toggles and ACK/NAK/STALL, CRC-5 and CRC-16 checked, GET_DESCRIPTOR (device and configuration, honouring wLength), SET_ADDRESS, SET_CONFIGURATION, everything else STALLed; responses start 3.7 to 4.7 bit times after the host's packet (allowed: 2 to 6.5). 253 of 256 words, one thread. Simulated against a USB host model only | D+ `uio[0]`, D- `uio[1]` (1.5 k pull-up on D- outside) |
| `eth_10bt_tx.s` | 10BASE-T transmit on the serializer (clock 40 MHz; 20 MHz also works): link pulses every 16 ms, and Ethernet frames (preamble, SFD, broadcast destination, fixed source and EtherType 0x88B5, up to 15 payload bytes staged by the host plus generated padding or filler up to a 1474-byte frame, CRC-32, start of idle, inter-frame gap). 88 words, one thread. Transmit only; simulated against a Manchester/Ethernet receiver model only. A real link needs a line driver and magnetics | TX+ `uio[0]`, TX- `uio[1]` |

**Verification**

The behaviour is defined cycle by cycle in `docs/SEMANTICS.md`. A Python
model and the RTL were written from it independently and run in lockstep
from reset in simulation, every cycle compared, with all host traffic
mirrored; every firmware program is checked against a model of its peer
that knows only the protocol (UART, SPI master and slave, I2C master and
slave, a JTAG TAP, an SW-DP target, a PS/2 device, a WS2812 decoder, a USB
host, a 10BASE-T receiver), and the USB and Ethernet models are checked
against published CRC values, a published Ethernet frame and USB packets
built by hand; the assembler bounds, for every timed wait of every program,
the instruction slots that can precede it on any path and compares them
with the deadline (`keyerasm.py --check-timing`); the FIFO, the pin unit, the core's timer and
control rules, the capture and replay engines and the serializer carry
formal proofs; the host-interface and pads-only
firmware tests also run on the gate-level netlist; and the test suite
itself is measured by mutation testing. `docs/VERIFICATION.md` has the
layers, what each found and the commands.

## How to test

Everything below needs only the Tiny Tapeout demo board (its RP2350 is the
host) and the driver `tools/keyerhost.py`. The driver runs under
MicroPython on the board and bit-bangs the host SPI on `ui[0..2]` and
`uo[0]`; from a PC, `python3 tools/keyerhost.py ...` forwards each command
to the board with `mpremote` (`pip install mpremote`), and at the board's
prompt the same commands are `keyerhost.main([...])`. The driver's SPI code
is the code the simulation exercises (`cd test && make` runs it against the
design, `test/test_host.py`); the pin numbers in `BoardTransport` follow
the demo board's GPIO map (SCK GP17, MOSI GP18, CS_n GP19, MISO GP33: `ui`
is GP17-24, `uo` is GP33-40) and can be overridden. The driver selects the
project, sets the clock to 50 MHz and resets it through the board's SDK
when it is present, once per session; `reset` resets it again on purpose.
Words, bytes, addresses and masks on the command line are hex; thread
numbers and `--option` values are decimal. No board has run this yet
(DECISIONS D-035): every step below is the simulation's output.

1. Self-test, nothing connected to the pins:

   ```sh
   python3 tools/keyerhost.py selftest
   ```

   It checks the ID register, writes and reads back all 256 program words,
   runs an echo program through both FIFOs with more data than they hold,
   and replays a waveform on `uo[2]`/`uo[3]` while capturing it. Expected:
   `selftest passed, ISA version 3`.

2. UART with the board as the other end, no wiring: the RP2350's UART1 can
   drive `ui[3]` (GP20 is UART1 TX) and listen on `uo[4]` (GP37 is UART1
   RX), so the firmware is loaded with its transmit pin moved from `uo[2]`
   to `uo[4]` (pin 20) and its divider set for 115200 baud at 50 MHz:

   ```sh
   python3 tools/keyerhost.py load fw/uart.s -D BAUD_DIV=434 -D TX=20
   python3 tools/keyerhost.py pc 1 0F            # thread 1 starts at rx_init
   python3 tools/keyerhost.py run 3              # both threads
   ```

   Then at the board's MicroPython prompt (`mpremote`):

   ```python
   from machine import UART, Pin
   import keyerhost
   u = UART(1, baudrate=115200, tx=Pin(20), rx=Pin(37))
   keyerhost.main(["push", "0", "48", "65", "6C", "6C", "6F"])   # the chip transmits "Hello"
   u.read()                                                      # b'Hello'
   u.write(b"Keyer")                                             # the chip receives
   keyerhost.main(["pop", "1"])                                  # 4B 65 79 65 72
   ```

   With a wire from `uo[2]` to `ui[3]` instead (the default pins, no `-D
   TX`), `push 0 ...` followed by `pop 1` returns the same bytes through the
   chip's own transmitter and receiver, which is what
   `test_host_uart_loopback` does in simulation. A USB-UART adapter on
   `uo[2]`/`ui[3]` shows the bytes on a terminal.

3. Capture and decode the chip's own UART, no wiring: the transmitter of
   step 2 is on pin 20, bit 0 of group 5 (pins 20-23). The capture engine
   writes the buffer in the slots of a thread that is not running, so run
   the transmitter alone; arm a capture that triggers when the line falls
   (pattern `0` under mask `1`) into the words above the 37-word firmware,
   send a byte, stop, read the recording back and decode it (`--period` is
   BAUD_DIV):

   ```sh
   python3 tools/keyerhost.py run 1              # thread 0 only
   python3 tools/keyerhost.py capture 5 1 0 1 40 40
   python3 tools/keyerhost.py push 0 4B
   python3 tools/keyerhost.py capture disarm
   python3 tools/keyerhost.py stop
   python3 tools/keyerhost.py capture listing --clock 50000000 --names 0=TX
   python3 tools/keyerhost.py capture decode uart --bit 0 --period 434 --clock 50000000
   ```

   The listing shows the start bit, the data edges and the stop bit with
   their times, and the decode prints the byte `4B` with the cycle of its
   start bit (`test_host_capture_decode_uart` runs this sequence with eight
   bytes, on the default pin and divider, and checks every decoded byte and
   every edge spacing). `capture read --save F` keeps the entries; `capture
   decode uart --file F ...` decodes them later without the board.

4. Capture an I2C transaction and decode it. This step needs pull-ups on
   `uio[2]` (SCL) and `uio[3]` (SDA) and an I2C EEPROM at address 0x50 (the
   only step that needs anything beyond the board; without a device the
   address byte is NACKed, which is captured and decoded just the same).
   The capture watches group 0 (pins 0-3) under mask `C` (SCL is bit 2, SDA
   bit 3), triggers on a START (SCL high, SDA low: pattern `4` under mask
   `C`) and records into the 117 words above the 139-word firmware (base
   `8B`, length `75`):

   ```sh
   python3 tools/keyerhost.py load fw/i2c_master.s -D I2C_Q=30
   python3 tools/keyerhost.py capture 0 C 4 C 8B 75
   python3 tools/keyerhost.py run 1
   python3 tools/keyerhost.py push 0 01 03 02 A0 10 01 03 01 A1 04 01 02 03 00
   python3 tools/keyerhost.py pop 0                # 00 00 5A 00
   python3 tools/keyerhost.py capture disarm
   python3 tools/keyerhost.py stop
   python3 tools/keyerhost.py capture listing --clock 50000000 --names 2=SCL,3=SDA
   python3 tools/keyerhost.py capture decode i2c --clock 50000000
   ```

   The bytes pushed are the I2C master's bytecode: START, write `A0 10`
   (address 0x50, pointer 0x10), repeated START, write `A1` (address 0x50,
   read), read one byte, STOP, and a write of no bytes whose status byte
   tells the host that the STOP is done. `pop` shows the two write
   statuses (0: ACKed), the byte read and that status. The simulation of
   this sequence (`test_host_capture_decode_i2c`, an EEPROM model holding
   0x5A at 0x10) prints:

   ```
   capture: group 0 (pins 0-3), 102 entries, 4988 cycles = 99.760 us at 50 MHz
     idx    cycle         us delta  p3=SDA p2=SCL p1 p0  change
       0        0      0.000     0       0      1  -  -  trigger
       1       36      0.720    36       0      0  -  -  SCL fall
       2      122      2.440    86       1      0  -  -  SDA rise
       3      158      3.160    36       1      1  -  -  SCL rise
   ...
     101     4988     99.760    44       1      1  -  -  SDA rise

   i2c (scl bit 2 = pin 2, sda bit 3 = pin 3): 2 transfers, 0 errors
     @0 (0.000 us)          START   0x50 W ACK : 10 ACK
     @2478 (49.560 us)      RESTART 0x50 R ACK : 5A NACK
     @4988 (99.760 us)      STOP
   ```

   `I2C_Q=30` gives about 400 kHz at 50 MHz; without `-D` (200) the bus
   runs at about 80 kHz with the same entries and longer deltas. A logic
   analyser on SCL/SDA shows the same edges. `replay 0 C 8B 0` plays the
   recording back on the same pins (`fw/capture_demo.s` does the capture
   and the replay from thread 1, and `test_host_capture_replay_demo` checks
   that the EEPROM model sees the transaction twice with the same edge
   timing). `docs/CAPTURE.md` section 5 has every option of the three
   `capture` commands and the SPI, USB and 10BASE-T decoders.

5. In simulation, without hardware: `bash scripts/check_all.sh` runs the
   model's tests, the cocotb suite (the driver's self-tests and the four
   steps above among them) and the formal proofs; `cd test && make` runs
   the cocotb suite alone.

## External hardware

None. The self-test, the UART through the board's own UART peripheral and
the capture of the chip's own transmitter (steps 1 to 3) use the demo board
alone. For the other firmware: pull-up resistors (4.7 kOhm) on any
bidirectional pin used in open-drain mode (I2C on `uio[2]`/`uio[3]`, PS/2 on
`uio[0]`/`uio[1]`, SWDIO on `uio[0]`); a wire or a USB-UART adapter on
`uo[2]`/`ui[3]`; an SPI flash or sensor on `uo[3..5]`/`ui[4]`; an I2C
device on `uio[2]`/`uio[3]`; a JTAG or SWD target on
`uo[3..5]`/`ui[4]`/`uio[0]`, an SPI or I2C master, a PS/2 keyboard, a
WS2812B strip on `uo[2]` (through a 3.3 V to 5 V level shifter); for the
serializer firmware a 1.5 kOhm pull-up on `uio[1]` and a USB host for the
low-speed device, a line driver and magnetics for 10BASE-T. A logic
analyser is useful to see the replayed waveforms.
