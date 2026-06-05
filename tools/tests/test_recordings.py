"""Unit tests for tools/stvt_recordings.py.

Covers the pure logic — filename clustering by multirec timestamp
suffix, junk-file detection (STUB/SHORT/EMPTY), and Session helpers.
Does NOT spawn VLC or touch the real recordings/ folder; everything
runs against synthesized FileInfo objects or a temp directory.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import stvt_recordings as recs   # noqa: E402


def _f(name: str, size_mb: float = 100.0, duration_s: float | None = 600.0,
       kind: str = "program", virtual: str | None = None,
       short_name: str | None = None, note: str = "OK") -> recs.FileInfo:
    """Build a FileInfo without touching the filesystem. Tests can lie
    about size/note however they want."""
    return recs.FileInfo(
        path=Path(name),
        name=name,
        size_mb=size_mb,
        mtime=0.0,
        duration_s=duration_s,
        kind=kind,
        virtual=virtual,
        short_name=short_name,
        note=note,
    )


# ── stamp extraction ─────────────────────────────────────────────────


class TestSessionStamp(unittest.TestCase):
    def test_full_file_stamp(self):
        self.assertEqual(
            recs.session_stamp("mux_FULL_rf31_20260605_012429.ts"),
            "20260605_012429",
        )

    def test_program_file_stamp(self):
        self.assertEqual(
            recs.session_stamp("mux_p1_26_1_WETA-HD_20260605_012429.ts"),
            "20260605_012429",
        )

    def test_program_with_dashed_callsign_stamp(self):
        # WETA-HD has a dash; the regex anchors on the trailing date_time.ts
        self.assertEqual(
            recs.session_stamp("mux_p3_5_1_WTTG-DT_20260604_224641.ts"),
            "20260604_224641",
        )

    def test_unrecognized_filename(self):
        self.assertIsNone(recs.session_stamp("notarecording.ts"))
        self.assertIsNone(recs.session_stamp("hello_world.ts"))


# ── session clustering ──────────────────────────────────────────────


class TestClusterIntoSessions(unittest.TestCase):
    """Files sharing a YYYYMMDD_HHMMSS suffix belong to one session.
    The FULL file (if present) provides the session's RF and floats to
    the top of the file list."""

    def test_same_stamp_clusters(self):
        files = [
            _f("mux_FULL_rf31_20260605_012429.ts", kind="full"),
            _f("mux_p1_26_1_WETA-HD_20260605_012429.ts",
               virtual="26.1", short_name="WETA-HD"),
            _f("mux_p2_26_2_WETA_UK_20260605_012429.ts",
               virtual="26.2", short_name="WETA_UK"),
        ]
        sessions = recs.cluster_into_sessions(files)
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s.stamp, "20260605_012429")
        self.assertEqual(len(s.files), 3)
        # FULL first in display order
        self.assertEqual(s.files[0].kind, "full")
        # RF parsed off the FULL filename
        self.assertEqual(s.rf, 31)

    def test_different_stamps_split(self):
        files = [
            _f("mux_FULL_rf31_20260605_010000.ts", kind="full"),
            _f("mux_FULL_rf36_20260605_020000.ts", kind="full"),
        ]
        sessions = recs.cluster_into_sessions(files)
        self.assertEqual(len(sessions), 2)
        # Newest session first
        self.assertEqual(sessions[0].stamp, "20260605_020000")
        self.assertEqual(sessions[1].stamp, "20260605_010000")

    def test_session_without_full_file(self):
        # Some sessions never produce a FULL file (multirec chain
        # crashed early). Session should still exist, just with rf=None.
        files = [
            _f("mux_p3_5_1_WTTG-DT_20260605_010000.ts",
               virtual="5.1", short_name="WTTG-DT"),
        ]
        sessions = recs.cluster_into_sessions(files)
        self.assertEqual(len(sessions), 1)
        self.assertFalse(sessions[0].has_full)
        self.assertIsNone(sessions[0].rf)

    def test_adhoc_files_each_get_own_session(self):
        # Files that don't match the multirec pattern (manual imports,
        # broken names) shouldn't collide into one giant session.
        files = [
            _f("random_a.ts"),
            _f("random_b.ts"),
        ]
        sessions = recs.cluster_into_sessions(files)
        self.assertEqual(len(sessions), 2)


# ── session helpers ─────────────────────────────────────────────────


class TestSessionProperties(unittest.TestCase):
    def test_started_at_formats_known_stamp(self):
        s = recs.Session(stamp="20260605_012429", rf=31, files=[])
        self.assertEqual(s.started_at, "2026-06-05 01:24:29")

    def test_started_at_falls_back_for_bad_stamp(self):
        s = recs.Session(stamp="adhoc:foo.ts", rf=None, files=[])
        self.assertEqual(s.started_at, "adhoc:foo.ts")

    def test_total_size_sums(self):
        files = [_f("a.ts", size_mb=100.0), _f("b.ts", size_mb=250.5)]
        s = recs.Session(stamp="x", rf=None, files=files)
        self.assertAlmostEqual(s.total_size_mb, 350.5)

    def test_has_full_detects_full_file(self):
        s = recs.Session(stamp="x", rf=None,
                          files=[_f("a.ts", kind="full"),
                                 _f("b.ts", kind="program")])
        self.assertTrue(s.has_full)
        self.assertEqual(s.full_file.name, "a.ts")

    def test_programs_collects_per_program_names(self):
        files = [
            _f("a.ts", kind="full"),
            _f("p1.ts", kind="program", virtual="5.1", short_name="WTTG"),
            _f("p2.ts", kind="program", virtual="5.2", short_name="BUZZR"),
        ]
        s = recs.Session(stamp="x", rf=None, files=files)
        self.assertEqual(s.programs, ["5.1 WTTG", "5.2 BUZZR"])


# ── junk detection ──────────────────────────────────────────────────


class TestJunkDetection(unittest.TestCase):
    """OK + no-probe = keep. Anything else (STUB/SHORT/EMPTY) = sweep."""

    def test_ok_is_not_junk(self):
        self.assertFalse(_f("ok.ts", note="OK").is_junk)

    def test_stub_is_junk(self):
        self.assertTrue(_f("stub.ts", note="STUB (2s)").is_junk)

    def test_short_is_junk(self):
        self.assertTrue(_f("short.ts", note="SHORT (30s)").is_junk)

    def test_empty_is_junk(self):
        self.assertTrue(_f("empty.ts", note="EMPTY (0 bytes)").is_junk)

    def test_noprobe_is_not_junk(self):
        # Without ffprobe we can't be sure — don't auto-sweep.
        self.assertFalse(_f("x.ts", note="no-probe").is_junk)
        self.assertFalse(_f("x.ts", note="no-probe; <1MB").is_junk)

    def test_find_junk_filters_across_sessions(self):
        files_a = [_f("ok.ts", note="OK"),
                   _f("stub.ts", note="STUB (2s)")]
        files_b = [_f("empty.ts", note="EMPTY (0 bytes)"),
                   _f("good.ts", note="OK")]
        sessions = [recs.Session(stamp="a", rf=None, files=files_a),
                    recs.Session(stamp="b", rf=None, files=files_b)]
        junk = recs.find_junk(sessions)
        names = sorted(f.name for f in junk)
        self.assertEqual(names, ["empty.ts", "stub.ts"])


# ── live-directory scan ─────────────────────────────────────────────


class TestScanDir(unittest.TestCase):
    """scan_dir walks a directory and builds FileInfo. We can't supply
    a real ffprobe in CI, so we pass ffprobe=None and check that size +
    classification still come out right."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # 0-byte file -> EMPTY
        (self.dir / "mux_FULL_rf36_20260605_010000.ts").write_bytes(b"")
        # Small "program" file -> no-probe; <1MB (no ffprobe available)
        (self.dir / "mux_p3_5_1_WTTG-DT_20260605_010000.ts").write_bytes(
            b"x" * 1024
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_returns_one_entry_per_ts(self):
        files = recs.scan_dir(self.dir, ffprobe=None, quiet=True)
        self.assertEqual(len(files), 2)
        names = sorted(f.name for f in files)
        self.assertEqual(names,
                          ["mux_FULL_rf36_20260605_010000.ts",
                           "mux_p3_5_1_WTTG-DT_20260605_010000.ts"])

    def test_scan_flags_empty_file(self):
        files = recs.scan_dir(self.dir, ffprobe=None, quiet=True)
        empty = next(f for f in files if f.size_mb == 0)
        self.assertEqual(empty.note, "EMPTY (0 bytes)")

    def test_scan_and_cluster_round_trip(self):
        files = recs.scan_dir(self.dir, ffprobe=None, quiet=True)
        sessions = recs.cluster_into_sessions(files)
        # Both files share the same timestamp -> one session
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].rf, 36)
        self.assertTrue(sessions[0].has_full)

    def test_scan_empty_dir(self):
        with tempfile.TemporaryDirectory() as empty:
            files = recs.scan_dir(Path(empty), ffprobe=None, quiet=True)
            self.assertEqual(files, [])

    def test_scan_missing_dir(self):
        files = recs.scan_dir(Path("/nonexistent/path/xyz"), ffprobe=None,
                                quiet=True)
        self.assertEqual(files, [])


if __name__ == "__main__":
    unittest.main()
