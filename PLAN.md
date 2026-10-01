# Loom: master plan

Jane Street protocol emulator ASIC competition. Deadline Monday, January 18, 2027.
Target: IHP 130 nm CMOS5L through Tiny Tapeout, 6x4 tiles, March 2027 shuttle.

Working name is "Loom" (threads through pins). Top module `tt_um_ahan17x_loom`.
Rename is a find-and-replace; nothing depends on it.

## 1. What we are building

A small chip that bit-bangs wire protocols from firmware. A host (the RP2350
on the Tiny Tapeout demo board) loads a program over SPI, then two hardware
threads run it with cycle-exact timing against the chip's 24 I/O pins.
UART, SPI and I2C are the required protocols. Low-speed USB and 10BASE-T
Ethernet are stretch goals.

Pitch, in one paragraph: PIO (RP2040) is cycle-exact but has no ALU and 32
instructions shared by 4 state machines; the PRU (TI) is a real CPU but gets its
timing from running at 200 MHz. Loom is in between: a real 16-bit CPU with a
pin-oriented instruction set, two barrel-interleaved hardware threads so a
full-duplex protocol is two straight-line programs instead of one interleaved
one, hardware timers that keep bit edges phase-locked no matter how many
instructions the loop has, and a 256-word SRAM program memory. Every
instruction takes exactly one thread slot. There are no caches, no stalls and
no interrupts, so timing can be read off the program listing.

## 2. Decisions taken (override any of these)

| Decision | Choice | Why |
|---|---|---|
| HDL | Verilog-2005 | Template requires a Verilog top; keeps the GitHub flow unmodified. Hardcaml port is an optional later bonus. |
| Clock | Design for 60 MHz (16.7 ns), run at 50 MHz by default | 60 MHz divides USB low-speed (40 cycles/bit) and 10BASE-T (3 cycles/half-bit) evenly. Template default is 50 MHz; raise once timing closes. |
| Core | 16-bit datapath, 16-bit instructions, 8 registers per thread | Bytes are the common case; 16 bits holds timers, CRC-16 and counters. |
| Threads | 2 hardware threads, barrel-interleaved (T0 on even cycles, T1 on odd) | Each thread gets a hazard-free, zero-branch-penalty machine at 1 instruction per 2 cycles. Full duplex becomes trivial. Area cost is one extra register file and PC. |
| Program memory | 256 x 16 SRAM macro `RM_IHPSG13_1P_256x16` (28,100 um^2, under 1 tile) | Flip-flops for the same memory would be ~275,000 um^2, over a third of the chip. Simulation uses a flop model with the same interface. FPGA uses block RAM. |
| Host interface | SPI slave (mode 0) on ui[0..2] + uo[0], IRQ line on uo[1] | 5 pins. Leaves 19 pins for protocols. Easy to drive from the demo board's RP2350. Host SCK up to clk/8. |
| Pin model | 24-pin flat space: 0-7 uio (bidir, per-pin push-pull or open-drain mode), 8-15 ui (inputs), 16-23 uo (outputs) | Open-drain mode makes I2C/1-wire safe by construction: in OD mode the pad can never be driven high. That becomes a formal property. |
| Timing primitives | Per-thread 16-bit timer with sticky tick + WAITT; WAIT on pin level/edge; DELAY | Bit edges land on timer ticks, not on instruction counts, so loops can vary in length without drift. |
| Data path to host | Per-thread 8-bit FIFOs each way (depth 16) with blocking PUSH/POP | Firmware stays simple: `pop r0` waits for the host. |
| Verification | Python ISS is the golden model; cocotb runs the same program on RTL and compares retired instructions and pin traces; SymbiYosys formal on the FIFO, timer, pin unit and host interface; constrained-random programs | This is what the brief says it judges. |

All inputs are double-synchronised (2 cycles of latency), since protocol signals
are asynchronous to the core clock.

## 3. Scope

MVP (must ship, and must always pass the full GDS flow):

1. ISA spec, assembler, instruction-set simulator.
2. RTL: FIFO, timer, pin unit, 2-thread core, SRAM wrapper + model, SPI host interface, top.
3. Firmware: UART TX/RX, SPI master (modes 0-3), I2C master (start/stop/ack, clock stretching), all exercised against protocol checkers in simulation.
4. Tests: cocotb unit tests; ISS-vs-RTL lockstep; protocol decoders on simulated waveforms; formal proofs on the small blocks.
5. GDS at 6x4 through the template's GitHub Action, timing clean at the target clock.
6. Docs: `docs/info.md` datasheet, ISA reference, verification write-up, demo-board Python driver.

Stretch, in order:

1. Programmable CRC engine (any polynomial up to 32 bits). Covers USB, Ethernet, CAN.
2. Timestamped edge capture ("logic analyser mode"): record (delta-t, pins) on any change of a pin mask into a FIFO the host drains. Aimed at Jane Street's debugging and reverse-engineering use.
3. Serializer engine: shifts bytes out/in at a programmed bit period with NRZI or Manchester coding and bit stuffing. Needed for 10BASE-T; makes USB LS comfortable.
4. USB low-speed device (SETUP/IN/OUT, CRC5/CRC16, bit stuffing).
5. 10BASE-T transmit (link pulses, preamble, Manchester, CRC32). Receive only if time allows.
6. Hardcaml version of the core (Jane Street's own tool; a strong signal).

Deliberately out: data memory (registers + FIFOs are enough for these protocols), interrupts, multiply.

## 4. Area budget (from the CMOS5L liberty file)

- 6x4 tiles = 24 x 200 um x 150 um = 720,000 um^2. At the template's 60% placement density, roughly 430,000 um^2 of cells.
- Flip-flop with reset: 49 um^2. mux2: 18 um^2. NAND2: 7.3 um^2.
- SRAM macro 256x16: 28,127 um^2.
- Estimated core: ~1,200 flops (two register files 256, PCs/flags/LR ~40, timers 64, four FIFOs 512, host interface ~120, pin unit ~110, sync ~50) = ~60,000 um^2 of flops plus logic. Expect 150,000-200,000 um^2 total. Leaves room for every stretch item.

Yosys will be run against the real liberty file after each RTL milestone and the numbers logged in WORKLOG.md.

## 5. Milestones

| Date | Milestone |
|---|---|
| Oct 1-5 | Plan, ISA v0.1, assembler + ISS with tests, first RTL modules. |
| Oct 6-12 | UART/SPI/I2C firmware passing on the ISS. ISA frozen (v1.0). Core RTL complete, lockstep test passing. |
| Oct 13-19 | Host interface, top, cocotb protocol tests. First Yosys area and timing numbers. Repo on GitHub; GDS action green at 6x4 with the flop model. |
| Oct 20-26 | SRAM macro integrated in the flow (copy the config from the Tiny Tapeout SRAM test project). Formal proofs. |
| Oct 27-Nov 9 | FPGA bring-up against real devices (Ahan). CRC engine, capture buffer. |
| Nov 10-30 | Serializer engine, USB low-speed. Timing closure at 60 MHz. |
| Dec 1-20 | 10BASE-T transmit. Datasheet, verification write-up, demo-board driver. Hardcaml port if time. |
| Dec 21-Jan 10 | Freeze. Gate-level simulation, post-layout checks, final GDS. |
| Jan 11-18 | Buffer. Submit through the form Jane Street will add to the post. |

Rule: `main` always passes tests and the GDS flow. Stretch work lives on branches until it does too.

## 6. What needs Ahan

- Create a public GitHub repo from the CMOS5L template, push, enable Actions and Pages. (The GDS flow runs there; it cannot run in this workspace.)
- Fill out the sign-up form. Find 1-2 teammates; verification and FPGA/bench testing are the natural split.
- Decisions to confirm or change: project name, 2 threads vs 4, 256 vs 512 words of program memory, SPI as the host interface.
- Review each module as it lands. The repo will say openly that the design was built with AI assistance; the brief invites that, but the design choices have to be yours to defend.
- Bench work: FPGA prototype, logic analyser captures, real UART/SPI/I2C devices, later USB and Ethernet.
- Ask on the Tiny Tapeout Discord whether the 256x16 SRAM macro is supported on the CMOS5L shuttle flow (the PDK tree says yes; the flow config is the question).

## 7. Risks

- SRAM macro in the flow: fallback is a 128 x 16 latch-based memory (~100,000 um^2). The imem module interface is the same either way.
- Timing at 60 MHz: fallback is 50 MHz; only Ethernet timing becomes awkward.
- Host SPI edge-detection latency: documented limit of SCK <= clk/8; the RP2350 can do that easily.
- 10BASE-T receive is hard (clock recovery, Manchester decode at 20 MHz); treat it as optional.
- Scope creep: every feature after the MVP must keep `main` green.
