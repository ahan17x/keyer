# SPDX-License-Identifier: Apache-2.0
"""fw/usb_ls_device.s on the RTL, in lockstep with the golden model.

Run from test/:
  make SIM_BUILD=sim_build/usb COCOTB_RESULTS_FILE=sim_build/usb/results.xml \
       COCOTB_TEST_MODULES=test_usb_ls_device

The USB low-speed host model (tools/protomodels_usb_host.py) drives D+ and
D- (uio0, uio1) of both machines at 48 MHz, 32 cycles per bit, and checks
every packet the device sends. Bus resets are compressed to 64 bit times
(43 us; anything over 2.5 us is a reset) and their recovery to 16, so each
test stays under about 100,000 cycles including the program load.
"""

import cocotb
from cocotb.triggers import ClockCycles

from protomodels_usb_host import UsbLsHost, setup_packet, RESP_MIN, RESP_MAX
from test import GL, fw, lockstep_firmware

T, RST, REC = 32, 64, 16


def declared(words, syms, first, last):
    """Descriptor bytes as the firmware source declares them (SERIC immediates)."""
    return [w & 0xFF for w in words[syms[first]:syms[last]] if w >> 8 == 0xFB]


async def usb(dut, script, limit=150000):
    """Load the firmware, run thread 0, then let the host play `script(host)`."""
    words, syms = fw("usb_ls_device.s")
    words = words[:max(a for a, w in enumerate(words) if w) + 1]
    host = UsbLsHost(bit_cycles=T, seed=7)
    rec = {}

    async def go(spi):
        await ClockCycles(dut.clk, 40)          # the firmware's eight set-up instructions
        rec.update(script(host))

    ls, spi = await lockstep_firmware(dut, words, 10, models=[host], after_load=go)
    await ls.run(limit, until=lambda _: not host.busy)
    assert not host.busy, "the host did not finish in %d cycles" % limit
    await ls.run(200)
    dut._log.info("lockstep: %d cycles, %d responses, delay %.3f..%.3f bit times",
                  ls.cycle, len(host.delays), min(host.delays), max(host.delays))
    assert all(RESP_MIN <= d <= RESP_MAX for d in host.delays)
    dev = declared(words, syms, "dev", "cfg")
    cfg = declared(words, syms, "cfg", "desc_end")
    return host, rec, spi, dev, cfg, ls


@cocotb.test(skip=GL)
async def test_lockstep_usb_enumerate_to_address(dut):
    """Reset, GET_DESCRIPTOR(device, 64) at address 0, reset, SET_ADDRESS(5),
    GET_DESCRIPTOR(device, 18) at address 5."""
    def script(h):
        h.reset(RST, REC)
        a = h.get_descriptor(0, 1, 64)
        h.reset(RST, REC)
        b = h.set_address(0, 5)
        c = h.get_descriptor(5, 1, 18)
        old = h.token_in(0, 0)                  # address 0 is no longer ours
        return dict(a=a, b=b, c=c, old=old)

    host, r, spi, dev, cfg, ls = await usb(dut, script)
    assert host.errors == [], host.errors[:5]
    assert r["a"].ok and r["a"].data == dev
    assert r["b"].ok and r["c"].ok and r["c"].data == dev
    assert r["old"][0].result == "timeout"
    assert ls.cycle < 150000
    got = await spi.drain_outbox(0)
    assert got == [6, 5, 6], got


@cocotb.test(skip=GL)
async def test_lockstep_usb_error_paths(dut):
    """IN before any SETUP (NAK); a token with a bad CRC-5 and one for
    another address (ignored); GET_DESCRIPTOR(configuration, 9) whose SETUP
    DATA0 and status DATA1 each have a bad CRC-16 once (no handshake, the
    retries succeed); GET_STATUS (STALLed)."""
    def script(h):
        h.reset(RST, REC)
        nak = h.token_in(0, 0)
        bad = h.token_in(0, 0, bad_crc5=True)
        other = h.token_in(3, 0)
        c = h.get_descriptor(0, 2, 9, corrupt_setups=1, corrupt_status=1)
        st = h.control_read(0, setup_packet(0x80, 0, 0, 0, 2))
        return dict(nak=nak, bad=bad, other=other, c=c, st=st)

    host, r, spi, dev, cfg, ls = await usb(dut, script)
    assert host.errors == [], host.errors[:5]
    assert r["nak"][0].result == "NAK"
    assert r["bad"][0].result == "timeout" and r["other"][0].result == "timeout"
    res = [t.result for t in r["c"].txns]
    assert res == ["timeout", "ACK", "DATA1", "DATA0", "timeout", "ACK"], res
    assert r["c"].ok and r["c"].data == cfg[:9]
    assert r["st"].status == "stall"
    assert ls.cycle < 150000
    got = await spi.drain_outbox(0)
    assert got == [6, 0], got
