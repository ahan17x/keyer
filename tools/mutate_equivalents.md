# Equivalent mutants

Mutants that no test or proof kills because no behaviour at the chip's pins
or in any state the tests may read differs, and that Yosys cannot prove
equivalent by itself (it proves equality for every input and every state;
these are equal only for the inputs and states that can occur). One row
each, with the reason. `tools/mutate.py report` reads the ids from this
table; a row whose line of source changes gets a new id and must be judged
again.

| Id | Where | Mutation | Why it is equivalent |
|---|---|---|---|
| `host-c6659575` | `keyer_host.v`, synchroniser reset | `sck_s` resets to `3'b001` instead of `3'b000` | the stray SCK "edge" it produces one cycle after reset is ignored: CS_n resets to inactive (`csn_s = 3'b111`), and the receive logic is held in reset while inactive; the value has left the shift register before a transaction can start (CS_n must be high for 4 cycles, SEMANTICS 10.1) |
| `host-6388c735` | `keyer_host.v`, receive block | `if (!rst_n)` -> `if (rst_n)` around the clearing of `is_write` and `reg_sel` | they are then cleared whenever CS_n is high instead of at reset; both are written by every command byte before anything reads them (`data_done` needs a completed command byte in the same transaction) |
| `host-a8186d77` | `keyer_host.v`, receive block | `is_write` resets to 1 | same: `is_write` is read only after the command byte of the current transaction has written it |
| `host-925dfa28` | `keyer_host.v`, reset of the write pulses | `rep_cfg_we` resets to 1 | one write pulse in the first cycle after reset, carrying `wdata_q` = 0 into a register that reset has just set to 0 |
| `host-d51d82cf` | `keyer_host.v`, reset of the write pulses | `rep_buf_we` resets to 1 | same, with `{wdata_q, imem_lo}` = 0 |
