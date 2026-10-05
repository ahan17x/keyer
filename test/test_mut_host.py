# SPDX-License-Identifier: Apache-2.0
"""Host interface, top level and ISA header: tests for the survivors of the
full mutation campaign (GitHub run 37260274984, tools/mutate.py). Each test
names the faults it kills; the expected values come from docs/SEMANTICS.md
(sections 3.1, 5.3, 10, 11, 14), not from the RTL. The tests that use only
the pads also run on the gate-level netlist.
"""

import cocotb
from cocotb.triggers import ClockCycles

import keyerasm

from keyer_tb import (R_CAP_BUF, R_CAP_CFG, R_CR_COUNT, R_CR_CTRL, R_CTRL, R_FIFOCLR, R_ID,
                      R_IMEM_ADDR, R_IMEM_DATA, R_INBOX0, R_INBOX1, R_IRQEN, R_LEVELS, R_PC0,
                      R_PC1, R_PINMODE, R_PINOUT, R_PINS, R_REP_BUF, R_REP_CFG, R_STAT, SpiMaster,
                      reset)
from test import GL, start


def asm(src):
    """Assemble, keeping only the words up to the last one the source
    places (test.asm pads to 256 words, which the host would load whole)."""
    words, syms, _ = keyerasm.assemble(src)
    return keyerasm.to_list(words, size=0), syms


# T0 runs from 0, T1 from 4; every run of a thread pushes r0 (= 0) into its
# own outbox and halts, so the host can step the outbox counts and the halted
# bits one at a time.
PUSH_HALT, _ = asm("""
        push r0
        halt
        push r0
        halt
        push r0
        halt
        push r0
        halt
""")


def uo_bit(dut, i):
    """Bit i of uo_out as '0', '1' or 'x'."""
    return str(dut.uo_out.value)[7 - i].lower()


async def irq_per_bit(dut, spi):
    """The IRQ pad (uo[1]) with IRQEN holding each single bit in turn."""
    got = []
    for k in range(8):
        await spi.write(R_IRQEN, [1 << k])
        got.append(uo_bit(dut, 1))
    return "".join(got)


async def irq_with(dut, spi, mask):
    await spi.write(R_IRQEN, [mask])
    return uo_bit(dut, 1)


@cocotb.test()
async def test_irq_follows_each_condition(dut):
    """SEMANTICS 11: IRQ is the OR over the IRQEN bits of outbox 0 / 1
    non-empty, halted[0] / [1], inbox 0 / 1 empty, capture done, replay
    done. Each bit is enabled alone in states where its condition is false,
    true with a count of 1, and true with a count of 2; IRQEN = 0 gives no
    IRQ whatever holds. Kills the inverted and off-by-one FIFO conditions
    (host-976df132, -18a65a73, -ebb6b0dd, -f84e4731, -c7e51a05, -8535549d,
    -0747f5d9, -d2c77d1c), IRQ stuck at 1 (host-04e511a9) and the enable
    ORed instead of ANDed (host-6e865af9)."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    # bit order of the strings: IRQEN bit 0 first
    assert await irq_with(dut, spi, 0x00) == "0"
    assert await irq_per_bit(dut, spi) == "00001100"          # after reset: both inboxes empty, nothing else
    assert await irq_with(dut, spi, 0xFF) == "1"
    assert await irq_with(dut, spi, 0x0F) == "0"
    await spi.load_program(PUSH_HALT)
    await spi.set_pc(1, 4)
    await spi.run(0b01)                                       # outbox 0 holds 1, T0 halted
    assert await spi.read(R_LEVELS, 4) == [0, 1, 0, 0]
    assert await irq_per_bit(dut, spi) == "10101100"
    await spi.run(0b10)                                       # outbox 1 holds 1, T1 halted
    assert await spi.read(R_LEVELS, 4) == [0, 1, 0, 1]
    assert await irq_per_bit(dut, spi) == "11111100"
    await spi.run(0b11)                                       # both outboxes hold 2
    assert await spi.read(R_LEVELS, 4) == [0, 2, 0, 2]
    assert await irq_per_bit(dut, spi) == "11111100"
    await spi.write(R_INBOX0, [0x5A])
    await spi.write(R_INBOX1, [0xA5])                         # both inboxes hold 1
    assert await irq_per_bit(dut, spi) == "11110000"
    await spi.write(R_INBOX0, [0x11])
    await spi.write(R_INBOX1, [0x22])                         # both inboxes hold 2
    assert await spi.read(R_LEVELS, 4) == [2, 2, 2, 2]
    assert await irq_per_bit(dut, spi) == "11110000"
    await spi.cr_ctrl(0b0001)                                 # ARM with cap_len = 0: done at once (14.4)
    assert await irq_per_bit(dut, spi) == "11110010"
    await spi.cr_ctrl(0b0100)                                 # START with n_rep = 0: done at once (14.5)
    assert await irq_per_bit(dut, spi) == "11110011"
    assert await irq_with(dut, spi, 0x00) == "0"
    assert await irq_with(dut, spi, 0x30) == "0"              # only the false conditions enabled


@cocotb.test()
async def test_fifoclr_bits_and_extra_bytes(dut):
    """SEMANTICS 10.3: FIFOCLR byte 0 empties inbox 0, outbox 0, inbox 1,
    outbox 1 (bits 0-3); a second data byte has no effect, and a write to
    ID (read-only) is ignored. Kills FIFOCLR decoded at 0x0F
    (host-1a91d27c) and the byte-0 condition inverted, stuck or moved to
    byte 1 (host-578c89da, -c16894fa, -cc481b70, -74a1aef8, -06fb5e3a)."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    await spi.load_program(PUSH_HALT)
    await spi.set_pc(1, 4)
    await spi.run(0b11)
    await spi.run(0b11)                                       # each thread pushed twice
    await spi.write(R_INBOX0, [1, 2, 3])
    await spi.write(R_INBOX1, [4, 5, 6, 7])
    assert await spi.read(R_LEVELS, 4) == [3, 2, 4, 2]
    await spi.write(R_ID, [0x0F, 0x0F])
    assert await spi.read(R_LEVELS, 4) == [3, 2, 4, 2]
    await spi.write(R_FIFOCLR, [0x00, 0x0F])                  # byte 1 ignored
    assert await spi.read(R_LEVELS, 4) == [3, 2, 4, 2]
    await spi.write(R_FIFOCLR, [0x01])
    assert await spi.read(R_LEVELS, 4) == [0, 2, 4, 2]
    await spi.write(R_FIFOCLR, [0x02, 0x00])
    assert await spi.read(R_LEVELS, 4) == [0, 0, 4, 2]
    await spi.write(R_FIFOCLR, [0x04])
    assert await spi.read(R_LEVELS, 4) == [0, 0, 0, 2]
    await spi.write(R_FIFOCLR, [0x08])
    assert await spi.read(R_LEVELS, 4) == [0, 0, 0, 0]


@cocotb.test()
async def test_byte_0_registers_ignore_further_bytes(dut):
    """SEMANTICS 10.3: CTRL, IMEM_ADDR, PINMODE, IRQEN, CR_CTRL and REP_CFG
    take byte 0 only. A second byte that would change something is
    ignored. Kills the byte-0 condition stuck at 1 for CTRL (host-0b3ddf01),
    PINMODE (host-384b4405), IRQEN (host-84d5b9d8), CR_CTRL (host-0208b285)
    and REP_CFG (host-085ce362)."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    words, _ = asm("spin: bra spin")
    await spi.load_program(words)
    await spi.write(R_CTRL, [0x00, 0x03])                     # byte 1 would start both threads
    assert await spi.read(R_STAT, 1) == [0]
    await spi.write(R_INBOX0, [9])
    await spi.write(R_CTRL, [0x00, 0x04])                     # byte 1 would soft-reset thread 0
    assert await spi.read(R_LEVELS, 4) == [1, 0, 0, 0]
    await spi.write(R_IMEM_ADDR, [0x20, 0x55])
    assert await spi.read(R_IMEM_ADDR, 2) == [0x20, 0]
    await spi.write(R_PINMODE, [0xA5, 0x5A])
    assert await spi.read(R_PINMODE, 1) == [0xA5]
    await spi.write(R_PINMODE, [0x00])
    await spi.write(R_IRQEN, [0x12, 0x34])
    assert await spi.read(R_IRQEN, 1) == [0x12]
    await spi.write(R_IRQEN, [0x00])
    await spi.write(R_CR_CTRL, [0x00, 0x04])                  # byte 1 would START a replay (done at once)
    assert await spi.read(R_CR_CTRL, 1) == [0]
    await spi.write(R_CR_CTRL, [0x00, 0x01])                  # ... or ARM a capture of length 0
    assert await spi.read(R_CR_CTRL, 1) == [0]
    await spi.write(R_REP_CFG, [0xF1, 0x23])
    assert await spi.read(R_REP_CFG, 1) == [0xF1]


@cocotb.test()
async def test_reads_of_ctrl_pins_pinout_and_unmapped_registers(dut):
    """SEMANTICS 10.4: CTRL reads {6'b0, running}; PINS byte 2 is the
    firmware uo_out with bits 1:0 read 0; PINOUT is uio_out, then uio_oe;
    registers without a read value (INBOX0, INBOX1, FIFOCLR, the unused
    addresses) read 0. Kills CTRL's upper bits read as 1 (host-f11c4dca),
    PINS byte 2 decoded as byte 3 (host-48cefbc5), PINOUT's bytes swapped
    (host-4511cf62) and the default read value 1 (host-9ba770f8)."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    words, syms = asm("""
            oen  uio0           ; uio_oe = 0x01
            set  uio1           ; uio_out = 0x02 (push-pull)
            set  uo2
            set  uo4            ; uo_out = 0x14
            halt
    spin:   bra  spin
    """)
    await spi.load_program(words)
    assert await spi.read(R_CTRL, 1) == [0]
    for reg in (R_INBOX0, R_INBOX1, R_FIFOCLR, 0x17, 0x40, 0x7F):
        assert await spi.read(reg, 2) == [0, 0], hex(reg)
    await spi.run(0b01)
    await ClockCycles(dut.clk, 20)
    assert await spi.read(R_STAT, 1) == [0b0100]                 # T0 halted
    assert await spi.read(R_CTRL, 1) == [0]
    assert await spi.read(R_PINOUT, 2) == [0x02, 0x01]
    # uio pads: pin 0 driven low, the rest pulled up; ui: no inputs and the
    # SPI pins low when the byte is sampled (test_id_and_registers); uo
    assert await spi.read(R_PINS, 3) == [0xFE, 0x00, 0x14]
    await spi.set_pc(1, syms["spin"])
    await spi.run(0b10)
    assert await spi.read(R_CTRL, 1) == [0b10]
    await spi.run(0b11)                                          # T0 resumes after its HALT: the spin loop
    assert await spi.read(R_CTRL, 1) == [0b11]
    for reg in (R_INBOX0, R_INBOX1, R_FIFOCLR, 0x7F):
        assert await spi.read(reg, 1) == [0], hex(reg)
    await spi.run(0)
    assert await spi.read(R_CTRL, 1) == [0]


@cocotb.test()
async def test_pp_returns_a_pin_to_push_pull(dut):
    """SEMANTICS 5.3: PP p clears od_mask[p] and leaves the drive alone, so
    OEN and SET work on the pin again. Kills the PP sub-opcode encoded as
    OD's (isa-4f9f5e77): PP then decodes as undefined and the pin stays
    open-drain."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    words, _ = asm("""
            od   uio2
            od   uio5
            pp   uio2
            oen  uio2
            set  uio2
            pp   uio6           ; already push-pull: no change
            halt
    """)
    await spi.load_program(words)
    await spi.run(0b01)
    await ClockCycles(dut.clk, 30)
    assert await spi.read(R_STAT, 1) == [0b0100]
    assert await spi.read(R_PINMODE, 1) == [0x20]
    assert await spi.read(R_PINOUT, 2) == [0x04, 0x04]


@cocotb.test()
async def test_hard_reset_in_the_middle_of_activity(dut):
    """SEMANTICS 3.1, asserted while both threads run, the FIFOs hold data,
    a capture is armed and every host register is non-zero: afterwards every
    register reads 0 (IRQEN and the IMEM address included), the pads are
    idle, and the program memory is unchanged. Kills IRQEN resetting to 1
    (host-e602e4a6), the IMEM address resetting to 1 (host-ab64e499) and an
    IMEM write pulse out of reset (host-499b9af2), which overwrites word 0."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    words, _ = asm("""
            ldi  r0, 0x5A
            set  uo3
            oen  uio6
            set  uio6
            push r0
    spin:   bra  spin
    """)
    words += [0xBEEF, 0x1234]
    await spi.load_program(words)
    await spi.write(R_INBOX0, [1, 2])
    await spi.write(R_INBOX1, [3])
    await spi.write(R_IMEM_ADDR, [0x40, 0])
    await spi.write(R_IRQEN, [0xFF])
    await spi.write(R_PINMODE, [0x81])
    await spi.cap_config(group=2, mask=0x8, tpat=0x8, tmask=0x8, base=0xC0, length=8)
    await spi.rep_config(group=1, mask=0x3, base=0xA0, length=5)
    await spi.cr_ctrl(0b0001)                                    # ARM: waits for ui3
    await spi.set_pc(1, 5)
    await spi.run(0b11)
    await ClockCycles(dut.clk, 40)
    assert await spi.read(R_STAT, 1) == [0b11]
    assert await spi.read(R_LEVELS, 4) == [2, 1, 1, 0]
    assert await spi.read(R_PINOUT, 2) == [0x40, 0x40]
    assert await spi.read(R_CR_CTRL, 1) == [0x01]
    assert uo_bit(dut, 1) == "1"

    await reset(dut, pads)                                       # CS_n is high: no transaction is cut
    await ClockCycles(dut.clk, 4)
    assert str(dut.uo_out.value) == "00000000"                   # MISO, IRQ and uo[7:2] all 0
    assert str(dut.uio_oe.value) == "00000000" and str(dut.uio_out.value) == "00000000"
    expect = [(R_ID, [0x4B, 0x03]), (R_CTRL, [0]), (R_STAT, [0]), (R_PC0, [0, 0]), (R_PC1, [0, 0]),
              (R_IMEM_ADDR, [0, 0]), (R_LEVELS, [0, 0, 0, 0]), (R_PINMODE, [0]), (R_IRQEN, [0]),
              (R_PINS, [0xFF, 0x00, 0x00]), (R_PINOUT, [0, 0]), (R_CR_CTRL, [0]), (R_CAP_CFG, [0, 0]),
              (R_CAP_BUF, [0, 0]), (R_REP_CFG, [0]), (R_REP_BUF, [0, 0]), (R_CR_COUNT, [0, 0])]
    for reg, val in expect:
        assert await spi.read(reg, len(val)) == val, hex(reg)
    assert uo_bit(dut, 1) == "0"
    # the address is 0 without an IMEM_ADDR write, and the words survived
    rb = await spi.read(R_IMEM_DATA, 2 * len(words))
    got = [rb[2 * i] | (rb[2 * i + 1] << 8) for i in range(len(words))]
    assert got == words, ["%04X" % w for w in got]
    assert await spi.read(R_IMEM_ADDR, 2) == [len(words), 0]


@cocotb.test()
async def test_imem_data_writes_dropped_while_a_thread_runs(dut):
    """SEMANTICS 10.3: an IMEM_DATA pair completed while either thread runs
    is dropped, and the address does not advance. Kills the memory port
    granted while threads run (top-cb4bcf00) or while only one of them
    does (top-510d1047)."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    words, _ = asm("spin: bra spin")
    await spi.load_program(words)
    await spi.load_program([0x1234, 0xABCD], base=0x80)
    for mask in (0b01, 0b10, 0b11):
        await spi.set_pc(0, 0)
        await spi.set_pc(1, 0)
        await spi.run(mask)
        await spi.write(R_IMEM_ADDR, [0x80, 0])
        await spi.write(R_IMEM_DATA, [0x11, 0x22, 0x33, 0x44])
        assert await spi.read(R_IMEM_ADDR, 2) == [0x80, 0], mask
        assert await spi.read(R_STAT, 1) == [mask]
        await spi.run(0)
        assert await spi.read(R_IMEM_ADDR, 2) == [0x80, 0], mask
        assert await spi.read(R_IMEM_DATA, 4) == [0x34, 0x12, 0xCD, 0xAB], mask


@cocotb.test()
async def test_capture_writes_while_the_host_reads_registers(dut):
    """SEMANTICS 10.5 and 14.1: the host owns the memory port only for
    IMEM_DATA traffic, so with both threads stopped a capture writes its
    entries while the host polls CR_CTRL: no overflow, every entry in
    memory with its pins and its delta. Kills the host's IMEM read request
    raised in every cycle after a read transaction (host-99e2cec1), which
    starves the capture until its queue overflows."""
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    D = 150
    await spi.cap_config(group=2, mask=0x8, tpat=0, tmask=0, base=0x80, length=4)
    await spi.cr_ctrl(0b0001)                                    # ARM: triggers at once (mask 0), ui3 low

    async def toggle_ui3():
        for v in (1, 0, 1):
            await ClockCycles(dut.clk, D)
            pads.set_fw_inputs(v << 3, 0xFF)

    task = cocotb.start_soon(toggle_ui3())
    polls = []
    while not task.done():
        polls.append(await spi.read(R_CR_CTRL, 1))
    await ClockCycles(dut.clk, 10)
    assert len(polls) >= 2, polls                               # the host read while entries came
    assert await spi.read(R_CR_CTRL, 1) == [0b0110]              # triggered, done, no overflow, inactive
    assert await spi.read(R_CR_COUNT, 2) == [4, 0]
    entries = await spi.read_entries(0x80, 4)
    assert [e & 0xF for e in entries] == [0, 8, 0, 8], ["%04X" % e for e in entries]
    assert entries[0] >> 4 == 0 and [e >> 4 for e in entries[2:]] == [D, D], ["%04X" % e for e in entries]


async def _lockstep_host(dut, body):
    from keyer_tb import Lockstep
    pads = await start(dut)
    spi = SpiMaster(dut, pads, half=4)
    ls = Lockstep(dut, pads)
    task = cocotb.start_soon(body(spi))
    while not task.done():
        await ls.run(100)
    await ls.run(100)
    return ls, spi


@cocotb.test(skip=GL)
async def test_lockstep_replay_fetches_against_host_and_thread_start(dut):
    """SEMANTICS 2.1, 10.5, 14.1, in lockstep with the model: a replay of
    255 entries one cycle apart fetches in every free cycle. (1) The host
    reads IMEM_DATA during it: in the host's cycles the engine must not use
    the port. (2) RUN starts both threads during it: the thread whose slot
    follows the engine's fetch loses that slot. Kills the engine ignoring
    the host (top-e129e032) and the fetch counted valid after an engine
    access (top-f13e5152, top-d519bd52)."""
    words, _ = asm("spin: bra spin")
    filler = [0x0010] * 255                                      # entries {delta 1, pins 0}
    seen = {}

    async def body(spi):
        await spi.load_program(words)
        await spi.write_entries(0x01, filler)
        await spi.rep_config(group=0, mask=0, base=0x01, length=255)
        await spi.write(R_IMEM_ADDR, [0x01, 0])
        await spi.cr_ctrl(0b0100)                                # START
        seen["read"] = await spi.read(R_IMEM_DATA, 4)            # host port cycles inside the replay
        await ClockCycles(dut.clk, 300)
        seen["first"] = await spi.read(R_CR_CTRL, 1)
        await spi.cr_ctrl(0b0100)                                # START again ...
        await spi.run(0b11)                                      # ... and RUN lands while it fetches
        await ClockCycles(dut.clk, 40)
        await spi.run(0)
        await spi.cr_ctrl(0b1000)                                # STOP

    ls, spi = await _lockstep_host(dut, body)
    assert seen["read"] == [0x10, 0x00, 0x10, 0x00], seen
    assert seen["first"][0] & 0b0011_0000 == 0b0010_0000, seen   # the first replay is over
    assert ls.retired > 0
