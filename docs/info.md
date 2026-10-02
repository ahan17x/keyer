<!---
This file is used to generate your project datasheet.
-->

## How it works

Keyer is a small processor built for bit-banging wire protocols. A host loads a
program over SPI, then two hardware threads run it with cycle-exact timing
against the chip's 24 I/O pins. The required protocols (UART, SPI, I2C) are
firmware; so is anything else that fits the timing: the chip stays
reprogrammable after fabrication.

- Two barrel-interleaved threads: thread 0 executes on even core clocks,
  thread 1 on odd. Each instruction takes exactly one slot, taken branches
  cost nothing extra, and there are no stalls, caches or interrupts, so
  timing can be read off the listing. Full-duplex protocols are two
  straight-line programs.
- 16-bit datapath, eight registers per thread, 85 instructions. Pin
  instructions read, write and wait on any of the 24 pins (level or edge).
- Per-thread deadline timer: `WAITD` lands bit edges on the tick grid
  regardless of how many instructions the loop has, a late loop catches up
  without losing ticks, and every blocking instruction has a timeout form
  that gives up at the deadline, so a stuck bus cannot hang a thread.
- Bidirectional pins have a per-pin open-drain mode in which the pad can never
  be driven high, which makes I2C and similar buses safe by construction.
- Program memory is a 256 x 16 SRAM macro. Each thread has an inbox and an
  outbox FIFO (16 x 8) to the host.
- Host interface: SPI slave (mode 0, up to clk/8) with a register map for
  program load, thread control, FIFOs, pin modes and pin readback, plus an
  IRQ output.
- Capture and replay: a logic-analyser-style recorder of timestamped edges
  on four pins with a trigger condition, into the program memory, and a
  replay engine that drives a recorded or host-written waveform with the
  recorded timing, cycle for cycle. Both work while one thread runs the
  firmware under test, without touching its timing (`docs/CAPTURE.md`).

The ISA and register map are in `docs/isa.md`, the cycle-exact rules in
`docs/SEMANTICS.md`. The design was
verified by running a cycle-exact Python model of the ISA in lockstep with the
RTL (every cycle, from reset, with all host traffic mirrored), against
independent UART, SPI and I2C protocol models and constrained-random programs.

## How to test

1. Connect the demo board's RP2350 SPI to `ui[0..2]` and `uo[0]`.
2. Assemble a program with `tools/keyerasm.py` (examples in `fw/`).
3. Write it through the IMEM_ADDR / IMEM_DATA registers, set PCs, write RUN
   bits to CTRL. Feed data through INBOX0/1; collect results from OUTBOX0/1
   (check LEVELS first).
4. The firmware in `fw/uart.s` runs a full-duplex UART on `uo[2]` (TX) and
   `ui[3]` (RX); `fw/spi_master.s` and `fw/i2c_master.s` drive the pins named
   at the top of each file. `fw/capture_demo.s` records an I2C transaction
   of the master on thread 0 and replays it (setup in its header comment).

A Python driver for the demo board is planned under `tools/`.

## External hardware

Pull-up resistors (4.7k) on any bidirectional pin used in open-drain mode
(I2C). For the demo firmware: a USB-UART adapter on uo[2]/ui[3], an SPI
flash or sensor on uo[3..5]/ui[4], an I2C device on uio[2]/uio[3].
