"""Unit tests for tools/stvt_signal.py.

Only the pure helpers (label thresholds, bar rendering, the metric
formatter) are tested. The actual SDR sweep loop CANNOT be tested
here — it talks to live hardware via SoapySDR.

If sdr_sweep can't be imported (no SoapySDR on this machine), import
of stvt_signal still works — it stashes the error in SWEEP_IMPORT_ERR
and only fails at runtime in monitor_one()/scan_band(). The pure
helpers we test here are independent of that.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import stvt_signal as ssig   # noqa: E402


class TestLabelPilotSnr(unittest.TestCase):
    """Pilot SNR thresholds mirror tv_tuner.run_scan's STRICT/WEAK gates
    (30 / 15 dB). NONE caps at 5 dB so the scanner's noise floor
    doesn't read as a weak signal."""

    def test_above_strict_is_strong(self):
        self.assertEqual(ssig._label_pilot_snr(50.0), "STRONG")
        self.assertEqual(ssig._label_pilot_snr(30.0), "STRONG")

    def test_above_weak_is_good(self):
        self.assertEqual(ssig._label_pilot_snr(25.0), "GOOD")
        self.assertEqual(ssig._label_pilot_snr(15.0), "GOOD")

    def test_above_floor_is_weak(self):
        self.assertEqual(ssig._label_pilot_snr(10.0), "WEAK")
        self.assertEqual(ssig._label_pilot_snr(6.0), "WEAK")

    def test_at_or_below_floor_is_none(self):
        self.assertEqual(ssig._label_pilot_snr(5.0), "NONE")
        self.assertEqual(ssig._label_pilot_snr(0.0), "NONE")
        self.assertEqual(ssig._label_pilot_snr(-20.0), "NONE")


class TestLabelPilotSharp(unittest.TestCase):
    def test_strict_threshold(self):
        self.assertEqual(ssig._label_pilot_sharp(26.25), "STRONG")
        self.assertEqual(ssig._label_pilot_sharp(20.0), "GOOD")
        self.assertEqual(ssig._label_pilot_sharp(8.0), "GOOD")
        self.assertEqual(ssig._label_pilot_sharp(5.0), "WEAK")
        self.assertEqual(ssig._label_pilot_sharp(2.0), "NONE")


class TestLabelVsb(unittest.TestCase):
    def test_excellent_is_high(self):
        self.assertEqual(ssig._label_vsb(10.0), "EXCELLENT")

    def test_strong_above_strict(self):
        self.assertEqual(ssig._label_vsb(2.4), "STRONG")
        self.assertEqual(ssig._label_vsb(5.0), "STRONG")

    def test_good_above_weak_floor(self):
        self.assertEqual(ssig._label_vsb(-10.0), "GOOD")
        self.assertEqual(ssig._label_vsb(-14.0), "GOOD")

    def test_weak_below_floor(self):
        self.assertEqual(ssig._label_vsb(-20.0), "WEAK")


class TestLabelRms(unittest.TestCase):
    def test_strong_for_loud(self):
        self.assertEqual(ssig._label_rms(-20.0), "STRONG")

    def test_good_for_normal(self):
        self.assertEqual(ssig._label_rms(-35.0), "GOOD")

    def test_weak_for_marginal(self):
        self.assertEqual(ssig._label_rms(-55.0), "WEAK")

    def test_none_for_dead(self):
        self.assertEqual(ssig._label_rms(-80.0), "NONE")


class TestBar(unittest.TestCase):
    """The bar always renders BAR_WIDTH chars (hash + dash) and clamps
    out-of-range inputs at 0 / 1."""

    def test_zero(self):
        b = ssig._bar(0.0)
        self.assertEqual(len(b), ssig.BAR_WIDTH)
        self.assertEqual(b.count("#"), 0)

    def test_full(self):
        b = ssig._bar(1.0)
        self.assertEqual(len(b), ssig.BAR_WIDTH)
        self.assertEqual(b.count("#"), ssig.BAR_WIDTH)

    def test_half(self):
        b = ssig._bar(0.5)
        self.assertEqual(len(b), ssig.BAR_WIDTH)
        self.assertAlmostEqual(b.count("#"), ssig.BAR_WIDTH // 2, delta=1)

    def test_overflow_clamps(self):
        self.assertEqual(ssig._bar(5.0), "#" * ssig.BAR_WIDTH)

    def test_underflow_clamps(self):
        self.assertEqual(ssig._bar(-5.0), "-" * ssig.BAR_WIDTH)


class TestRenderBlock(unittest.TestCase):
    """The full readout block always emits 6 lines (1 header + 4 metric
    + 1 footer). The in-place TTY refresh in monitor_one assumes that
    exact line count, so this test pins it."""

    def test_block_has_six_lines(self):
        metrics = {
            "rms_dbfs": -30.0,
            "pilot_snr_db": 40.0,
            "pilot_sharpness_db": 28.0,
            "vsb_asymmetry_db": 5.0,
        }
        block = ssig._render_block(36, 605.0, "Antenna A", metrics, True)
        self.assertEqual(len(block.splitlines()), 6)

    def test_block_handles_neg_inf(self):
        """If the SDR returned no samples (e.g. dropped buffer), the
        metric is -inf. _render_block should emit a "--" placeholder
        instead of crashing on the format spec."""
        metrics = {
            "rms_dbfs": float("-inf"),
            "pilot_snr_db": float("-inf"),
            "pilot_sharpness_db": float("-inf"),
            "vsb_asymmetry_db": float("-inf"),
        }
        block = ssig._render_block(36, 605.0, "Antenna A", metrics, False)
        self.assertEqual(len(block.splitlines()), 6)
        self.assertIn("--", block)

    def test_block_without_bars(self):
        metrics = {"rms_dbfs": -30.0, "pilot_snr_db": 40.0,
                   "pilot_sharpness_db": 28.0, "vsb_asymmetry_db": 5.0}
        block = ssig._render_block(36, 605.0, "Antenna A", metrics, False)
        # No bar chars when bars=False
        self.assertNotIn("#" * 5, block)


if __name__ == "__main__":
    unittest.main()
