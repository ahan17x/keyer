# Equivalent mutants: host interface and top level (campaign run 37260274984, group A)

Survivors of the full campaign in `keyer_host.v` and `tt_um_ahan17x_keyer.v`
that no test can kill because no behaviour at the pads or in the state the
lockstep harness compares differs, for any input sequence (none of the
arguments below needs the SPI timing constraints of SEMANTICS 10.1). Same
format and rules as `tools/mutate_equivalents.md`.

Evidence beyond the argument: every row was also run in a differential
random simulation (both designs side by side in Icarus, the flop memory
with the same random contents, identical random pads: SPI traffic biased
toward the effectful registers at random speeds including faster than
clk / 8, transactions cut mid-byte, firmware pins, and hard resets of 1 to
6 cycles in the middle of activity), comparing every cycle the pads,
`running`, `halted`, `blocked`, both PCs, `od_mask`, the FIFO counts, the
capture/replay status, counts and configuration, IRQEN and the IMEM
address, and every 64 cycles the whole program memory and the core's
registers: 4 seeds x 200,000 cycles each, no difference. The same bench
reports a difference within a few hundred cycles for the killed controls
`host-499b9af2` and `host-9ba770f8`. It also reports a difference for the
row `host-c6659575` of `tools/mutate_equivalents.md` (and for
`host-6c196bbf`, not listed here): both differ only when the host holds
CS_n low across the release of a hard reset, which SEMANTICS does not
cover; with CS_n held high from two cycles before the end of reset the
bench finds no difference.

"Pulse in cycle 0" below: a write strobe that resets to 1 is high during
every reset cycle and during cycle 0, and the `else` branch clears it at
the end of cycle 0, so it lands exactly once, at the end of cycle 0, when
its target has just been reset too (core, FIFOs, pin unit and capture
engine all take `rst_n`); `wdata_q`, `imem_lo` and `rx_byte` are 0 then.

| Id | Where | Mutation | Why it is equivalent |
|---|---|---|---|
| `host-3095941f` | `keyer_host.v`, synchroniser reset | `mosi_s` resets to `3'b001` | `mosi_q = mosi_s[1]` is read only at an SCK rise while active; `active = ~csn_s[1]` is 0 in cycles 0 and 1 whatever the pads (`csn_s` resets to `111`), and from cycle 2 `mosi_s[1]` is the pad sampled in cycle 0: the reset bit has gone |
| `host-646c02e9` | `keyer_host.v`, synchroniser reset | `csn_s` resets to `3'b110` | `active` becomes 1 in cycle 1 instead of 2, but in cycle 1 `sck_s[2:1]` still hold reset zeros, so there is no SCK edge: the receive and transmit registers end cycle 1 with exactly the values their inactive reset gives (all 0, `miso` 0). `csn_s[2]` is never read, and from cycle 2 `csn_s[1:0]` are the pads in both designs |
| `host-914fba78` | `keyer_host.v`, receive reset (also while CS_n is high) | `shift_in` resets to `7'd1` | `shift_in` is read only into `rx_byte = {shift_in, mosi_q}` at the eighth rise of a byte; it is 7 bits wide and shifts once per rise, so after the seven earlier rises of the first byte none of the reset value is left (later bytes of the transaction are not reset) |
| `host-37c45961` | `keyer_host.v`, receive reset (also while CS_n is high) | `rx_byte` resets to `8'd1` | `rx_byte` is read (a) with `byte_done`, when it has just been loaded with the byte, (b) through `wdata_q` and `imem_wdata`, which only matter in the cycle a write strobe is high, one cycle after `data_done`, when `rx_byte` (and `wdata_q`, one cycle later) still hold that byte; the reset value only exists while no byte of the current transaction has completed |
| `host-4cc21073` | `keyer_host.v`, receive reset (also while CS_n is high) | `imem_hi` resets to 1 | the command byte's `byte_done` writes `imem_hi <= 0` (`~cmd_phase` is 0); every read of `imem_hi` needs `data_done` or a pending load (`load_pending` is also set by that byte and reset with `imem_hi`), so none can see the reset value |
| `host-27828b31` | `keyer_host.v`, receive block | `if (!rst_n)` -> `if (1'b0)`: `is_write` and `reg_sel` are never reset | the reason of `host-a8186d77` for both registers: every use needs `data_done`, `cmd_done` or a pending load, all of which follow the command byte of the current transaction, which writes both (`byte_idx` is cleared while CS_n is high); at power-up their value is arbitrary and equally unread |
| `host-48954ae4` | `keyer_host.v`, receive block | `if (!rst_n)` -> `if (1'b1)`: `is_write` and `reg_sel` cleared whenever CS_n is high | as `host-6388c735` |
| `host-c0660dd9` | `keyer_host.v`, receive block | `if (!rst_n)` -> `if (!(!rst_n))` | the same circuit as `host-6388c735` (`if (rst_n)`) |
| `host-3056e852` | `keyer_host.v`, receive block | `reg_sel` resets to `7'd1` | as `host-a8186d77`: `reg_sel` is read only after the command byte of the current transaction has written it |
| `host-e6f6ba4f` | `keyer_host.v`, reset of the write pulses | `run_we` resets to 1 | pulse in cycle 0 with `run_val` = 0: `running <= 0`, `DELAY <= 0` (SEMANTICS 2.3), both already 0 after the hard reset; a RUN bit of 0 does not touch `halted` |
| `host-0b1b0046` | `keyer_host.v`, reset of the write pulses | `run_val` resets to `2'b01` | `run_val` is read only while `run_we` is high, and `run_we` is set only together with `run_val <= rx_byte[1:0]`, so the reset value is overwritten before it is first read |
| `host-0cdec1b7` | `keyer_host.v`, reset of the write pulses | `rst_pulse` resets to `2'b01` | pulse in cycle 0: soft reset of thread 0 (SEMANTICS 3.2) clears LR, Z, C, timer and DELAY, all 0 after the hard reset, and empties FIFOs that are empty; no push or commit can share cycle 0 (threads are stopped, no SPI byte is complete) |
| `host-78733aa6` | `keyer_host.v`, reset of the write pulses | `pc_we` resets to `2'b01` | pulse in cycle 0: `PC[0] <= pc_val = wdata_q = 0` while thread 0 is stopped; `PC[0]` is already 0 |
| `host-0896fcc3` | `keyer_host.v`, reset of the write pulses | `outbox_pop` resets to `2'b01` | pulse in cycle 0: a pop of outbox 0, which is empty after reset, so it is ignored (SEMANTICS 8) |
| `host-901ca4a6` | `keyer_host.v`, reset of the write pulses | `fifo_clr` resets to `4'd1` | pulse in cycle 0: clears inbox 0, already empty; no push can land in cycle 0 |
| `host-411ed20c` | `keyer_host.v`, reset of the write pulses | `pinmode_we` resets to 1 | pulse in cycle 0 with `pinmode_val = wdata_q = 0`: `od_mask <= 0`, `uio_oe <= uio_oe & ~0`, `uio_out <= uio_out & ~0` (SEMANTICS 5.3): no change from the reset state |
| `host-a49062ef` | `keyer_host.v`, reset of the write registers | `imem_lo` resets to `8'd1` | `imem_lo` is read only with `imem_we` (the high byte of an IMEM_DATA pair) or with the CAP_CFG, CAP_BUF and REP_BUF pulses (after data byte 1); in both cases the low byte of the same pair or byte 0 of the same transaction has written it first (`!imem_hi && !data_idx[0]` holds for every such byte) |
| `host-bf048322` | `keyer_host.v`, reset of the write registers | `wdata_q` resets to `8'd1` | `wdata_q <= rx_byte` in every cycle out of reset, so the reset value exists only during cycle 0, and `wdata_q` only matters while a write strobe is high, none of which is in cycle 0 |
| `host-fa635f3c` | `keyer_host.v`, reset of the write pulses | `cr_ctrl_we` resets to 1 | pulse in cycle 0 with `cr_ctrl_val = wdata_q = 0`: ARM, DISARM, START and STOP are bits 0-3 of the value (SEMANTICS 14.2, 14.5), all 0, so no action |
| `host-49a17a21` | `keyer_host.v`, reset of the write pulses | `cap_cfg_we` resets to 1 | pulse in cycle 0 carrying `{wdata_q, imem_lo}` = 0 into a configuration that reset has just set to 0 (as `host-925dfa28`) |
| `host-fee88032` | `keyer_host.v`, reset of the write pulses | `cap_buf_we` resets to 1 | same, for `cap_base` and `cap_len` |
| `top-84d630fd` | `tt_um_ahan17x_keyer.v`, `fetch_ok` | `if (!rst_n)` -> `if (1'b0)`: `fetch_ok` is not reset | `fetch_ok` is read only as `running & fetch_ok` (the core's `run_ok`); `running` is 0 in cycle 0 after any reset and can first be 1 in a cycle after a RUN write, by which time `fetch_ok` has been reloaded from the port use of the cycle before |
| `top-30498627` | `tt_um_ahan17x_keyer.v`, `fetch_ok` | `fetch_ok` resets to 1 | same: the only cycle with the reset value is cycle 0, in which no thread is running (SEMANTICS 2.1's invalid fetch in cycle 0 is never needed by a running thread) |
