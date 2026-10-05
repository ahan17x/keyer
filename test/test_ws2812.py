# SPDX-License-Identifier: Apache-2.0
"""fw/ws2812.s on the RTL, in lockstep with the golden model, with the
WS2812B pixel-chain model (tools/protomodels_ws2812.py) on the DOUT pad.

Run from test/:
  make SIM_BUILD=sim_build/ws2812 COCOTB_RESULTS_FILE=sim_build/ws2812/results.xml COCOTB_TEST_MODULES=test_ws2812
"""

import cocotb
from cocotb.triggers import ClockCycles

import protomodels_ws2812 as wsm
from keyer_tb import R_INBOX0, R_PC0, R_STAT
from test import GL, fw, lockstep_firmware

DOUT = 18                                   # uo2
OK, UNDERRUN = 0x00, 0xFF


def image(symbols):
    """The firmware without the unused tail of the 256-word image: the load
    over SPI is then 33 words instead of 256 (about 28,000 cycles less)."""
    words, syms = fw("ws2812.s", symbols)
    used = max(i for i, w in enumerate(words) if w) + 1
    return words[:used], syms


def frame(data):
    return [len(data) & 0xFF, len(data) >> 8] + list(data)


class PushLog:
    """Notes the cycle of every byte the host lands in thread 0's inbox, and
    any byte that was dropped because the inbox was full."""

    def __init__(self):
        self.cycles, self.dropped, self._m = [], [], None

    def on_cycle(self, m):
        if self._m is None:
            self._m = m
            push = m.host_inbox_push

            def logged(tid, byte):
                ok = push(tid, byte)
                if tid == 0:
                    (self.cycles if ok else self.dropped).append(m.cycle)
                return ok

            m.host_inbox_push = logged


async def feed(dut, spi, data):
    """Refill the inbox the way a host driver must: read LEVELS, then write
    no more bytes than there is room for (a write to a full inbox is lost)."""
    data = list(data)
    while data:
        room = 16 - (await spi.levels())[0]
        if room > 0:
            await spi.write(R_INBOX0, data[:room])
            del data[:room]
        else:
            await ClockCycles(dut.clk, 100)


def check_timing(chain, sym):
    for c, high, bit in chain.highs:
        assert high == (sym["WS_T1H"] if bit else sym["WS_T0H"]), (c, high, bit)
    for c, per in chain.periods:
        assert per == sym["WS_BIT"], (c, per)
    for c, low in chain.gaps:
        assert low >= sym["WS_RESET"] >= chain.reset, (c, low)


@cocotb.test(skip=GL)
async def test_lockstep_ws2812_frame_refilled_over_spi(dut):
    """Seven LEDs at the real 50 MHz timing (62 cycles per bit). The header
    and the first LED are queued before the thread starts; the other 18
    bytes, more than the inbox holds, arrive over the host SPI while the
    frame is on the wire. The strip must see the whole frame with every edge
    on the grid and the host a 0x00 status."""
    sym = {"WS_T0H": 20, "WS_T1H": 40, "WS_BIT": 62, "WS_RESET": 2600}
    words, syms = image(sym)
    data = [0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xA5, 0x3C, 0x81, 0x01, 0x80, 0x7E,
            0x55, 0xAA, 0x0F, 0xF0, 0x33, 0xCC, 0x12, 0xEF, 0x69]
    chain = wsm.Ws2812Chain(pin=DOUT, **wsm.cycles_from_ns(20))
    log = PushLog()

    async def after(spi):
        # the start-up reset time first, so the refill happens during the frame
        await ClockCycles(dut.clk, sym["WS_RESET"])
        await feed(dut, spi, data[3:])

    ls, spi = await lockstep_firmware(dut, words, 500, models=[chain, log],
                                      inbox0=frame(data)[:5], after_load=after)
    t = ls.m.threads[0]
    await ls.run(14000, until=lambda l: chain.frames and t.blocked and t.pc == syms["ws_frame"])
    dut._log.info("ws2812 refill: %d cycles in lockstep, %d instructions" % (ls.cycle, ls.retired))
    assert chain.errors == [], chain.errors[:3]
    assert chain.frames == [data], chain.frames
    assert chain.pixels[0][2] == (0xA5, 0x3C, 0x81)
    check_timing(chain, sym)
    assert len(chain.highs) == 8 * len(data)
    first_rise, last_fall = chain.highs[0][0] - chain.highs[0][1], chain.highs[-1][0]
    during = [c for c in log.cycles if first_rise < c < last_fall]
    assert len(during) == 18 and not log.dropped, (len(during), log.dropped)
    assert await spi.drain_outbox(0) == [OK]
    assert (int(dut.uo_out.value) >> 2) & 1 == 0          # line idle low


@cocotb.test(skip=GL)
async def test_lockstep_ws2812_underrun_then_next_frame(dut):
    """The host announces two LEDs and stops after the first. The strip must
    get a clean one-LED frame, the thread must wait for the rest of the
    announced bytes and drop them, the host must read 0xFF, and the next
    frame, a reset time later, must go out whole with status 0x00. Datasheet
    timing for a 33.3 MHz clock (42 cycles per bit) to keep the run short."""
    sym = {"WS_T0H": 14, "WS_T1H": 26, "WS_BIT": 42, "WS_RESET": 1700}
    words, syms = image(sym)
    a, b = [0xC3, 0x00, 0xFF, 0x11, 0x22, 0x33], [0x5A, 0xFF, 0x00]
    chain = wsm.Ws2812Chain(pin=DOUT, **wsm.cycles_from_ns(30))
    seen = {}

    async def after(spi):
        await ClockCycles(dut.clk, sym["WS_RESET"] + 3 * 8 * sym["WS_BIT"] + 300)
        # starved: blocked in the drain loop, nothing reported yet
        seen["stat"] = await spi.read(R_STAT, 1)
        seen["pc"] = await spi.read(R_PC0, 2)
        seen["levels"] = await spi.levels()
        await spi.write(R_INBOX0, a[3:] + frame(b))

    ls, spi = await lockstep_firmware(dut, words, 500, models=[chain],
                                      inbox0=frame(a)[:5], after_load=after)
    t = ls.m.threads[0]
    await ls.run(6000, until=lambda l: len(chain.frames) == 2 and t.blocked and t.pc == syms["ws_frame"])
    dut._log.info("ws2812 underrun: %d cycles in lockstep, %d instructions" % (ls.cycle, ls.retired))
    assert seen["stat"] == [0b010001] and seen["pc"] == [syms["ws_underrun"], 0], seen
    assert seen["levels"][:2] == [0, 0], seen
    assert chain.errors == [], chain.errors[:3]
    assert chain.frames == [a[:3], b], chain.frames
    assert chain.pixels == [[(0xC3, 0x00, 0xFF)], [(0x5A, 0xFF, 0x00)]]
    check_timing(chain, sym)
    assert len(chain.gaps) == 1
    assert await spi.drain_outbox(0) == [UNDERRUN, OK]
    assert (await spi.levels())[0] == 0
