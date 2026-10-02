# Formal proofs

Tools: `pip install yowasp-yosys` (provides `yowasp-sby`), `apt install z3`,
`yosys-abc` (comes with yosys).

| File | Block | Properties | Result |
|---|---|---|---|
| `fifo.sby prove` | `keyer_fifo` | count tracks pushes minus pops; empty/full/count consistent; never both | proved (k-induction, DEPTH=4 instance) |
| `fifo.sby bmc` | `keyer_fifo` | an arbitrary pushed word is read out unchanged after exactly the entries ahead of it | BMC depth 20 |
| `pins.sby` | `keyer_pins` | open-drain pins are never driven high; uo[1:0] fixed; level = pad two cycles earlier; level2 = level two cycles earlier | proved (k-induction) |
| `run_core_pdr.sh` | `keyer_core` | tick never lost; disabled timer never ticks; PC moves only on commit or host write; threads start only via host or START; halted implies stopped; thread parity; blocked threads hold PC | proved (abc pdr) |

Run: `yowasp-sby -f fifo.sby`, `yowasp-sby -f pins.sby`, `./run_core_pdr.sh`.
The core properties live in `src/keyer_core.v` under `` `ifdef FORMAL``.
