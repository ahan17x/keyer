![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/check/badge.svg)

# Keyer: a protocol emulator ASIC

Entry for the [Jane Street protocol emulator ASIC competition](https://blog.janestreet.com/protocol-emulator-asic-competition/).
IHP 130 nm CMOS5L through Tiny Tapeout, 6x4 tiles.

Keyer is a two-thread, 16-bit, pin-oriented CPU, named after the telegraph
keyer: a program becomes precisely timed marks and spaces on a wire. A host
loads a program over SPI; the threads bit-bang UART, SPI, I2C (and whatever
else fits the timing) on the chip's 24 pins with cycle-exact timing. See [docs/info.md](docs/info.md)
for the overview and [docs/isa.md](docs/isa.md) for the instruction set.

## Layout

| Path | What |
|---|---|
| `src/` | Verilog RTL. `tt_um_ahan17x_keyer.v` is the top. `keyer_isa.vh` is generated. |
| `docs/` | Datasheet source (`info.md`), the ISA reference (`isa.md`), the cycle-exact contract (`SEMANTICS.md`), decisions and bug ledger. |
| `tools/keyer_isa.py` | Encoding table, the single source of truth for opcodes. |
| `tools/keyerasm.py` | Assembler. |
| `tools/keyersim.py` | Cycle-exact instruction-set simulator (the golden model). |
| `tools/protomodels.py` | UART / SPI / I2C protocol models used by the tests. |
| `tools/keyerhost.py` | Host driver: library and command line, on the demo board (MicroPython) and on the simulation. |
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

## Status

See `WORKLOG.md` for the running log and `PLAN.md` for the plan. This design is
being built with AI assistance (Claude), with the design decisions reviewed
and owned by the author.

## License

Apache-2.0.
