# Review of a parallel entry: thomasgilbert481/tt_um_loom (2026-10-02)

Reference material only. Read when deciding positioning (D-016) or
configuring the SRAM macro flow. Nothing here is to be copied into the
design; facts and flow infrastructure may be used with attribution.

Both projects were built with the same model family, which is why the names
(Loom, loomasm, loomsim, protomodels) and the first-level architecture
coincide. Theirs started 2026-09-14 with a director session plus parallel
Opus subagents, each confined to a directory, and the rule that the model
and RTL authors may not read each other's code. Status at review: hardware
declared complete, hardened at 6x4, DRC/LVS clean, precheck passing, RTL
freeze 2026-11-08.

| | Theirs | Ours |
|---|---|---|
| Threads / pipeline | 4 threads, 4-stage; 12.5 MIPS per thread at 50 MHz | 2 threads, 2-stage; 25 MIPS per thread |
| Edge timing | dither 0-3 clocks; `SETP pin,v,D` latches a write to the exact deadline clock | dither 0-1 clock; no latch |
| Timing model | NOW/TD deadline register (`WAITD k`, `SETD`), fractional tick prescaler, timeout on every wait | sticky tick, `WAITT`/`SETT`; no timeouts |
| Bit engine | per thread: shifter, CRC-16, NRZ/NRZI/Manchester, USB and CAN stuffing, differential out | none yet |
| Memory | 512x16 macro in the flow, precheck-clean; `LD`/`ST` into it | 256x16 macro planned |
| Other | 2-entry return stack, CSRs, shared flags, pin groups, host debug port (halt/step/read registers) | `RDLR`/`JMPR`, fixed uio byte I/O |
| Size | 31,393 cells, 3,227 flops, 54.7% utilisation | 6,410 cells, 1,190 flops, ~13% |
| Hardening | 4-5 h per run against GitHub's 6 h limit; routing on Metal1-4 is the binding constraint | expected minutes |
| Clock | 50 MHz typical (+5.4 ns slack); slow corner ~42 MHz | 60 MHz target, unproven |
| Firmware | 15 programs incl. USB LS device, CAN, JTAG, SWD, PS/2, WS2812 | UART, SPI master, I2C master |
| Verification | independent model/RTL authorship; lockstep; 19 formal properties + thread-isolation miter; mutation testing (823 faults); gate-level in CI; static deadline checker in the assembler; bug ledger | same-session model/RTL; lockstep; 3 formal groups |

Facts corrected from their research (`docs/tt_cmos5l_facts.md` in their repo):
6x4 block = 1289.28 x 710.64 um (916,214 um^2; core 902,417 um^2); routing
on Metal1-4 only; organisers confirmed by email (2026-09-28) that an IHP
SRAM macro may go on the shuttle and Tiny Tapeout is preparing a reference
macro template; 8x4 is in the tools since 2026-09-21 but the organisers say
design to 6x4. Their section 11 is a working macro placement recipe (MACROS
key, PDN stripes through the macro's power columns, Magic waivers).

Pitfalls from their decision log worth knowing: the `gds` workflow needs a
paths filter so docs commits do not start four-hour runs (their D-018);
routing time is superlinear in congestion (D-022/D-025); an open-drain pin
could drive high depending on register write order until the pads applied
the mask (D-023, found by formal); a reset-vector default collided across
memory sizes (D-017).

What they do not have: capture/replay or any logic-analyser function; an
FPGA prototype (dropped); Hardcaml (rejected, D-009).
