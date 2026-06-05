"""Unit tests for tools/stvt_schedule.py.

Schedule has a live queue at ~/.tv_tuner/schedule.json which the daemon
actively reads/writes. These tests redirect QUEUE_PATH to a temp file
so they never touch the real queue, then exercise the pure queue
operations: add, dedupe, conflict detection, ID generation, time
formatting, and the channel-snapshot grouping.

IMPORTANT: this test suite imports stvt_schedule, which itself imports
tv_tuner and stvt_multirec to get FFMPEG path + tail helpers. Those
imports succeed without spawning the SDR — we only call pure
functions in tests.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import stvt_schedule as sched   # noqa: E402

LIVE_SCAN = Path.home() / ".tv_tuner" / "scan.json"


# ----------------- fixture helpers -----------------

def _temp_queue_path():
    """Patch sched.QUEUE_PATH to a fresh temp file. Returns Path; the
    caller is responsible for cleanup."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tmp.close()
    Path(tmp.name).unlink(missing_ok=True)
    return Path(tmp.name)


def _make_entry(eid: str, rf: int, prog: int, start: int, length: int,
                title: str = "x", status: str = "pending") -> dict:
    return {
        "id":         eid,
        "rf":         rf,
        "program":    prog,
        "virtual":    f"{rf}.{prog}",
        "callsign":   "TEST",
        "title":      title,
        "start_unix": start,
        "end_unix":   start + length,
        "length_sec": length,
        "status":     status,
    }


# ----------------- tests -----------------

class TestGpsConversion(unittest.TestCase):
    def test_gps_to_unix_monotonic(self):
        a = sched.gps_to_unix(1_000_000_000)
        b = sched.gps_to_unix(1_000_000_001)
        self.assertEqual(b - a, 1)


class TestMakeId(unittest.TestCase):
    """make_id slugs the title and stamps the start time so two
    recordings of the same show on different days have different IDs."""

    def test_id_contains_rf_and_program(self):
        eid = sched.make_id(34, 3, 1717520400, "Some Show")
        self.assertIn("rf34", eid)
        self.assertIn("p3", eid)

    def test_id_slugs_special_chars(self):
        eid = sched.make_id(34, 3, 1717520400, "Hello! World? (live)")
        # Only alnum + underscore in the slug portion
        slug = eid.split("_p3_", 1)[1]
        self.assertTrue(all(c.isalnum() or c == "_" for c in slug),
                        f"bad chars in slug: {slug!r}")

    def test_id_truncates_long_title(self):
        eid = sched.make_id(34, 3, 1717520400, "x" * 100)
        slug = eid.split("_p3_", 1)[1]
        self.assertLessEqual(len(slug), 24)

    def test_two_different_starts_yield_different_ids(self):
        a = sched.make_id(34, 3, 1717520400, "Show")
        b = sched.make_id(34, 3, 1717520400 + 86400, "Show")
        self.assertNotEqual(a, b)


class TestQueueOperations(unittest.TestCase):
    """Add / load / save / dedupe round-trips via a redirected queue
    path."""

    def setUp(self):
        self.tmp = _temp_queue_path()
        self.patcher = mock.patch.object(sched, "QUEUE_PATH", self.tmp)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.unlink(missing_ok=True)

    def test_load_empty_returns_empty_list(self):
        self.assertEqual(sched.load_queue(), [])

    def test_save_then_load_roundtrip(self):
        entry = _make_entry("id1", 34, 3, 1_700_000_000, 1800)
        sched.save_queue([entry])
        loaded = sched.load_queue()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["id"], "id1")

    def test_add_to_queue_appends_and_sorts_by_start(self):
        later = _make_entry("late", 34, 3, 2_000_000_000, 1800)
        earlier = _make_entry("early", 34, 3, 1_000_000_000, 1800)
        self.assertEqual(sched.add_to_queue(later), "added")
        self.assertEqual(sched.add_to_queue(earlier), "added")
        loaded = sched.load_queue()
        self.assertEqual([q["id"] for q in loaded], ["early", "late"])

    def test_duplicate_add_returns_duplicate(self):
        entry = _make_entry("id1", 34, 3, 1_700_000_000, 1800)
        sched.add_to_queue(entry)
        self.assertEqual(sched.add_to_queue(entry), "duplicate")
        self.assertEqual(len(sched.load_queue()), 1)

    def test_load_malformed_json_returns_empty(self):
        self.tmp.write_text("not json", encoding="utf-8")
        self.assertEqual(sched.load_queue(), [])


class TestDetectConflicts(unittest.TestCase):
    """Conflict rule: overlapping time + different RF = conflict.
    Same RF is fine (multirec handles all programs in one tuning)."""

    def test_same_rf_overlap_is_not_conflict(self):
        existing = [_make_entry("a", 34, 3, 100, 1800)]
        new = _make_entry("b", 34, 5, 200, 1800)
        self.assertEqual(sched.detect_conflicts(existing, new), [])

    def test_different_rf_overlap_is_conflict(self):
        existing = [_make_entry("a", 34, 3, 100, 1800)]
        new = _make_entry("b", 36, 1, 200, 1800)
        conflicts = sched.detect_conflicts(existing, new)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["id"], "a")

    def test_different_rf_no_overlap_is_not_conflict(self):
        existing = [_make_entry("a", 34, 3, 100, 100)]
        new = _make_entry("b", 36, 1, 1000, 1800)
        self.assertEqual(sched.detect_conflicts(existing, new), [])

    def test_done_entries_ignored(self):
        existing = [_make_entry("a", 34, 3, 100, 1800, status="done")]
        new = _make_entry("b", 36, 1, 200, 1800)
        self.assertEqual(sched.detect_conflicts(existing, new), [])

    def test_skipped_entries_ignored(self):
        existing = [_make_entry("a", 34, 3, 100, 1800, status="skipped")]
        new = _make_entry("b", 36, 1, 200, 1800)
        self.assertEqual(sched.detect_conflicts(existing, new), [])

    def test_self_id_excluded(self):
        existing = [_make_entry("same", 34, 3, 100, 1800)]
        new = _make_entry("same", 36, 1, 200, 1800)
        self.assertEqual(sched.detect_conflicts(existing, new), [])


class TestFmtTime(unittest.TestCase):
    def test_renders_known_timestamp(self):
        # Mid-2024 epoch second; we don't pin the exact format because it
        # depends on locale, but we know it should contain the year and
        # an AM/PM token.
        out = sched.fmt_time(1717520400)
        self.assertIsInstance(out, str)
        self.assertGreater(len(out), 0)


class TestMuxSummary(unittest.TestCase):
    """_mux_summary derives a human-readable label from a list of
    channel rows on the same RF."""

    def test_single_major_group_uses_that_callsign(self):
        rows = [
            {"virtual": "4.1", "callsign": "WRC"},
            {"virtual": "4.2", "callsign": "COZI"},
            {"virtual": "4.3", "callsign": "TBD"},
        ]
        title, sub = sched._mux_summary(34, rows)
        self.assertIn("RF 34", title)
        self.assertIn("WRC", title)
        self.assertIn("4.x", title)
        self.assertIn("3 channels", sub)

    def test_multi_major_groups_show_both(self):
        rows = [
            {"virtual": "4.1", "callsign": "WRC"},
            {"virtual": "4.2", "callsign": "COZI"},
            {"virtual": "44.1", "callsign": "TELE"},
        ]
        title, _ = sched._mux_summary(34, rows)
        self.assertIn("4.x", title)
        self.assertIn("44.x", title)

    def test_empty_rows_returns_no_channels(self):
        title, sub = sched._mux_summary(34, [])
        self.assertIn("RF 34", title)
        self.assertIn("no channels", sub)


@unittest.skipUnless(LIVE_SCAN.exists(),
                     f"requires real scan at {LIVE_SCAN}")
class TestLiveScanIntegration(unittest.TestCase):
    """End-to-end: build_channel_snapshot must accept the real scan
    without raising, and find_channel should resolve virtual numbers
    back to (channel, psip)."""

    def test_build_channel_snapshot_succeeds(self):
        with open(sched.SCAN_PATH, encoding="utf-8") as f:
            scan = json.load(f)
        rows = sched.build_channel_snapshot(scan)
        self.assertIsInstance(rows, list)
        # Some channel should yield a row
        self.assertGreater(len(rows), 0)
        # Every row has the expected keys
        for r in rows:
            for key in ("rf", "program", "virtual", "callsign",
                        "on_now", "next"):
                self.assertIn(key, r)

    def test_find_channel_resolves_first_row(self):
        with open(sched.SCAN_PATH, encoding="utf-8") as f:
            scan = json.load(f)
        rows = sched.build_channel_snapshot(scan)
        if not rows:
            self.skipTest("no rows in scan")
        first = rows[0]
        c, p = sched.find_channel(scan, first["virtual"])
        self.assertIsNotNone(c, f"could not resolve {first['virtual']}")
        self.assertIsNotNone(p)


if __name__ == "__main__":
    unittest.main()
