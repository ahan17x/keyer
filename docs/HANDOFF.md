# Handoff: state of the project and the next task

Updated 2026-10-02. This is the first file a session reads. Keep it short:
what exists, what is decided, what is open, what to do next.

## What exists (all tests green)

- `docs/isa.md`: ISA v0.2 (85 instructions), pin space, timer, FIFOs, host
  register map. Not yet a cycle-exact contract; see task 2.
- `tools/keyer_isa.py` (encoding table, generates `src/keyer_isa.vh`),
  `tools/keyerasm.py` (assembler), `tools/keyersim.py` (cycle-exact golden
  model), `tools/protomodels.py` (UART/SPI/I2C models, host feeder),
  `tools/test_iss.py` + `tools/test_fw.py` (28 tests).
- `fw/uart.s` (full duplex on two threads), `fw/spi_master.s`,
  `fw/i2c_master.s` (bytecode from the host; repeated start; clock
  stretching). All verified against the protocol models.
- `src/`: `keyer_fifo.v`, `keyer_imem.v` (behavioural + SRAM macro wrapper
  under `KEYER_IMEM_SRAM`), `keyer_pins.v`, `keyer_core.v`, `keyer_host.v`,
  `tt_um_ahan17x_keyer.v`. Verilator `-Wall` clean.
- `test/`: cocotb, 9 tests: 3 host-interface, 6 lockstep (ISS vs RTL every
  cycle from reset, host actions mirrored). `test/keyer_tb.py` is the harness.
- `formal/`: FIFO (induction + BMC), pins (induction; open-drain never drives
  high), core (7 properties, abc pdr). See `formal/README.md`.
- `synth/`: Yosys against the CMOS5L liberty. Result: 6,410 cells, 1,190
  flops, 114,169 um^2 of standard cells + 28,127 um^2 SRAM macro. The 6x4
  block is 1289.28 x 710.64 um (core 902,417 um^2); routing is on Metal1-4.
- `info.yaml` at 6x4, `docs/info.md` datasheet draft, `README.md`.
- Not yet done: GitHub push and first `gds` run; SRAM macro flow config
  (`src/config.json` MACROS + PDN); demo-board driver; FPGA; stretch
  features (CRC engine, capture buffer, serializer, USB LS).

## Open decisions (docs/DECISIONS.md D-013 to D-016)

Rename; timeouts and timer model; 2 vs 4 threads; the differentiator. The
first session resolves these with Ahan before changing code.

## Next tasks, in order

1. **Resolve D-013 to D-016 with Ahan.** Write the outcomes as DECISIONS
   entries. Apply the rename (project name, `tt_um_` top module, info.yaml,
   README, test/Makefile, docs).
2. **Write `docs/SEMANTICS.md`**, the cycle-exact contract: for every
   instruction, what state changes on which clock, including the blocking
   rules, the timer, the synchroniser latency, pin write visibility, host
   effects. Use `docs/isa.md` and `tools/keyersim.py` as the starting point
   (the ISS is the current best statement of the semantics; the lockstep
   tests pin down the timing). From then on SEMANTICS wins.
3. **Re-derive one side independently** (D-012): with the `golden-model`
   subagent (cannot read `src/`), rewrite `tools/keyersim.py` from
   SEMANTICS alone, then run `cd test && make`. Every mismatch is decided by
   quoting SEMANTICS and logged in `docs/BUGS.md`.
4. **Push to GitHub, get the first `gds` run** at 6x4 with the flop memory,
   then with the macro. Record cells, utilisation, timing and routing time
   in WORKLOG.md. (The parallel entry's `docs/tt_cmos5l_facts.md` section 11
   documents a working macro placement recipe for this flow, Apache-2.0;
   Tiny Tapeout is also preparing an official macro template.)
5. Then the differentiator (D-016) and the stretch list in PLAN.md.

## Kickoff prompt for the first Claude Code session

The full prompt is in `docs/KICKOFF_PROMPT.md`. Paste it as the first
message of a `claude` session started in this directory. Before that, run
`bash scripts/setup_mac.sh` once (tools) and `bash scripts/check_all.sh`
(every test, about two minutes) to confirm the machine is set up.
