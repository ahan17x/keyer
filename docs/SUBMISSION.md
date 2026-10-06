# Submission text

The text for the Jane Street protocol emulator competition form and for the
Tiny Tapeout datasheet (`docs/info.md`). Numbers are those of
`docs/AREA.md` (run 37363511492, master) and `docs/VERIFICATION.md` as of
2026-10-06; update both before the final submission (docs/HANDOFF.md has
the schedule). Keep the two texts consistent: the datasheet's "How it
works" and "Verification" paragraphs are the long form of sections 1 to 3
here.

## 1. What the chip is

Keyer is a small processor built to bit-bang wire protocols, named after
the telegraph keyer: a program becomes precisely timed marks and spaces on
a wire. A host loads a program over SPI, and two hardware threads run it
with cycle-exact timing against the chip's 24 I/O pins. UART, SPI and I2C
are firmware, and so is anything else that fits the timing (an SPI slave,
an I2C EEPROM, a JTAG master, an SWD host, a PS/2 host, a WS2812 driver, a
low-speed USB device and a 10BASE-T transmitter are in the repository), so
the chip stays reprogrammable after fabrication.

The processor is a 16-bit machine with eight registers per thread and 98
instructions in one 16-bit format. The two threads are barrel-interleaved:
thread 0 executes on even clock cycles, thread 1 on odd ones, every
instruction takes exactly one slot of its thread, taken branches cost
nothing extra, and there are no caches, stalls or interrupts, so the timing
of a program can be read off its listing. A full-duplex protocol is two
straight-line programs. Pin instructions set, clear, read, branch on and
wait for any of the 24 pins, by level or by edge; inputs pass a two-flop
synchroniser with exactly two clocks of latency. The bidirectional pins
have a per-pin open-drain mode in which the pad can never be driven high,
which makes I2C and similar buses safe by construction. The program memory
is 256 words of 16 bits in an SRAM macro; each thread has a 16-byte inbox
and a 16-byte outbox to the host. The design is 15,085 standard cells plus
the macro, 27.1% of a 6x4 Tiny Tapeout block on IHP's 130 nm CMOS5L
process, signed off at 50 MHz with setup met at all three corners (+12.39
ns fast, +8.25 ns typical, +0.99 ns slow).

## 2. What is unique about it

**Capture and replay with decoding.** The same pins are a small logic
analyser and waveform generator. A capture records timestamped edges on a
group of four pins, from a trigger condition on (an I2C START is one
setting), as 16-bit entries in program-memory words the program does not
use; an edge is never lost silently. A replay drives a recorded or
host-written waveform back on the pins with the recorded timing, cycle for
cycle, through the normal pin rules, so open-drain pins stay safe. Both
engines use the program memory only in the slots of a thread that is not
running, so the firmware under test keeps its exact timing while it is
being recorded. The host driver reads a capture back, prints it as a timing
listing and decodes it with the protocol models the project already uses
to judge its firmware: UART, SPI, I2C, low-speed USB and 10BASE-T
Manchester. A transaction can be recorded from a real device, inspected,
edited and played back at the device, or played back at the chip's own
firmware under test.

**Two-thread deadline timing with a timeout on every wait.** Each thread
has a tick timer with a deadline register: `WAITD k` waits until the timer
reaches the deadline and advances it by k ticks, so one `WAITD 1` per bit
puts every bit edge on the tick grid however many instructions the loop
has, and a loop that runs late catches up without losing a tick or
shifting the phase. Every blocking instruction (pin waits, FIFO waits) has
a timeout form that also completes at the deadline and reports it in the
carry flag, so a stuck bus cannot hang a thread: the I2C master gives up on
a clock stretched beyond its limit and reports the failure to the host.
The assembler checks the timing statically: for every timed wait of every
program it bounds the instruction slots that can precede it on any path and
compares them with the deadline.

**A serializer that reaches USB and Ethernet.** One shared engine does the
bit level of coded serial lines, so firmware works in bytes: a shift
register clocked by the owning thread's timer, NRZI or Manchester coding,
bit stuffing, CRC-5, CRC-16 and CRC-32, J, K and SE0 on a pin pair, sync
and end-of-frame detection, and framing status with a sticky overrun bit.
With it, a 253-word program is a low-speed USB device that enumerates
against a USB host model (bus reset, SETUP, IN and OUT on endpoint 0, data
toggles, descriptors, SET_ADDRESS, SET_CONFIGURATION), and an 88-word
program transmits 10BASE-T link pulses and Ethernet frames with CRC-32
that a receiver model checks against the standard's timing templates.

## 3. How it was verified

The behaviour is defined cycle by cycle in a written contract
(`docs/SEMANTICS.md`), and the spec wins: if the model, the RTL and the spec
disagree, the spec is the reference and a change to it is a recorded
decision. A Python golden model and the Verilog RTL were written from the
contract independently, by sessions that could not read each other's
side, and the two are run in lockstep from reset in simulation: the model
steps once per RTL clock and every drive register, program counter, timer,
capture and replay state, serializer register, executing instruction and
register-file write is compared every cycle, with all host traffic
mirrored. Nine layers of checks sit on top of that, each with the command
that reproduces its numbers:

- Lint and compile with Verilator `-Wall` and Icarus; a single encoding
  table generates the header the RTL decodes with.
- 484 model and tool tests pin the golden model, the assembler and the
  host driver to the contract instruction by instruction.
- Every firmware program (twelve) runs against a model of its peer that
  knows only the protocol, eleven models in all, 313 test cases; the USB
  and Ethernet models are themselves checked against published CRC values,
  a published Ethernet frame and USB packets built by hand.
- 77 cocotb tests run the RTL in lockstep with the model, through the pads
  with the real host driver, and with the protocol models attached; the
  pads-only tests also run on the hardened gate-level netlist.
- Formal proofs with SymbiYosys and abc: the FIFO (occupancy and
  first-in-first-out order), the pin unit (an open-drain pin is never
  driven high, synchroniser latency exactly two cycles), the core's timer
  and control rules, thread selection and completion, the capture and
  replay engines (an overflow is never silent, replay timing is exact for
  every delta, writes stay inside the buffer) and the serializer (the
  stuffer never emits seven consecutive ones, the CRC equals an
  independent bit-serial reference, a transmitter into a receiver delivers
  the byte and a good verdict).
- Equivalence proofs on every refactor: a restructured core is proved with
  Yosys to have the same flops as the core it replaces and, from equal
  state and inputs, every flop input and output equal.
- Mutation testing of the test suite itself: 2,118 single-line faults in
  the RTL (operator swaps, removed negations, flipped constants, inverted
  conditions, stuck-at on every condition, enable and strobe); 2,021 are
  killed by a test failure or a formal counterexample, 97 are equivalent
  (no behaviour at the pins or in any state a test may read differs:
  proved by Yosys, or argued in writing where the proof needs the reset
  sequence), none survives. The campaign found no fault in the RTL or the
  model; it found two holes in the suite, both closed.
- Static timing: every hardening run is checked at all three process
  corners, and the merge rule for any change under `src/` is a clean run
  (setup met at every corner, hold clean, LVS and antenna clean, precheck
  passing) under 40% utilisation. The last run had no slew violation and
  one reported capacitance entry on the macro's output, being repaired.

Every bug found by any layer is a row in `docs/BUGS.md` (51 rows: five in
the RTL, found by review against the contract, by a lockstep test and by
the model's author questioning the spec, the rest in the firmware, the
models, the tests, the harness and the flow), and every design decision is
an appended, numbered entry in `docs/DECISIONS.md` with the alternatives
rejected. No board has run the design yet; every protocol claim rests on
the protocol models.

## 4. Short form (for the Tiny Tapeout `info.yaml` description and a form field of one paragraph)

Keyer: a two-thread, cycle-exact 16-bit CPU that bit-bangs wire protocols
(UART, SPI, I2C, JTAG, SWD, PS/2, WS2812, low-speed USB, 10BASE-T) from
firmware loaded over SPI, with deadline timers that put every bit edge on
a tick grid and time out every wait, a serializer engine for NRZI and
Manchester lines with CRC, and a capture-and-replay unit that records and
plays back timestamped waveforms on its 24 pins and decodes them with the
same protocol models that verify the firmware. Written from a cycle-exact
spec with an independent golden model run in lockstep with the RTL, formal
proofs, equivalence proofs on every refactor and a mutation score of 2,021
of 2,021 non-equivalent faults.
