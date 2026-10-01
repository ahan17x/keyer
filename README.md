![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg)

# Loom: a protocol emulator ASIC

Entry for the [Jane Street protocol emulator ASIC competition](https://blog.janestreet.com/protocol-emulator-asic-competition/).
IHP 130 nm CMOS5L through Tiny Tapeout, 6x4 tiles.

Loom is a two-thread, 16-bit, pin-oriented CPU. A host loads a program over
SPI; the threads bit-bang UART, SPI, I2C (and whatever else fits the timing)
on the chip's 24 pins with cycle-exact timing. See [docs/info.md](docs/info.md)
for the overview and [docs/isa.md](docs/isa.md) for the instruction set.

## Layout

| Path | What |
|---|---|
| `src/` | Verilog RTL. `tt_um_ahan17x_loom.v` is the top. `loom_isa.vh` is generated. |
| `docs/` | Datasheet source (`info.md`) and the ISA reference (`isa.md`). |
| `tools/loom_isa.py` | Encoding table, the single source of truth for opcodes. |
| `tools/loomasm.py` | Assembler. |
| `tools/loomsim.py` | Cycle-exact instruction-set simulator (the golden model). |
| `tools/protomodels.py` | UART / SPI / I2C protocol models used by the tests. |
| `tools/test_*.py` | pytest suites for the assembler, ISS and firmware. |
| `fw/` | Firmware: `uart.s`, `spi_master.s`, `i2c_master.s`. |
| `test/` | cocotb tests, including the ISS-vs-RTL lockstep harness. |
| `synth/` | Yosys area-estimate scripts against the CMOS5L liberty. |

## Running the tests

```sh
pip install cocotb pytest
python3 -m pytest tools/ -q          # assembler, ISS, firmware on the ISS
cd test && make                      # cocotb: host interface + lockstep (needs iverilog)
```

Regenerate the Verilog opcode header after editing the ISA table:

```sh
python3 tools/loom_isa.py --vh > src/loom_isa.vh
```

## Verification approach

`tools/loomsim.py` defines the behaviour; the RTL is checked against it cycle
by cycle from reset by `test/loom_tb.py`, with every host action (program
load, run/stop, FIFO traffic, pin modes) mirrored from the RTL into the model.
Protocol correctness is checked by independent models that know only the
protocol, not the firmware. Constrained-random instruction streams run on both
threads with random pin activity.

## Status

See `WORKLOG.md` for the running log and `PLAN.md` for the plan. This design is
being built with AI assistance (Claude), with the design decisions reviewed
and owned by the author.

## License

Apache-2.0.
