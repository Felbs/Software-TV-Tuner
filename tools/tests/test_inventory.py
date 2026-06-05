"""Unit tests for tools/stvt_inventory.py — pure parsing + classify.

Filename parsing is the high-risk part; multirec writes a few different
naming conventions and the classifier flips on byte/duration thresholds
that are easy to get wrong.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import stvt_inventory as inv   # noqa: E402


class TestParseFilename(unittest.TestCase):
    def test_full_mux_name(self):
        meta = inv.parse_filename("mux_FULL_rf31_20260605_012429.ts")
        self.assertEqual(meta["kind"], "full")
        self.assertEqual(meta["rf"], 31)
        self.assertEqual(meta["stamp"], "20260605_012429")

    def test_program_name(self):
        meta = inv.parse_filename(
            "mux_p3_5_1_WTTG-DT_20260604_224641.ts")
        self.assertEqual(meta["kind"], "program")
        self.assertEqual(meta["virtual"], "5.1")
        self.assertEqual(meta["short"], "WTTG-DT")
        self.assertEqual(meta["stamp"], "20260604_224641")

    def test_program_short_name_with_underscore(self):
        # WETA_UK is one of the real-world cases where the short name
        # contains an underscore. The regex should still anchor on the
        # trailing _YYYYMMDD_HHMMSS.ts.
        meta = inv.parse_filename(
            "mux_p2_26_2_WETA_UK_20260605_012429.ts")
        self.assertEqual(meta["kind"], "program")
        self.assertEqual(meta["virtual"], "26.2")
        self.assertEqual(meta["short"], "WETA_UK")

    def test_remote_naming(self):
        meta = inv.parse_filename(
            "224641_v5.1_WTTG_News.ts")
        self.assertEqual(meta["kind"], "remote")
        self.assertEqual(meta["virtual"], "5.1")
        self.assertEqual(meta["short"], "WTTG")
        self.assertEqual(meta["title"], "News")

    def test_unknown_name(self):
        meta = inv.parse_filename("randomthing.ts")
        self.assertEqual(meta["kind"], "unknown")
        self.assertIsNone(meta["rf"])
        self.assertIsNone(meta["virtual"])


class TestStampToIso(unittest.TestCase):
    def test_valid_stamp(self):
        self.assertEqual(inv.stamp_to_iso("20260605_012429"),
                         "2026-06-05 01:24:29")

    def test_none_input(self):
        self.assertIsNone(inv.stamp_to_iso(None))

    def test_invalid_stamp(self):
        self.assertIsNone(inv.stamp_to_iso("not-a-stamp"))


class TestClassify(unittest.TestCase):
    """Threshold checks — be specific because changing these accidentally
    silently reclassifies a chunk of the user's archive."""

    def test_empty_file(self):
        self.assertEqual(inv.classify(0.0, None), "EMPTY (0 bytes)")
        self.assertEqual(inv.classify(0.0, 1234.0), "EMPTY (0 bytes)")

    def test_no_probe_under_1mb_is_flagged(self):
        # No duration info + tiny file = junk-leaning warning
        self.assertEqual(inv.classify(0.5, None), "no-probe; <1MB")

    def test_no_probe_large_file_just_no_probe(self):
        self.assertEqual(inv.classify(100.0, None), "no-probe")

    def test_stub_under_5s(self):
        self.assertTrue(inv.classify(10.0, 3.5).startswith("STUB"))

    def test_short_under_60s(self):
        self.assertTrue(inv.classify(10.0, 30.0).startswith("SHORT"))

    def test_ok_full_recording(self):
        self.assertEqual(inv.classify(1000.0, 3600.0), "OK")


class TestRenderTable(unittest.TestCase):
    def test_renders_minimal_entries(self):
        entries = [inv.Entry(
            path="/tmp/sample.ts", size_mb=12.3,
            mtime_iso="2026-06-05 01:00:00",
            duration_s=60.0, kind="program",
            rf=None, virtual="4.1", short_name="WRC",
            title=None, started_at=None, note="OK",
        )]
        out = inv.render_table(entries)
        self.assertIn("sample.ts", out)
        self.assertIn("4.1", out)
        self.assertIn("WRC", out)
        self.assertIn("OK", out)

    def test_empty_entries(self):
        self.assertIn("no recordings", inv.render_table([]))


if __name__ == "__main__":
    unittest.main()
