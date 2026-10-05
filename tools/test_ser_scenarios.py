"""The serializer scenarios of tools/ser_scenarios.py on the golden model alone.
The same scenarios run on the RTL in lockstep in test/test_ser.py."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ser_scenarios as S  # noqa: E402
from keyersim import Machine  # noqa: E402


def run(words, models, cycles, run_mask=1, pc1=0, stop=None):
    m = Machine()
    m.load(words)
    if pc1:
        m.host_set_pc(1, pc1)
    for t in (0, 1):
        if run_mask & (1 << t):
            m.host_run(t, True)
    got = []
    for _ in range(cycles):
        for mod in models:
            mod.on_cycle(m)
        m.step()
        while True:
            v = m.host_outbox_pop(0)
            if v is None:
                break
            got.append(v)
        if stop and stop(m):
            break
    return m, got


@pytest.mark.parametrize("T", [1, 4, 32])
def test_nrzi_tx(T):
    words, models, cycles, check = S.nrzi_tx(T=T)
    m, got = run(words, models, cycles)
    check(m, got)


@pytest.mark.parametrize("T", [1, 2, 3])
def test_manchester_tx(T):
    words, models, cycles, check = S.manchester_tx(T=T)
    m, got = run(words, models, cycles)
    check(m, got)


@pytest.mark.parametrize("T", [4, 8, 32])
def test_nrzi_rx(T):
    words, models, cycles, check, _ = S.nrzi_rx(T=T)
    m, got = run(words, models, cycles)
    check(m, got)


@pytest.mark.parametrize("T", [2, 4, 9])
def test_manchester_rx(T):
    words, models, cycles, check, _ = S.manchester_rx(T=T)
    m, got = run(words, models, cycles)
    check(m, got)


@pytest.mark.parametrize("seed", [1, 2, 4, 6, 7])
def test_random_runs_and_reaches_the_interesting_states(seed):
    """The random scenario must not hang and must actually exercise the
    engine (otherwise the lockstep run of the same seed proves little)."""
    words, base, models = S.random_scenario(seed)
    m = Machine()
    m.load(words)
    m.host_set_pc(1, base)
    m.host_run(0, True)
    m.host_run(1, True)
    seen = set()
    pcs = [set(), set()]
    for _ in range(12000):
        models[0].on_cycle(m)
        m.step()
        s = m.ser
        seen.add(("tx", s.tx_state))
        seen.add(("rx", s.rx_state))
        for f in ("rx_valid", "rx_end", "rx_ovr", "rx_serr", "rx_ferr", "tx_half"):
            if getattr(s, f):
                seen.add(f)
        for t in (0, 1):
            pcs[t].add(m.threads[t].pc)
    want = {("tx", 1)}
    if seed % 3 == 1:                           # the receive-heavy flavour
        want |= {("rx", 1), "rx_valid"}
    assert want <= seen, seen
    assert len(pcs[0]) > 60 and len(pcs[1]) > 60
