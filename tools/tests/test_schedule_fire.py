"""Regression tests for stvt_schedule.fire_recording's duration math.

Bug context (observed in production 2026-06-05): the scheduler daemon
fired an "America ReFramed" entry at 1:24 AM (start_unix was 12:00 AM,
length 90 min, end_unix 1:30 AM). The original code passed the entry's
full length to multirec --duration, so the recording ran until ~2:55 AM
and stole the SDR from the next scheduled mux (WETA, 2:00 AM start).

Fix: compute_fire_duration_min clamps the recording to the time
remaining until end_unix, with a 1-min floor + 1-min pad.

These tests cover the math directly (without spawning multirec).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import stvt_schedule as sched   # noqa: E402


def _entry(start_unix: int, length_sec: int, eid: str = "test_entry") -> dict:
    return {
        "id":         eid,
        "rf":         31,
        "program":    4,
        "virtual":    "26.1",
        "callsign":   "WETA",
        "title":      "America ReFramed",
        "start_unix": start_unix,
        "end_unix":   start_unix + length_sec,
        "length_sec": length_sec,
        "status":     "pending",
    }


class TestComputeFireDurationOnTime(unittest.TestCase):
    """Daemon fires at (or before) start_unix — should record the full
    original length + 1-min pad, no clamp flag."""

    def test_fire_exactly_at_start(self):
        start = 1_780_000_000
        length = 90 * 60  # 90 min
        e = _entry(start, length)
        duration_min, clamped = sched.compute_fire_duration_min(e, start)
        # Full 90 min + 1-min pad
        self.assertEqual(duration_min, 91)
        self.assertFalse(clamped)

    def test_fire_with_lead_seconds_before_start(self):
        start = 1_780_000_000
        length = 30 * 60
        e = _entry(start, length)
        # Daemon fires 30s early (lead-sec default)
        duration_min, clamped = sched.compute_fire_duration_min(e, start - 30)
        self.assertEqual(duration_min, 31)
        self.assertFalse(clamped)


class TestComputeFireDurationLateFire(unittest.TestCase):
    """Daemon fires AFTER start_unix — duration must clamp to remaining
    window so it doesn't bleed into the next slot."""

    def test_late_fire_clamps_to_remaining(self):
        # Reproduce the observed America-ReFramed scenario:
        # start = 12:00 AM, length = 90 min, fired at 1:24 AM.
        # Original buggy duration: 91 min (would run until ~2:55 AM).
        # Clamped duration: ~6 min (until 1:30 AM end_unix) + 1 pad = 7.
        start = 1_780_632_000           # 12:00 AM
        length = 90 * 60                # 90 min
        e = _entry(start, length)
        fire_at = start + 84 * 60       # 1:24 AM (24 min into the show + 60 of length elapsed)
        duration_min, clamped = sched.compute_fire_duration_min(e, fire_at)
        self.assertTrue(clamped, "clamp flag must trip when remaining < length")
        # remaining = 6 min, // 60 + 1 = 7
        self.assertEqual(duration_min, 7)

    def test_extremely_late_fire_floors_at_one_minute(self):
        # Fire 5 seconds before end_unix — remaining is tiny but we must
        # still pass a non-zero duration to multirec.
        start = 1_780_000_000
        length = 30 * 60
        e = _entry(start, length)
        fire_at = e["end_unix"] - 5
        duration_min, clamped = sched.compute_fire_duration_min(e, fire_at)
        self.assertTrue(clamped)
        self.assertGreaterEqual(duration_min, 1)
        # 60s floor // 60 + 1 = 2
        self.assertEqual(duration_min, 2)

    def test_fire_past_end_still_returns_floor(self):
        # If somehow we fire past end_unix (shouldn't happen — daemon
        # marks as missed — but defensive), still floor at 1 min so we
        # don't pass --duration 0 or negative to multirec.
        start = 1_780_000_000
        length = 30 * 60
        e = _entry(start, length)
        fire_at = e["end_unix"] + 60
        duration_min, clamped = sched.compute_fire_duration_min(e, fire_at)
        self.assertTrue(clamped)
        self.assertGreaterEqual(duration_min, 1)


class TestComputeFireDurationEdgeCases(unittest.TestCase):
    def test_zero_length_entry_still_records_minute(self):
        # Malformed entry with 0 length — defensive floor.
        e = _entry(1_780_000_000, 0)
        duration_min, _ = sched.compute_fire_duration_min(e, 1_780_000_000)
        self.assertGreaterEqual(duration_min, 1)

    def test_short_entry_on_time_no_clamp(self):
        # 1-min show fired on time: 1 + 1 pad = 2 min.
        e = _entry(1_780_000_000, 60)
        duration_min, clamped = sched.compute_fire_duration_min(e, 1_780_000_000)
        self.assertFalse(clamped)
        self.assertEqual(duration_min, 2)


class TestComputeSkipReason(unittest.TestCase):
    """The daemon's 'something else is on the SDR' decision. Single-
    tenant SDR: even a same-RF concurrent fire is illegal because the
    2nd multirec would spawn its own tv_live which can't acquire the
    device, leaving a stub ~30s file that looks done but isn't."""

    def test_no_active_means_safe_to_fire(self):
        new = _entry(1_780_000_000, 1800, eid="new")
        self.assertIsNone(sched.compute_skip_reason(new, [], []))

    def test_same_rf_active_is_same_mux_skip(self):
        active_entry = _entry(1_780_000_000, 1800, eid="active")
        new = dict(_entry(1_780_000_300, 1800, eid="new"))
        new["rf"] = active_entry["rf"]
        reason = sched.compute_skip_reason(new, ["active"], [active_entry])
        self.assertIsNotNone(reason)
        self.assertIn("same-mux", reason)
        self.assertIn(f"RF{new['rf']}", reason)

    def test_different_rf_active_is_different_mux_skip(self):
        active_entry = _entry(1_780_000_000, 1800, eid="active")
        active_entry["rf"] = 34
        new = _entry(1_780_000_300, 1800, eid="new")
        new["rf"] = 36
        reason = sched.compute_skip_reason(new, ["active"], [active_entry])
        self.assertIsNotNone(reason)
        self.assertIn("different-mux", reason)
        self.assertIn("RF34", reason)
        self.assertIn("only one SDR", reason)

    def test_active_id_missing_from_queue_is_skipped(self):
        # Defensive: if active maps to an id that's been pruned from
        # the queue, compute_skip_reason should not raise — just keep
        # scanning the rest of active_ids. (Returns None if no other
        # blocker is found.)
        new = _entry(1_780_000_000, 1800, eid="new")
        self.assertIsNone(
            sched.compute_skip_reason(new, ["ghost_id"], [])
        )

    def test_skip_reason_uses_first_active_match(self):
        # Two actives — the first one wins for the skip-reason message.
        a = _entry(1_780_000_000, 1800, eid="a")
        a["rf"] = 34
        a["title"] = "alpha"
        b = _entry(1_780_000_000, 1800, eid="b")
        b["rf"] = 36
        b["title"] = "beta"
        new = _entry(1_780_000_300, 1800, eid="new")
        new["rf"] = 30
        reason = sched.compute_skip_reason(new, ["a", "b"], [a, b])
        self.assertIsNotNone(reason)
        self.assertIn("RF34", reason)
        self.assertIn("alpha", reason)


if __name__ == "__main__":
    unittest.main()
