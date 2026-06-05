"""Unit tests for tools/stvt_dedup.py.

The dedup logic builds on two cheap primitives — size bucketing and
3-slice fingerprinting — and then groups by hash. The tricky parts are:

  1. Files identical in size but different in content must NOT be
     grouped.
  2. Files identical in size AND content (across all 3 sample points)
     SHOULD be grouped, with the oldest mtime marked as the keeper.
  3. delete_extras with --dry-run must not actually unlink anything.

All tests build a throwaway directory of synthetic .ts files. None of
them touch the live recordings tree.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import stvt_dedup as dedup   # noqa: E402


class TestSampleHash(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write(self, name: str, content: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(content)
        return path

    def test_identical_small_files_have_same_hash(self):
        a = self._write("a.ts", b"hello world" * 100)
        b = self._write("b.ts", b"hello world" * 100)
        self.assertEqual(
            dedup.sample_hash(a, a.stat().st_size),
            dedup.sample_hash(b, b.stat().st_size),
        )

    def test_different_small_files_differ(self):
        a = self._write("a.ts", b"a" * 1000)
        b = self._write("b.ts", b"b" * 1000)
        self.assertNotEqual(
            dedup.sample_hash(a, a.stat().st_size),
            dedup.sample_hash(b, b.stat().st_size),
        )

    def test_large_file_sample_picks_up_middle_diff(self):
        # Build two 1 MB files where only the middle 64 KB differs.
        # 3-slice sampling should catch the difference.
        head = b"H" * 200_000
        tail = b"T" * 200_000
        mid_a = b"A" * 600_000
        mid_b = b"B" * 600_000
        a = self._write("a.ts", head + mid_a + tail)
        b = self._write("b.ts", head + mid_b + tail)
        self.assertNotEqual(
            dedup.sample_hash(a, a.stat().st_size),
            dedup.sample_hash(b, b.stat().st_size),
        )


class TestGroupBySize(unittest.TestCase):
    def test_only_buckets_with_two_plus_returned(self):
        entries = [
            dedup.Entry(path="a", size=100, mtime=0, hash=""),
            dedup.Entry(path="b", size=100, mtime=0, hash=""),
            dedup.Entry(path="c", size=200, mtime=0, hash=""),
        ]
        groups = dedup.group_by_size(entries)
        self.assertIn(100, groups)
        self.assertNotIn(200, groups)
        self.assertEqual(len(groups[100]), 2)


class TestFingerprintGroups(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _entry(self, name: str, content: bytes, mtime: float | None = None):
        path = self.dir / name
        path.write_bytes(content)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        st = path.stat()
        return dedup.Entry(path=str(path), size=st.st_size,
                           mtime=st.st_mtime, hash="")

    def test_two_identical_files_group_together(self):
        a = self._entry("a.ts", b"content" * 1000, mtime=100)
        b = self._entry("b.ts", b"content" * 1000, mtime=200)
        groups = dedup.fingerprint_groups({a.size: [a, b]}, full=False)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_same_size_different_content_split(self):
        # Force a guaranteed sampling mismatch by making files >> 192 KB
        # so we hit the 3-slice path, and differ at the head sample.
        big = 300_000
        a = self._entry("a.ts", b"A" * big)
        b = self._entry("b.ts", b"B" * big)
        groups = dedup.fingerprint_groups({a.size: [a, b]}, full=False)
        # Different hashes -> two singleton "groups" -> dropped (need >=2)
        self.assertEqual(groups, [])


class TestDeleteExtras(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_pair(self):
        """Two identical files, with explicit mtimes so the older one is
        the deterministic keeper."""
        a = self.dir / "older.ts"
        b = self.dir / "newer.ts"
        a.write_bytes(b"same content" * 5000)
        b.write_bytes(b"same content" * 5000)
        os.utime(a, (1_000_000, 1_000_000))
        os.utime(b, (2_000_000, 2_000_000))
        return a, b

    def test_dry_run_does_not_delete(self):
        a, b = self._make_pair()
        ea = dedup.Entry(path=str(a), size=a.stat().st_size,
                         mtime=a.stat().st_mtime, hash="h")
        eb = dedup.Entry(path=str(b), size=b.stat().st_size,
                         mtime=b.stat().st_mtime, hash="h")
        n = dedup.delete_extras([[ea, eb]], dry=True)
        self.assertEqual(n, 0)
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())

    def test_real_delete_removes_newer_keeps_older(self):
        a, b = self._make_pair()
        ea = dedup.Entry(path=str(a), size=a.stat().st_size,
                         mtime=a.stat().st_mtime, hash="h")
        eb = dedup.Entry(path=str(b), size=b.stat().st_size,
                         mtime=b.stat().st_mtime, hash="h")
        n = dedup.delete_extras([[ea, eb]], dry=False)
        self.assertEqual(n, 1)
        self.assertTrue(a.exists(), "older file should be kept")
        self.assertFalse(b.exists(), "newer file should be deleted")


class TestRender(unittest.TestCase):
    def test_no_duplicates_message(self):
        self.assertIn("no duplicates", dedup.render([]))

    def test_renders_keep_and_extra_labels(self):
        e1 = dedup.Entry(path="/x/a.ts", size=100, mtime=1.0, hash="h")
        e2 = dedup.Entry(path="/x/b.ts", size=100, mtime=2.0, hash="h")
        out = dedup.render([[e1, e2]])
        self.assertIn("KEEP", out)
        self.assertIn("extra", out)
        self.assertIn("a.ts", out)
        self.assertIn("b.ts", out)


if __name__ == "__main__":
    unittest.main()
