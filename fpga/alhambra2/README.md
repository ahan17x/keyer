# Keyer on the Alhambra II

A build of the unmodified design for the Alhambra II (iCE40 HX4K-TQ144,
12 MHz oscillator) with Yosys, nextpnr-ice40 and icepack. The sources are the
ASIC's `src/` files with `KEYER_IMEM_FLOPS` defined, which selects the
behavioural program memory; it maps to one block RAM. `keyer_alhambra2.v` is
the board wrapper: pins, a power-on reset and nothing else.

Not yet run on the board.

## Build

```sh
bash fpga/alhambra2/build.sh             # synthesis, place and route, bitstream, summary
bash fpga/alhambra2/sim.sh               # the pads-only cocotb tests on the post-synthesis netlist
iceprog fpga/alhambra2/build/keyer.bin   # program the board (apio's iceprog works too)
```

The tools are taken from `PATH`, then from apio's package directory
(`~/.apio/packages/tools-oss-cad-suite/bin`, where `apio install
oss-cad-suite` puts nextpnr-ice40, icepack and icetime). `FREQ=24 bash
build.sh` constrains for another clock; `SEED=n` changes the placement seed.

## Result

Build of 2026-10-04 (Yosys 0.69, nextpnr 0.7, seed 1):

| Item | Value |
|---|---|
| Synthesis | 2,786 LUT4, 1,039 flip-flops, 412 carry cells |
| Block RAM | 5 of 32: the program memory and the four FIFOs |
| Placed logic cells | 3,557 of 7,680 (46% of the die) |
| I/O | 32 |
| Clock | 40.8 MHz achieved (nextpnr; icetime 39.2 MHz) against the 12 MHz required |
| Post-synthesis simulation | 10 of 10 host-interface and pads-only tests pass (`sim.sh`) |

The HX4K-TQ144 is the HX8K die in a smaller package. The open flow (and apio,
for this board) targets it as `--hx8k --package tq144:4k` and can use all
7,680 logic cells; the vendor tools limit the part to 3,520. The design
fits the die with room to spare; it does not fit the vendor's 3,520-cell
limit, which only matters if the vendor tools are used.

`sim.sh` synthesises the Tiny Tapeout top for the iCE40 without the wrapper
and runs the tests the ASIC's gate-level job runs (host interface, the host
driver's self-tests, UART loopback and the capture-and-replay demo through
the pads; the lockstep tests skip because they read RTL internals). It
checks what Yosys made of the memories: the program memory and the four
FIFOs are block RAMs here.

## Pins

| Board pin | Signal | Notes |
|---|---|---|
| D13 | SCK (`ui[0]`) | host SPI on the Arduino SPI positions |
| D11 | MOSI (`ui[1]`) | |
| D10 | CS_n (`ui[2]`) | |
| D12 | MISO (`uo[0]`) | |
| D0 .. D7 | `uio[0]` .. `uio[7]` | firmware pins 0-7, bidirectional; I2C firmware uses D2 (SCL) and D3 (SDA) and needs pull-ups |
| D8, D9 | `uo[2]`, `uo[3]` | firmware pins 18, 19 |
| DD0, DD1 | `uo[4]`, `uo[5]` | firmware pins 20, 21 |
| DD2, DD3, DD4 | `ui[3]`, `ui[4]`, `ui[5]` | firmware pins 11, 12, 13 |
| DD5 | RST_n | internal pull-up; drive low to reset |
| SW1 | `ui[6]` | firmware pin 14 |
| FTDI serial RX / TX | `ui[7]` / `uo[7]` | the board's USB serial port: assemble `fw/uart.s` with `RX = ui7`, `TX = uo7` for a UART on the PC |
| LED7 .. LED2 | `uo[7]` .. `uo[2]` | mirror of the firmware outputs; `uo[6]` is on LED6 only |
| LED1 | IRQ (`uo[1]`) | |
| LED0 | heartbeat | about 0.7 Hz while the clock runs |

## Using it

The core clock is 12 MHz, so timing constants are computed for 12 MHz
(`BAUD_DIV = 104` for 115200 baud; the host SPI may run up to clk/8 =
1.5 MHz). The host driver `tools/keyerhost.py` needs four GPIOs on any
MicroPython board: `BoardTransport(pins={"sck": .., "mosi": .., "csn": ..,
"miso": ..}, clock_hz=12000000)`, without calling `setup()` (that selects a
project on the Tiny Tapeout demo board). First check on the bench:
`python3 tools/keyerhost.py selftest` with nothing connected but the four
SPI wires and ground.

Things to verify on the board that the simulation cannot: the polarity of
SW1 (assumed high when pressed; it only feeds a firmware input), the level
of the header pins through the board's 5 V buffers, and that DD5 floats high
(otherwise tie it to 3.3 V).
