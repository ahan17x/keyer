# Formal proofs

Tools: `pip install yowasp-yosys` (provides `yowasp-sby`), `apt install z3`,
`yosys-abc` (comes with yosys).

| File | Block | Properties | Result |
|---|---|---|---|
| `fifo.sby prove` | `keyer_fifo` | count tracks pushes minus pops; empty/full/count consistent; never both | proved (k-induction, DEPTH=4 instance) |
| `fifo.sby bmc` | `keyer_fifo` | an arbitrary pushed word is read out unchanged after exactly the entries ahead of it | BMC depth 20 |
| `pins.sby` | `keyer_pins` | open-drain pins are never driven high; uo[1:0] fixed; level = pad two cycles earlier; level2 = level two cycles earlier | proved (k-induction) |
| `run_core_pdr.sh` | `keyer_core` | timer (T1-T8): a reached deadline completes WAITD k / a timeout-form wait in the same slot (exact completion rule); disabled timer never changes NOW; NOW moves only by a tick (+1) or to 0 on SETT / soft reset, and a due tick is never lost; DEADLINE moves only through SETD, completed WAITD, SETT, soft reset; WAITD k adds exactly k, SETD k sets NOW + k; timeout forms set C = !base, base forms leave C; prescale/period follow the tick rule; a timed-out wait pops, pushes and writes nothing. Control (P3-P7): PC moves only on commit or host write; threads start only via host or START; halted implies stopped; thread parity; blocked threads hold PC | proved (abc pdr, about 2 s) |

Run: `yowasp-sby -f fifo.sby`, `yowasp-sby -f pins.sby`, `./run_core_pdr.sh`.
The core properties live in `src/keyer_core.v` under `` `ifdef FORMAL``.
