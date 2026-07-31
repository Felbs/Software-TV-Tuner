"""Simulated-hardware tests for sdr_compat — including a fake PlutoSDR.

We do not own a Pluto (stated plainly in issue #2), so this simulation
encodes everything its SoapySDR probe told us: driver=PlutoSDR, one RX,
antenna A_BALANCED only, gain range [0, 71] dB, no IFGR gain element,
no rfgain_sel setting. If these tests pass, the compat layer at least
cannot crash on such a radio and picks sane values; live confirmation
still needs the issue reporter's hardware.

Run:  python tools/tests/test_sdr_compat.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sdr_compat  # noqa: E402


class FakeRange:
    def __init__(self, lo, hi):
        self._lo, self._hi = lo, hi

    def minimum(self):
        return self._lo

    def maximum(self):
        return self._hi


class FakePluto:
    """Mimics the LibreSDR/Pluto from issue #2's SoapySDRUtil --probe."""

    def __init__(self):
        self.calls = []
        self.gain = None
        self.antenna = None

    def setGainMode(self, d, ch, on):
        self.calls.append(("gainmode", on))

    def setGain(self, d, ch, *a):
        if len(a) == 2:                     # named element — Pluto has none
            raise RuntimeError("Unknown gain element: " + str(a[0]))
        self.gain = float(a[0])

    def getGainRange(self, d, ch):
        return FakeRange(0.0, 71.0)

    def writeSetting(self, k, v):
        raise RuntimeError("unknown setting " + k)

    def setAntenna(self, d, ch, name):
        if name != "A_BALANCED":
            raise RuntimeError("unknown antenna " + name)
        self.antenna = name

    def listAntennas(self, d, ch):
        return ["A_BALANCED"]


class FakeRSPdx(FakePluto):
    """SDRplay dialect: IFGR gain element + rfgain_sel setting exist."""

    def setGain(self, d, ch, *a):
        if len(a) == 2 and a[0] == "IFGR":
            self.gain = ("IFGR", float(a[1]))
            return
        raise AssertionError("sdrplay path must use IFGR")

    def writeSetting(self, k, v):
        self.calls.append((k, v))

    def setAntenna(self, d, ch, name):
        if name not in ("Antenna A", "Antenna B", "Antenna C"):
            raise RuntimeError("bad antenna")
        self.antenna = name


def main():
    # --- Pluto: generic gain mapping, hot IFGR 40 -> mid-high gain
    p = FakePluto()
    d = sdr_compat.apply_rx_gain(p, "driver=plutosdr", ifgr=40, rfgain_sel=5)
    assert p.gain is not None and 30 <= p.gain <= 40, (p.gain, d)
    # coldest knob -> lowest gain, hottest -> highest
    sdr_compat.apply_rx_gain(p, "driver=plutosdr", 59, 0)
    assert p.gain == 0.0, p.gain
    sdr_compat.apply_rx_gain(p, "driver=plutosdr", 20, 0)
    assert abs(p.gain - 71.0) < 1e-6, p.gain
    # RSPdx port name gracefully falls back to the Pluto's only antenna
    a = sdr_compat.apply_antenna(p, "Antenna B")
    assert p.antenna == "A_BALANCED" and "A_BALANCED" in a, (p.antenna, a)
    print("PASS pluto-sim: gain mapping + antenna fallback")

    # --- RSPdx: dialect unchanged (regression guard)
    r = FakeRSPdx()
    d = sdr_compat.apply_rx_gain(r, "driver=sdrplay", ifgr=53, rfgain_sel=7)
    assert r.gain == ("IFGR", 53.0), r.gain
    assert ("rfgain_sel", "7") in r.calls, r.calls
    a = sdr_compat.apply_antenna(r, "Antenna B")
    assert r.antenna == "Antenna B", r.antenna
    print("PASS rspdx-sim: IFGR/rfgain_sel dialect preserved")

    # --- resolver honors the env override above all
    import os
    os.environ["STVT_SOAPY_ARGS"] = "driver=remote,remote=1.2.3.4"
    try:
        assert sdr_compat.resolve_soapy_args() == "driver=remote,remote=1.2.3.4"
    finally:
        del os.environ["STVT_SOAPY_ARGS"]
    print("PASS resolver: STVT_SOAPY_ARGS wins")
    print("ALL SIMULATED-HARDWARE TESTS PASS")


if __name__ == "__main__":
    main()
