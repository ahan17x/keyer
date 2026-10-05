![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/check/badge.svg)

# Keyer: a protocol emulator ASIC

Entry for the [Jane Street protocol emulator ASIC competition](https://blog.janestreet.com/protocol-emulator-asic-competition/).
IHP 130 nm CMOS5L through Tiny Tapeout, 6x4 tiles.

Keyer is a two-thread, 16-bit, pin-oriented CPU, named after the telegraph
keyer: a program becomes precisely timed marks and spaces on a wire. A host
loads a program over SPI; the threads bit-bang UART, SPI, I2C, JTAG, SWD,
PS/2, WS2812 (and whatever else fits the timing) on the chip's 24 pins with cycle-exact timing. See [docs/info.md](docs/info.md)
for the overview and [docs/isa.md](docs/isa.md) for the instruction set.

## Layout

| Path | What |
|---|---|
| `src/` | Verilog RTL. `tt_um_ahan17x_keyer.v` is the top. `keyer_isa.vh` is generated. |
| `docs/` | Datasheet source (`info.md`), the ISA reference (`isa.md`), the cycle-exact contract (`SEMANTICS.md`), decisions and bug ledger. |
| `tools/keyer_isa.py` | Encoding table, the single source of truth for opcodes. |
| `tools/keyerasm.py` | Assembler. |
| `tools/keyersim.py` | Cycle-exact instruction-set simulator (the golden model). |
| `tools/protomodels.py`, `tools/protomodels_*.py` | Protocol models used by the tests: each knows only its protocol (UART, SPI slave and master, I2C slave and master, JTAG TAP, SW-DP target, PS/2 device, WS2812 decoder). |
| `tools/mutate.py` | Mutation testing of the RTL (`tools/mutate_equivalents.md` lists the equivalent mutants). |
| `tools/keyerhost.py` | Host driver: library and command line, on the demo board (MicroPython) and on the simulation. |
| `tools/test_*.py` | pytest suites for the assembler, ISS and firmware. |
| `fw/` | Firmware: `uart.s`, `spi_master.s`, `i2c_master.s`, `capture_demo.s`, `spi_slave.s`, `i2c_slave.s`, `jtag_master.s`, `swd.s`, `ps2_host.s`, `ws2812.s`. |
| `test/` | cocotb tests, including the ISS-vs-RTL lockstep harness. |
| `synth/` | Yosys area-estimate scripts against the CMOS5L liberty. |
| `formal/` | SymbiYosys proofs (FIFO, pin unit, core, capture and replay) and `equiv_core.sh`, the Yosys equivalence check of the core against a git reference. |
| `fpga/alhambra2/` | Build for the Alhambra II (iCE40 HX4K): wrapper, pins, build and post-synthesis simulation scripts. A synthesis and post-synthesis-simulation result only: it has not been run on a board and no hardware bring-up is planned (D-035). |

## Running the tests

```sh
pip install cocotb pytest
python3 -m pytest tools/ -q          # assembler, ISS, firmware on the ISS
cd test && make                      # cocotb: host interface + lockstep (needs iverilog)
```

Regenerate the Verilog opcode header after editing the ISA table:

```sh
python3 tools/keyer_isa.py --vh > src/keyer_isa.vh
```

## Verification approach

`docs/SEMANTICS.md` defines the behaviour cycle by cycle. The Python model
(`tools/keyersim.py`) and the RTL are written from it independently, by
sessions that cannot read each other's side; `test/keyer_tb.py` then runs the
model in lockstep with the RTL from reset, with every host action (program
load, run/stop, FIFO traffic, pin modes) mirrored from the RTL into the model.
Protocol correctness is checked by independent models that know only the
protocol, not the firmware. Constrained-random instruction streams run on both
threads with random pin activity.

The layers, what each one found, what each would miss and the commands
that reproduce every number are in [docs/VERIFICATION.md](docs/VERIFICATION.md).
`bash scripts/check_all.sh` runs everything except the mutation campaign,
which is the `mutation` workflow on GitHub (started by hand).

## Status

See `WORKLOG.md` for the running log and `PLAN.md` for the plan. This design is
being built with AI assistance (Claude), with the design decisions reviewed
and owned by the author.

## License

Apache-2.0.
