"""cocotb helpers for the Loom testbench: pad driver, SPI host master, and the
lockstep harness that runs the Python ISS cycle by cycle against the RTL.
"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

import loomsim

R_CTRL, R_STAT, R_PC0, R_PC1, R_IMEM_ADDR, R_IMEM_DATA = 0, 1, 2, 3, 4, 5
R_INBOX0, R_OUTBOX0, R_INBOX1, R_OUTBOX1, R_LEVELS, R_PINMODE = 6, 7, 8, 9, 10, 11
R_IRQEN, R_PINS, R_FIFOCLR, R_ID, R_PINOUT = 12, 13, 14, 15, 16


def ival(sig):
    """int(signal) with X/Z mapped to -1 so comparisons fail loudly instead of crashing."""
    try:
        return int(sig.value)
    except ValueError:
        return -1


class Pads:
    """Owns tb.ui_in and tb.uio_ext. SPI bits (ui 0..2) and firmware input
    bits (ui 3..7) are set by different coroutines; both go through here."""

    def __init__(self, dut):
        self.dut = dut
        self.ui = 0x04          # CS_n idle high
        self.uio_ext = 0xFF     # pull-ups on every bidirectional pin
        self.flush()

    def flush(self):
        self.dut.ui_in.value = self.ui
        self.dut.uio_ext.value = self.uio_ext

    def set_spi(self, sck, mosi, csn):
        self.ui = (self.ui & 0xF8) | (sck & 1) | ((mosi & 1) << 1) | ((csn & 1) << 2)
        self.flush()

    def set_fw_inputs(self, ext_ui, ext_uio):
        self.ui = (self.ui & 0x07) | (ext_ui & 0xF8)
        self.uio_ext = ext_uio & 0xFF
        self.flush()


class SpiMaster:
    """Mode 0 master driving the host SPI pins. half = half SCK period in clk cycles."""

    def __init__(self, dut, pads, half=8):
        self.dut, self.pads, self.half = dut, pads, half

    async def _byte(self, b):
        got = 0
        for i in range(7, -1, -1):
            self.pads.set_spi(0, (b >> i) & 1, 0)
            await ClockCycles(self.dut.clk, self.half)
            self.pads.set_spi(1, (b >> i) & 1, 0)
            got = (got << 1) | (ival(self.dut.uo_out) & 1)
            await ClockCycles(self.dut.clk, self.half)
        return got

    async def xfer(self, cmd, data):
        """Send cmd then data bytes; returns the bytes clocked in during data."""
        self.pads.set_spi(0, 0, 0)
        await ClockCycles(self.dut.clk, self.half)
        await self._byte(cmd)
        rx = []
        for b in data:
            rx.append(await self._byte(b))
        self.pads.set_spi(0, 0, 0)
        await ClockCycles(self.dut.clk, self.half)
        self.pads.set_spi(0, 0, 1)
        await ClockCycles(self.dut.clk, 2 * self.half)
        return rx

    async def write(self, reg, data):
        await self.xfer(0x80 | reg, list(data))

    async def read(self, reg, n):
        return await self.xfer(reg & 0x7F, [0] * n)

    async def load_program(self, words, base=0):
        await self.write(R_IMEM_ADDR, [base & 0xFF, 0])
        data = []
        for w in words:
            data += [w & 0xFF, (w >> 8) & 0xFF]
        await self.write(R_IMEM_DATA, data)

    async def run(self, mask):
        await self.write(R_CTRL, [mask & 0x3])

    async def set_pc(self, tid, pc):
        await self.write(R_PC0 + tid, [pc & 0xFF, 0])

    async def levels(self):
        return await self.read(R_LEVELS, 4)

    async def drain_outbox(self, tid):
        lv = await self.levels()
        n = lv[1 + 2 * tid]
        return (await self.read(R_OUTBOX0 + 2 * tid, n)) if n else []


async def reset(dut, pads, cycles=5):
    dut.ena.value = 1
    dut.rst_n.value = 0
    pads.flush()
    await ClockCycles(dut.clk, cycles)
    await RisingEdge(dut.clk)
    dut.rst_n.value = 1          # cycle 0 begins now (cyc == 0, rst_n == 1)


class Lockstep:
    """Runs the ISS one cycle per RTL cycle and compares.

    Must be started right after reset() so that both begin at cycle 0.
    Every host-side effect visible at the RTL host interface (program memory
    writes, run/stop, PC writes, FIFO pushes/pops, pin mode, FIFO clears, soft
    resets) is mirrored into the ISS on the same cycle, so the two machines
    see identical inputs. Protocol models (tools/protomodels.py) run against
    the ISS, and their external pin levels are applied to the RTL pads.
    """

    def __init__(self, dut, pads, models=(), check_regs=True):
        self.dut, self.pads, self.models = dut, pads, list(models)
        self.m = loomsim.Machine(trace=False)
        self.cycle = 0
        self.check_regs = check_regs
        self.retired = 0
        self.mismatches = []
        u = dut.user_project
        self.core, self.host, self.pins = u.u_core, u.u_host, u.u_pins
        self.regs_ok = True
        try:
            _ = self.core.regs[0].value
        except Exception:
            self.regs_ok = False

    def _fail(self, what, exp, got):
        msg = "cycle %d: %s: ISS=%s RTL=%s" % (self.cycle, what, exp, got)
        self.mismatches.append(msg)
        raise AssertionError(msg)

    def _check_state(self):
        m, c, p = self.m, self.core, self.pins
        # state during this cycle == ISS state after the previous step
        if ival(p.uio_out) != m.uio_out:
            self._fail("uio_out", "%02X" % m.uio_out, "%02X" % ival(p.uio_out))
        if ival(p.uio_oe) != m.uio_oe:
            self._fail("uio_oe", "%02X" % m.uio_oe, "%02X" % ival(p.uio_oe))
        if ival(p.uo_out) != m.uo_out:
            self._fail("uo_out", "%02X" % m.uo_out, "%02X" % ival(p.uo_out))
        if ival(p.od_mask) != m.od_mask:
            self._fail("od_mask", "%02X" % m.od_mask, "%02X" % ival(p.od_mask))
        run = ival(c.running)
        exp_run = (m.threads[0].running | (m.threads[1].running << 1))
        if run != exp_run:
            self._fail("running", exp_run, run)
        for t in (0, 1):
            th = m.threads[t]
            if ival(c.pc[t]) != th.pc:
                self._fail("pc[%d]" % t, "%02X" % th.pc, "%02X" % ival(c.pc[t]))
            if ival(c.tick[t]) != th.tick:
                self._fail("tick[%d]" % t, th.tick, ival(c.tick[t]))
            if ival(c.period[t]) != th.period:
                self._fail("period[%d]" % t, th.period, ival(c.period[t]))
            if ival(c.count[t]) != th.count:
                self._fail("count[%d]" % t, th.count, ival(c.count[t]))

    def _check_regs(self, tid):
        th = self.m.threads[tid]
        c = self.core
        if ival(c.fz[tid]) != th.z or ival(c.fc[tid]) != th.c:
            self._fail("flags T%d" % tid, (th.z, th.c), (ival(c.fz[tid]), ival(c.fc[tid])))
        if ival(c.lr[tid]) != th.lr:
            self._fail("lr T%d" % tid, th.lr, ival(c.lr[tid]))
        if self.regs_ok:
            for r in range(8):
                v = ival(c.regs[tid * 8 + r])
                if v != th.regs[r]:
                    self._fail("T%d r%d" % (tid, r), "%04X" % th.regs[r], "%04X" % v)

    async def run(self, cycles, until=None):
        """Run `cycles` cycles (or until until(self) is true). Call right after reset()."""
        dut, m = self.dut, self.m
        pending = None                      # thread whose write-back to check next cycle
        for _ in range(cycles):
            # 1. models decide this cycle's external levels from the ISS state
            for mod in self.models:
                mod.on_cycle(m)
            self.pads.set_fw_inputs(m.ext_ui, m.ext_uio)
            await ReadOnly()
            # 2. the ISS takes exactly the RTL's inputs for this cycle
            m.ext_ui = ival(dut.ui_in)
            m.ext_uio = ival(dut.uio_ext)
            # 3. compare registered state (ISS post-step of the previous cycle)
            self._check_state()
            if pending is not None and self.check_regs:
                self._check_regs(pending)
                pending = None
            exec_ = ival(self.core.exec)
            commit = ival(self.core.commit)
            tid = ival(self.core.tid)
            pc = ival(self.core.pc_cur)
            ir = ival(self.core.ir)
            host_snapshot = self._snapshot_host()
            # 4. step the ISS for this cycle and compare the execution record
            t = m.threads[tid]
            exp_exec = 1 if t.running else 0
            exp_pc = t.pc
            done = m.step()
            if exec_ != exp_exec:
                self._fail("exec T%d pc=%02X" % (tid, exp_pc), exp_exec, exec_)
            if exec_:
                if pc != exp_pc:
                    self._fail("pc_cur T%d" % tid, "%02X" % exp_pc, "%02X" % pc)
                if ir != m.imem[exp_pc]:
                    self._fail("ir T%d pc=%02X" % (tid, exp_pc), "%04X" % m.imem[exp_pc], "%04X" % ir)
                if commit != (1 if done else 0):
                    self._fail("commit T%d pc=%02X ir=%04X" % (tid, exp_pc, ir), int(done), commit)
                if commit:
                    self.retired += 1
                    pending = tid
            # 5. mirror the host's effects of this cycle (they land at its end)
            self._apply_host(host_snapshot)
            await RisingEdge(dut.clk)
            self.cycle += 1
            if until and until(self):
                return

    def _snapshot_host(self):
        h = self.host
        return dict(imem_we=ival(h.imem_we), imem_addr=ival(h.imem_addr),
                    imem_wdata=ival(h.imem_wdata), run_we=ival(h.run_we),
                    run_val=ival(h.run_val), rst_pulse=ival(h.rst_pulse),
                    pc_we=ival(h.pc_we), pc_val=ival(h.pc_val),
                    inbox_push=ival(h.inbox_push), inbox_wdata=ival(h.inbox_wdata),
                    outbox_pop=ival(h.outbox_pop), pinmode_we=ival(h.pinmode_we),
                    pinmode_val=ival(h.pinmode_val), fifo_clr=ival(h.fifo_clr))

    def _apply_host(self, s):
        m = self.m
        if s["imem_we"]:
            m.imem[s["imem_addr"] & 0xFF] = s["imem_wdata"]
        if s["run_we"]:
            for t in (0, 1):
                m.host_run(t, bool(s["run_val"] & (1 << t)))
        for t in (0, 1):
            if s["rst_pulse"] & (1 << t):
                m.threads[t].soft_reset()
            if s["pc_we"] & (1 << t):
                m.host_set_pc(t, s["pc_val"])
            if s["inbox_push"] & (1 << t):
                m.host_inbox_push(t, s["inbox_wdata"])
            if s["outbox_pop"] & (1 << t):
                m.host_outbox_pop(t)
        if s["pinmode_we"]:
            m.host_pinmode(s["pinmode_val"])
        if s["fifo_clr"]:
            m.host_fifo_clear(s["fifo_clr"])

