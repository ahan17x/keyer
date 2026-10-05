# Verification

What is checked, by which layer, what each layer has found so far (rows of
`docs/BUGS.md`), what each would miss on its own, and the command that
reproduces every number. Companion to `docs/AREA.md`. State: 2026-10-04,
after session 4.

The reference is `docs/SEMANTICS.md`, the cycle-exact contract. The golden
model (`tools/keyersim.py`) and the RTL (`src/`) are written from it by
sessions that cannot read each other's side (CLAUDE.md, "Independence");
everything below compares one of them, or both, with the contract or with
each other.

`bash scripts/check_all.sh` runs layers 1 to 6 (about seven minutes).
Layers 7 and 8 run on GitHub.

## The layers

| # | Layer | What it compares | Size | Command |
|---|---|---|---|---|
| 1 | Lint and compile | the RTL against the language rules | Verilator `-Wall`, Icarus `-g2005`, header freshness | `bash scripts/check_all.sh quick` |
| 2 | Model tests | the golden model, the assembler and the encoding table against SEMANTICS and isa.md | 406 pytest cases (87 on the model, the serializer scenarios and the host driver, 6 on the mutation tool, 313 firmware cases counted in layer 3) | `python3 -m pytest tools/ -q` |
| 3 | Protocol models | every firmware program against a model of its peer that knows only the protocol | 12 programs, 11 models, 313 pytest cases | `python3 -m pytest tools/test_fw.py tools/test_fw_*.py -q` |
| 4 | Lockstep and host tests | the RTL against the golden model, every cycle from reset, with all host traffic mirrored; the host interface through the pads; the serializer unit bench without the model | 71 cocotb tests in 14 modules; `test/ser_unit` | `cd test && make`; `bash test/ser_unit/run.sh` |
| 5 | Formal | the RTL against properties stated from SEMANTICS, for all inputs | FIFO, pin unit, core (timer, control, thread select), capture and replay, serializer (stuffing, CRC, round trip) | `cd formal && yowasp-sby -f pins.sby && yowasp-sby -f fifo.sby && ./run_core_pdr.sh && yowasp-sby -f capture.sby && yowasp-sby --yosys yowasp-yosys -f ser.sby` |
| 6 | Equivalence | a restructured core against the core it replaces | every flop input and output bit | `bash formal/equiv_core.sh GIT_REF` |
| 7 | Gate level | the hardened netlist (and the FPGA netlist) against the pads-only tests | the 13 pads-only tests (10 in the runs logged so far, which predate three of them) | `gl_test` job of the `gds` workflow; `bash fpga/alhambra2/sim.sh` |
| 8 | Mutation | the test suite and the proofs against single-line faults in the RTL | the full campaign of 1,542 mutants on GitHub (2026-10-05, before the serializer); every survivor processed | the `mutation` workflow; `python3 tools/mutate.py run --only ID,... -j 4` |

### 1. Lint and compile

Verilator `-Wall` (with `DECLFILENAME` and `UNUSEDSIGNAL` off) and an Icarus
compile of the eight source files; `src/keyer_isa.vh` must equal the output
of `tools/keyer_isa.py --vh`; every firmware file must assemble.

Found: nothing that reached the bug ledger; it is the gate that keeps width
and declaration mistakes out of the other layers. Would miss: everything
functional. It is not counted as a kill in mutation testing (D-031).

### 2. Model tests

`tools/test_iss.py` pins the golden model to SEMANTICS instruction by
instruction and rule by rule (slots, synchroniser latency on both thread
parities, timer, timeouts, FIFOs, capture and replay); `tools/test_keyerhost.py`
runs the host driver against a pin-level fake chip.

Found: BUGS 8's regression test lives here (the model's synchroniser had
one cycle of latency instead of two). Would miss: any error the model and
its tests share, which is why the model was re-derived from the spec by a
session that never saw the first one (D-012; zero cycles of disagreement
afterwards) and why layer 4 exists.

### 3. Protocol models

| Firmware | Words | Model (knows only the protocol) | Cases |
|---|---|---|---|
| `uart.s` | 31 | 8N1 decoder and stimulus with baud error | 4 |
| `spi_master.s` | 17 | SPI mode 0 slave | 1 |
| `i2c_master.s` | 139 | 24Cxx-style slave with clock stretching | 4 |
| `capture_demo.s` | 37 | the same I2C slave, twice | in layer 4 |
| `spi_slave.s` | 55 | SPI mode 0 master: set-up and hold of MISO, MISO off the bus when deselected, aborts | 18 |
| `i2c_slave.s` | 86 + 2 per data byte | I2C master: START/STOP rules, stretching with a limit, ACK bookkeeping | 35 |
| `jtag_master.s` | 67 | IEEE 1149.1 TAP: 16-state controller, IR, IDCODE, BYPASS, set-up/hold, arbitrary power-up state | 88 |
| `swd.s` | 117 | ADIv5 SW-DP target: dormant start, line reset, select sequence, request parity, ACK, turnarounds, contention | 27 |
| `ps2_host.s` | 79 | PS/2 device: both directions, parity, inhibit, abort and retransmit | 34 |
| `ws2812.s` | 33 | WS2812B decoder: high time and period of every bit against the datasheet windows, reset gap | 18 |

Each program fits the 256-word memory with room for what must share it
(the I2C slave's table of up to 64 bytes takes 128 words).

Found: BUGS 1, 4, 5, 6 (firmware: a clobbered carry, a short SCK period, a
long start bit, a wrong status byte); 20 to 25 (a tick between `SETD 0`
and `WAITD` shortening a clock phase in two programs, the SWD write parity,
a late MISO release, an undocumented idle time). The models had their own
bugs: 2, 3, 29; and the tests theirs: 7, 26, 27, 28. Would miss: an
instruction that model and RTL both get wrong in the same way is invisible
here (layer 4 would not see it either; layers 2 and 5 are the guard), and a
protocol rule the model's author misread. Not checked against real devices
yet.

### 4. Lockstep and host tests

`test/keyer_tb.py` steps the golden model once per RTL clock from reset and
compares the drive registers, PCs, timers, running state, the capture and
replay state, the executing instruction and whether it committed, and the
register file and flags after every commit; every host effect seen at the
RTL's host interface is mirrored into the model in the same cycle. The
firmware of layer 3 runs this way with its protocol models, plus directed
programs, constrained-random instruction streams on both threads with pin
noise, and (new) undefined encodings and the status word in every FIFO
state. The pads-only tests drive the host SPI and the protocol models from
the pads alone, so they also run on a netlist.

Found: BUGS 8 (the model, not the RTL), 14 (found by a functional check
next to the lockstep: the comparison cannot see a wrong program load), 15
(by the model's author asking about reserved pins). Would miss: what both
sides get wrong alike; anything the programs do not exercise (layer 8
measures that); a host action sent outside the lockstep loop; wrong
behaviour confined to states the harness does not compare (it compares what
SEMANTICS section 13 lists).

### 5. Formal

| Proof | Properties | Engine, time |
|---|---|---|
| `fifo.sby` | occupancy bookkeeping (k-induction); first-in first-out data order (BMC to depth 20, `DATA_CHECK`) | z3, about 45 s |
| `pins.sby` | an open-drain pin is never driven high; `uo[1:0]` untouched; synchroniser latency exactly two cycles; edge history exactly two cycles | z3, 1 s |
| `run_core_pdr.sh` | timer T1 to T8, control P3 to P7, thread select S1 and D1; on `waitd-csa` also T9 (a thread that executes and does not complete changes no register, flag or LR and sends nothing out) and P8 (START and STOP act on the other thread only) | abc PDR, 5 to 9 s |
| `capture.sby` | C1 to C10 (overflow never silent, writes inside the buffer, replay timing exact for every delta, data integrity, port discipline) and six covers | z3, 1 s |
| `ser.sby` | the stuffer never emits seven consecutive ones, in either mode, and an NRZI frame that starts from J never holds the line for seven symbol ticks (all inputs free); `crc_m` equals an independent bit-serial reference throughout a frame of any length and the appended bits are its complement; known answers for a symbolic 2-byte message under CRC-16 and CRC-32; a transmitter instance through the pin synchroniser into a receiver instance delivers the sync-framed byte, the CRC bytes and a good verdict (NRZI with stuffing at T = 4, Manchester at T = 2; one message byte) | abc pdr (unbounded) for stuffing and CRC, abc bmc3 to depth 88 to 200 for the rest, z3 covers; about 2 min |

Found: the saturated-counter underrun rule of the replay engine (spec
question Q16, resolved into SEMANTICS 14.7). The layer's own
infrastructure failed three times: BUGS 10 (a proof compiled out), 11 (a
property file deleted), 19 (a failed proof passing the script); each has a
check now. The serializer's wire-level stuffing property found a precondition the spec had not stated (a frame sent after a `SERCFG` abort that left K on the pair starts with a non-transition; SEMANTICS 15.3 now says so, BUGS 41); seven seeded faults each fail a task. The round trip is proved for one message byte only; longer frames rest on the lockstep and model tests. Would miss: anything not stated as a property (the host
interface and the top have none); a property proved on a small instance
that fails on the real size (the FIFO is proved at depth 4); a property
that follows the same misreading of the spec as the RTL. In the mutation
sample a proof was the first failing check for 1 of 128 killed mutants;
five capture mutants made the induction stop closing without a
counterexample, which the tool does not count (D-031).

### 6. Equivalence

`formal/equiv_core.sh REF` proves with Yosys that the working-tree
`keyer_core` has the same flops as the core at a git reference and that,
from equal state and inputs, every flop input and output bit is equal.
Used for the one-hot thread select (D-028: 776 of 776 points against the
`sram-macro` core; that session's script was not kept) and for the
carry-save `WAITD` comparison (533 of 533 points against master, 7 s:
`bash formal/equiv_core.sh master` on `waitd-csa`). A seeded off-by-one in
the new adder leaves points unproven, so the script can fail.

Found: in session 3, two faults the core properties of the time would have
let through (a blocked timeout-form wait writing C; START acting on its
own thread); they became properties T9 and P8. Would miss: a fault present
in both versions; anything outside the core.

### 7. Gate level

The `gl_test` job simulates the hardened netlist with the PDK cell models
and the vendored macro model and runs the tests that need only the pads
(host interface, the driver's self-tests, UART loopback, capture and
replay demo); the lockstep tests skip. `fpga/alhambra2/sim.sh` does the
same on the Yosys iCE40 netlist, where the program memory and the four
FIFOs are block RAMs. That is the whole of the FPGA result: synthesis, place
and route, and this simulation; the bitstream has not run on a board
(D-035).

Found: two faults of the test configuration on the first run (a missing
primitive file, power pins the netlist does not have; docs/AREA.md), none
in the design. Would miss: timing (STA covers it, docs/AREA.md); anything
the pads-only tests do not do.

### 8. Mutation

`tools/mutate.py` makes single-line faults in `src/` (operator swaps,
removed negations, flipped constants, inverted conditions, stuck-at on
every `if` condition, enable-like assignment and enable-like instance
port), runs the checks of layers 4 and 5 fastest first and stops at the
first that fails. A kill is a test failure reported in `results.xml` or a
counterexample from a proof, nothing else (D-031). Its own tests
(`tools/test_mutate.py`) include controls for the equivalence step.

**Sample of 2026-10-04** (150 of the 1,535 mutants, seed 1, master at
9838914 plus the protocol tests; rows in `docs/mutation_sample.jsonl`):

| File | Mutants | Killed | Survived | Equivalent | Error | Score |
|---|---|---|---|---|---|---|
| `keyer_capture.v` | 37 | 36 | 0 | 1 | 0 | 100.0% |
| `keyer_core.v` | 34 | 33 | 0 | 1 | 0 | 100.0% |
| `keyer_fifo.v` | 2 | 2 | 0 | 0 | 0 | 100.0% |
| `keyer_host.v` | 45 | 40 | 0 | 5 | 0 | 100.0% |
| `keyer_isa.vh` | 14 | 12 | 0 | 2 | 0 | 100.0% |
| `keyer_pins.v` | 9 | 9 | 0 | 0 | 0 | 100.0% |
| `tt_um_ahan17x_keyer.v` | 9 | 9 | 0 | 0 | 0 | 100.0% |
| **all** | **150** | **141** | **0** | **9** | **0** | **100.0%** |

| Operator | Mutants | Killed | Survived | Equivalent | Error | Score |
|---|---|---|---|---|---|---|
| op | 28 | 28 | 0 | 0 | 0 | 100.0% |
| neg | 6 | 5 | 0 | 1 | 0 | 100.0% |
| const | 50 | 43 | 0 | 7 | 0 | 100.0% |
| cond | 14 | 14 | 0 | 0 | 0 | 100.0% |
| stuck | 52 | 51 | 0 | 1 | 0 | 100.0% |

How it got there. The first pass, against the suite as it stood (26 cocotb
tests), killed 128 and left 22: 15 survivors, 2 proved equivalent by Yosys,
and 5 capture mutants stopped at a proof that no longer closed (then
treated as an error, now as inconclusive). Of those 22:

- 13 were faults nothing looked at. Two fell to tests that already existed
  once the inconclusive proof no longer ended the run, two to the new SPI
  slave and I2C slave lockstep tests (`OEF` had never been executed; the
  outbox-empty status bit), and nine needed new tests, all in
  `test/test_corners.py`:

  | Test | Fault it kills |
  |---|---|
  | `test_lockstep_undefined_encodings` | an undefined ALU1 sub-opcode writing Z |
  | `test_lockstep_status_word_fifo_bits` | `RDS` bit 1 (inbox full) stuck at 0 |
  | `test_thread1_fifos_from_the_host` | the INBOX1 register address decoded as OUTBOX1's: no test had written thread 1's inbox over SPI |
  | `test_two_byte_registers_take_exactly_two_bytes` | CAP_CFG and CAP_BUF written after every byte instead of after byte 1 |
  | `test_miso_is_low_outside_a_transaction` | MISO idling high (SEMANTICS 10.1 since D-033) |
  | `test_lockstep_capture_trigger_needs_a_transition` | the capture triggering on a level that already matched at ARM |
  | `test_lockstep_replay_stop_and_underrun` | the underrun conditions swapped; the prefetch not emptied on STOP or underrun |

- 9 are equivalent: 4 proved by Yosys (two opcode defines the RTL never
  uses, a default the decode always overrides, the `_unused` wire), 5
  listed with reasons in `tools/mutate_equivalents.md` (reset values and a
  reset condition in the host interface whose effect is overwritten before
  anything can read it).

Score of the sample after that: 141 killed of 141 non-equivalent. It is a
sample: in the first pass about one mutant in ten was a real gap, so the
other 1,385 mutants will hold more of them, fewer now that the new tests
exist. The full campaign is the `mutation` workflow (16 shards, started
from the Actions tab; about 51 s per mutant here with four workers, so
roughly 45 minutes a shard on a runner at half this machine's speed). Its
survivors get the same treatment: a test, or a row in
`tools/mutate_equivalents.md`.

**Full campaign of 2026-10-05** (GitHub run 37260274984, 16 shards, on
master a56aae4: the design before the serializer; rows in
`docs/mutation_full.jsonl`). As the runners reported it:

| File | Mutants | Killed | Not killed | Equivalent (Yosys) | Error |
|---|---|---|---|---|---|
| `keyer_capture.v` | 357 | 335 | 18 | 4 | 0 |
| `keyer_core.v` | 426 | 399 | 14 | 13 | 0 |
| `keyer_fifo.v` | 35 | 35 | 0 | 0 | 0 |
| `keyer_host.v` | 357 | 290 | 64 | 3 | 0 |
| `keyer_imem.v` | 15 | 7 | 8 | 0 | 0 |
| `keyer_isa.vh` | 98 | 85 | 1 | 12 | 0 |
| `keyer_pins.v` | 176 | 147 | 23 | 6 | 0 |
| `tt_um_ahan17x_keyer.v` | 78 | 67 | 7 | 4 | 0 |
| **all** | **1,542** | **1,365** | **135** | **42** | **0** |

No mutant ended in error. Every one of the 135 was then judged by the rule
of D-031 and re-run locally (only those mutants, with the new tests in the
suite):

| Verdict | Mutants |
|---|---|
| a test gap, now killed by a new test | 80 |
| equivalent, with a reason in `tools/mutate_equivalents.md` (41 new rows; 5 rows from the sample) | 46 |
| unresolved: SEMANTICS does not define the behaviour the mutant changes (D-038, open) | 9 |

**Score after processing: 1,445 killed, 88 equivalent, 0 errors, 9
survivors: 1,445 of 1,454 non-equivalent mutants, 99.4%.** The nine
survivors are listed in D-038 with what the RTL does; each becomes a kill
or an equivalent once Ahan decides the four questions.

The 19 new tests (`test/test_mut_host.py`, 9, eight of them pads-only so
they also run on the gate-level netlist; `test/test_mut_core.py`, 10):

| Test | What had not been looked at |
|---|---|
| `test_irq_follows_each_condition` | each IRQ condition and each IRQEN bit by itself (10 mutants) |
| `test_fifoclr_bits_and_extra_bytes`, `test_byte_0_registers_ignore_further_bytes` | which data byte of a write counts, per register (11) |
| `test_reads_of_ctrl_pins_pinout_and_unmapped_registers` | read-back bytes nobody had read (4) |
| `test_hard_reset_in_the_middle_of_activity` (both modules) | reset applied to non-zero state: every earlier test reset once at time zero (5) |
| `test_imem_data_writes_dropped_while_a_thread_runs`, `test_lockstep_replay_fetches_against_host_and_thread_start`, `test_capture_writes_while_the_host_reads_registers` | memory-port arbitration between host, engines and a starting thread (6) |
| `test_pp_returns_a_pin_to_push_pull` | `PP` had never been executed after `OD` (1) |
| `test_lockstep_pin_modes_and_reserved_pins` | `OEN`/`OEF`/`PP`/`OD` on pins in the other mode, PINMODE on driven pins, pin 24 (14) |
| `test_lockstep_replay_writes_every_group` | replay on every pin group (5) |
| `test_lockstep_delay_jmp_addi_and_soft_reset`, `test_lockstep_delay_count_cleared_under_a_rewritten_word`, `test_lockstep_delay_count_untouched_while_blocked` | the `DELAY` count against stops, soft reset and blocking (11) |
| `test_lockstep_capture_groups_and_idle_entries`, `test_lockstep_capture_queue_while_the_port_is_busy`, `test_lockstep_replay_underrun_at_saturation` | capture groups, idle entries at 4,095 cycles, the queue with the port busy, underrun at saturation (12, among them the 13 mutants on which the capture proof merely stopped closing) |
| `test_program_memory_macro_static_inputs` | the macro's `A_DLY` tie-off: the vendored model ignores it, the silicon does not (1) |

What the campaign says about the suite: the gaps were not in the datapath
(the lockstep comparison kills those) but in three places the sample had
only hinted at: reset of non-zero state, host-register corner bytes, and
pin and capture corners no firmware happens to reach. The 41 new
equivalents are reset values overwritten before anything reads them (the
host's strobes and shift registers, `level2` and the capture queue words
in the first cycles), and seven BIST inputs of the macro that are cut off
by `A_BIST_EN = 0` in the macro's own netlist. One documented equivalent
from the sample, `host-c6659575`, holds only if CS_n is high when reset is
released (question 2 of D-038).

Not yet mutated: `src/keyer_ser.v` and the lines the serializer changed in
the core, the pin unit and the top (the campaign ran on the design before
the merge). `keyer_ser.v` is in the tool's target list with its proof.

Which check kills first (the fastest failing check, so slow tests are
under-represented):

| Killing check (the fastest that fails) | Mutants |
|---|---|
| `test.test_capture_registers` | 34 |
| `test.test_lockstep_replay_host_waveform_and_loopback_capture` | 30 |
| `test_jtag_master.test_lockstep_jtag_slow_host` | 17 |
| `test.test_fifo_roundtrip_and_status` | 13 |
| `test.test_id_and_registers` | 7 |
| `test.test_lockstep_random_programs` | 5 |
| `test.test_pc_readback_and_soft_reset` | 5 |
| `test.test_pads_uart_loopback` | 4 |
| `test.test_lockstep_alu_and_branches` | 3 |
| `test.test_lockstep_capture_replay_demo` | 2 |
| `test_host.test_host_capture_replay_demo` | 2 |
| `test_corners.test_lockstep_replay_stop_and_underrun` | 2 |
| `test.test_lockstep_pins_timer_delay` | 2 |
| `test_host.test_host_selftests` | 2 |
| `test_corners.test_two_byte_registers_take_exactly_two_bytes` | 2 |
| `formal:pins` | 2 |
| `test_corners.test_lockstep_capture_trigger_needs_a_transition` | 1 |
| `formal:capture` | 1 |
| `test_corners.test_lockstep_status_word_fifo_bits` | 1 |
| `test_i2c_slave.test_lockstep_i2c_slave` | 1 |
| `test_corners.test_lockstep_undefined_encodings` | 1 |
| `test_corners.test_thread1_fifos_from_the_host` | 1 |
| `test_corners.test_miso_is_low_outside_a_transaction` | 1 |
| `test_ps2_host.test_lockstep_ps2_host` | 1 |
| `test_spi_slave.test_lockstep_spi_slave_filler_and_foreign_traffic` | 1 |

Would miss: faults the operators do not make (two-line faults, timing,
anything inside the macro); code under `KEYER_IMEM_FLOPS` is tested with
that define only; a mutant is judged against the checks of layers 4 and 5,
not against lint or the model tests.

## What no layer covers yet

- Real devices: no firmware has run against hardware and no hardware
  bring-up is planned (D-035). The FPGA build (`fpga/alhambra2/`) is a
  synthesis and post-synthesis-simulation result only: it has never been
  programmed into a board. Every protocol claim rests on the protocol
  models of layer 3.
- The host interface and the top level have no formal properties; they
  rely on layers 4, 7 and 8.
- The lockstep comparison trusts the mirroring of host effects; a fault in
  the harness that mirrored a wrong effect into the model consistently
  would hide the corresponding RTL fault.
- Timing at the slow corner is reported, not enforced (D-028).
- USB and 10BASE-T are checked against protocol models only: no USB host,
  hub, PHY or link partner has seen these waveforms, and the electrical
  layer (levels, edge rates, the transformer) is outside the simulation.
- The serializer and the lines it changed elsewhere have not been through
  mutation testing yet.
- Nine mutants of the host interface survive on behaviour SEMANTICS does
  not define (D-038).
