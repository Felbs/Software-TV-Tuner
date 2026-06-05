"""Unit tests for tools/stvt_epg.py — GPS conversion, event lookup,
text helpers, and the grid renderer. No SDR or GNU Radio required.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import stvt_epg as epg   # noqa: E402

LIVE_SCAN = Path.home() / ".tv_tuner" / "scan.json"


class TestGpsConversion(unittest.TestCase):
    """gps_to_unix should subtract the leap-second adjustment after
    adding the epoch offset. Known good: gps_seconds=0 -> 1980-01-06
    UTC minus 18s."""

    def test_zero_gps_to_unix(self):
        self.assertEqual(epg.gps_to_unix(0),
                         epg.GPS_UNIX_OFFSET - epg.GPS_UTC_LEAP_SECONDS)

    def test_monotonic(self):
        # Adding 1 to GPS time should add 1 to unix time.
        a = epg.gps_to_unix(1_000_000_000)
        b = epg.gps_to_unix(1_000_000_001)
        self.assertEqual(b - a, 1)

    def test_offsets_constants_present(self):
        # Sanity: the named constants exist and are reasonable.
        self.assertGreater(epg.GPS_UNIX_OFFSET, 315_000_000)
        self.assertGreaterEqual(epg.GPS_UTC_LEAP_SECONDS, 18)


class TestEventAt(unittest.TestCase):
    """event_at(events, when) returns the show covering `when`, or None."""

    def setUp(self):
        # Three back-to-back events starting at t=100.
        self.events = [
            {"start_unix": 100, "length": 100, "title": "A"},
            {"start_unix": 200, "length": 100, "title": "B"},
            {"start_unix": 300, "length": 100, "title": "C"},
        ]

    def test_before_first_returns_none(self):
        self.assertIsNone(epg.event_at(self.events, 50))

    def test_inside_first_returns_first(self):
        self.assertEqual(epg.event_at(self.events, 150)["title"], "A")

    def test_inside_second_returns_second(self):
        self.assertEqual(epg.event_at(self.events, 250)["title"], "B")

    def test_exact_start_is_inclusive(self):
        self.assertEqual(epg.event_at(self.events, 100)["title"], "A")

    def test_exact_end_is_exclusive(self):
        # End of A is 200; that should be the start of B, not A.
        self.assertEqual(epg.event_at(self.events, 200)["title"], "B")

    def test_after_last_returns_none(self):
        self.assertIsNone(epg.event_at(self.events, 500))

    def test_empty_events_returns_none(self):
        self.assertIsNone(epg.event_at([], 100))


class TestTruncate(unittest.TestCase):
    def test_short_string_unchanged(self):
        self.assertEqual(epg.truncate("hi", 10), "hi")

    def test_long_string_truncated_with_dot(self):
        # truncate(n) returns up to n chars, last char is '.'
        result = epg.truncate("123456789", 5)
        self.assertEqual(len(result), 5)
        self.assertTrue(result.endswith("."))

    def test_n_one_does_not_explode(self):
        # n=1 falls into the short branch (just returns the first char)
        result = epg.truncate("123", 1)
        self.assertEqual(len(result), 1)


class TestRenderGrid(unittest.TestCase):
    """render_grid produces a multiline string. Verify it includes the
    channel headers and survives no-events channels."""

    def test_renders_minimal_channel(self):
        channels = [{
            "rf": 34, "program": 3, "virtual": "4.1",
            "callsign": "WRC", "network": "NBC",
            "events": [],
        }]
        out = epg.render_grid(channels, rf_filter=None,
                              hours=2, color=False, scan_unix=None)
        self.assertIn("4.1", out)
        self.assertIn("WRC", out)
        # Without events every cell should say "(no data)"
        self.assertIn("no data", out)

    def test_rf_filter_excludes_other_channels(self):
        channels = [
            {"rf": 34, "program": 3, "virtual": "4.1",
             "callsign": "WRC", "network": "NBC", "events": []},
            {"rf": 36, "program": 3, "virtual": "5.1",
             "callsign": "WTTG", "network": "Fox", "events": []},
        ]
        out = epg.render_grid(channels, rf_filter=34, hours=2,
                              color=False, scan_unix=None)
        self.assertIn("WRC", out)
        self.assertNotIn("WTTG", out)

    def test_empty_channels_returns_no_match_message(self):
        out = epg.render_grid([], rf_filter=None, hours=2,
                              color=False, scan_unix=None)
        self.assertIn("no channels", out)


@unittest.skipUnless(LIVE_SCAN.exists(),
                     f"requires real scan at {LIVE_SCAN}")
class TestLiveScanIntegration(unittest.TestCase):
    """End-to-end: load the user's scan.json and render the grid."""

    def test_load_epg_returns_channels(self):
        channels, scan_unix = epg.load_epg()
        self.assertIsInstance(channels, list)
        self.assertGreater(len(channels), 0,
                           "real scan should yield at least one channel")
        # Every channel should have a virtual that parses as M.N.
        for ch in channels:
            major, _, minor = ch["virtual"].partition(".")
            self.assertTrue(major.isdigit(), f"bad major in {ch['virtual']}")
            self.assertTrue(minor.isdigit() if minor else True,
                            f"bad minor in {ch['virtual']}")

    def test_grid_render_does_not_raise(self):
        channels, scan_unix = epg.load_epg()
        out = epg.render_grid(channels, rf_filter=None,
                              hours=2, color=False, scan_unix=scan_unix)
        self.assertIsInstance(out, str)
        self.assertGreater(len(out), 0)


if __name__ == "__main__":
    unittest.main()
