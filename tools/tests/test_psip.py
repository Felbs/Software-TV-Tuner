"""Unit tests for tools/atsc_psip.py — pure-Python ATSC PSIP parsing.

Covers the bounded helpers that take hand-crafted byte arrays as input
(parse_atsc_text, parse_descriptor_list, parse_content_advisory,
gps_to_datetime, find_current_event). The full extract_psip pipeline
needs a real .ts capture to test end-to-end; that's exercised in
production via tv_tuner / stvt_epg and not duplicated here.

All synthetic inputs follow ATSC A/65 §6.10 (multiple_string_structure)
and §6.7 (content_advisory_descriptor) so the tests double as a sanity
spec for the parser.
"""
from __future__ import annotations

import datetime
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import atsc_psip as psip   # noqa: E402


def _mss_ascii(text: str, lang: bytes = b"eng") -> bytes:
    """Build a single-string single-segment multiple_string_structure with
    compression=0, mode=0x00 (ASCII), payload = text encoded as bytes."""
    body = text.encode("ascii")
    return (bytes([1])              # num_strings = 1
            + lang                  # 3-byte language code
            + bytes([1])            # num_segments = 1
            + bytes([0x00, 0x00, len(body)])
            + body)


def _mss_utf16be(text: str, lang: bytes = b"eng") -> bytes:
    """Same as _mss_ascii but mode=0x3F (UTF-16BE)."""
    body = text.encode("utf-16-be")
    return (bytes([1])
            + lang
            + bytes([1])
            + bytes([0x00, 0x3F, len(body)])
            + body)


class TestParseAtscTextHappyPath(unittest.TestCase):
    def test_ascii_simple(self):
        self.assertEqual(psip.parse_atsc_text(_mss_ascii("Hello")), "Hello")

    def test_ascii_strips_surrounding_whitespace(self):
        self.assertEqual(psip.parse_atsc_text(_mss_ascii("  Foo  ")), "Foo")

    def test_utf16_be_with_non_ascii(self):
        # "naïve" exercises a high-codepoint character round-trip.
        self.assertEqual(psip.parse_atsc_text(_mss_utf16be("naïve")), "naïve")

    def test_utf16_be_empty_string(self):
        self.assertEqual(psip.parse_atsc_text(_mss_utf16be("")), "")


class TestParseAtscTextRobustness(unittest.TestCase):
    """The parser must never raise; it returns "" on any malformed input."""

    def test_empty_bytes(self):
        self.assertEqual(psip.parse_atsc_text(b""), "")

    def test_zero_strings(self):
        self.assertEqual(psip.parse_atsc_text(bytes([0])), "")

    def test_truncated_after_count(self):
        self.assertEqual(psip.parse_atsc_text(bytes([1, 0x65])), "")

    def test_zero_segments(self):
        # num_strings=1, lang=eng, num_segments=0
        data = bytes([1]) + b"eng" + bytes([0])
        self.assertEqual(psip.parse_atsc_text(data), "")

    def test_unsupported_compression(self):
        # compression_type=0x01 is Huffman; we don't decode it.
        data = (bytes([1]) + b"eng" + bytes([1])
                + bytes([0x01, 0x00, 4]) + b"abcd")
        self.assertEqual(psip.parse_atsc_text(data), "")

    def test_unsupported_mode(self):
        # mode=0x10 isn't one we handle.
        data = (bytes([1]) + b"eng" + bytes([1])
                + bytes([0x00, 0x10, 4]) + b"abcd")
        self.assertEqual(psip.parse_atsc_text(data), "")


class TestParseDescriptorList(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(psip.parse_descriptor_list(b""), [])

    def test_single_zero_length_descriptor(self):
        out = psip.parse_descriptor_list(bytes([0x42, 0x00]))
        self.assertEqual(out, [{"tag": 0x42, "data": b""}])

    def test_two_descriptors_back_to_back(self):
        data = bytes([0x10, 0x02, 0xAA, 0xBB, 0x20, 0x01, 0xCC])
        out = psip.parse_descriptor_list(data)
        self.assertEqual(out, [
            {"tag": 0x10, "data": b"\xaa\xbb"},
            {"tag": 0x20, "data": b"\xcc"},
        ])

    def test_truncated_body_stops_walking(self):
        # Tag says length=5 but only 2 bytes follow — bail out cleanly.
        data = bytes([0x42, 0x05, 0xAA, 0xBB])
        self.assertEqual(psip.parse_descriptor_list(data), [])

    def test_truncated_header_stops_walking(self):
        # First descriptor consumes 3 bytes (tag + len=1 + body). The
        # trailing single byte 0x99 is not enough for another tag/length
        # pair, so the loop exits.
        data = bytes([0x42, 0x01, 0xAA, 0x99])
        out = psip.parse_descriptor_list(data)
        self.assertEqual(out, [{"tag": 0x42, "data": b"\xaa"}])


class TestParseContentAdvisory(unittest.TestCase):
    """content_advisory_descriptor body — header byte = rating_region_count
    in low 6 bits, then per region: 2-byte region+dim count, dim records
    (2 bytes each), then rating_description (len-prefixed MSS)."""

    def test_empty_returns_empty(self):
        self.assertEqual(psip.parse_content_advisory(b""), "")

    def test_us_tv_pg_main_only(self):
        # 1 region, region=0x01 (US), 1 dimension, dim=0, value=4 → TV-PG.
        data = bytes([0x01,                # 1 region
                      0x01, 0x01,          # US region, 1 dim
                      0x00, 0x04,          # dim=0, value=4 (TV-PG)
                      0x00])               # rating_description length=0
        self.assertEqual(psip.parse_content_advisory(data), "TV-PG")

    def test_us_tv_14_with_violence_and_language_flags(self):
        # dim=0 val=5 → TV-14; dim=2 val=1 → V; dim=4 val=1 → L.
        # Letters come back sorted+deduped → "LV" (alphabetical).
        data = bytes([0x01,
                      0x01, 0x03,
                      0x00, 0x05,
                      0x02, 0x01,
                      0x04, 0x01,
                      0x00])
        out = psip.parse_content_advisory(data)
        self.assertEqual(out, "TV-14 LV")

    def test_unknown_region_is_skipped(self):
        # Region 0x05 (not US) with 1 dim — parser should skip 2 bytes
        # of dim data then hit description length and exit cleanly.
        data = bytes([0x01,
                      0x05, 0x01,
                      0xFF, 0xFF,
                      0x00])
        self.assertEqual(psip.parse_content_advisory(data), "")

    def test_truncated_dim_record_stops_cleanly(self):
        # Claims 2 dimensions but only one fits.
        data = bytes([0x01,
                      0x01, 0x02,
                      0x00, 0x04])
        # Should still return main rating from the one complete dim.
        self.assertEqual(psip.parse_content_advisory(data), "TV-PG")


class TestGpsToDatetime(unittest.TestCase):
    """gps_to_datetime: GPS epoch is 1980-01-06 UTC; PSIP times are GPS
    seconds with an 18-second UTC offset (correct since 2017)."""

    def test_gps_zero_is_epoch_minus_leapseconds(self):
        expected = (psip.GPS_EPOCH
                    - datetime.timedelta(seconds=psip.GPS_UTC_LEAP_SECONDS))
        self.assertEqual(psip.gps_to_datetime(0), expected)

    def test_one_gps_second_equals_one_real_second(self):
        a = psip.gps_to_datetime(1_300_000_000)
        b = psip.gps_to_datetime(1_300_000_001)
        self.assertEqual((b - a).total_seconds(), 1)

    def test_returns_utc_aware(self):
        out = psip.gps_to_datetime(1_300_000_000)
        self.assertIsNotNone(out.tzinfo)


class TestFindCurrentEvent(unittest.TestCase):
    def setUp(self):
        # Two events. Times are GPS seconds; pick a known anchor.
        self.now = datetime.datetime(2026, 6, 5, 12, 0, 0,
                                     tzinfo=datetime.timezone.utc)
        # GPS seconds at self.now:
        now_gps = int((self.now - psip.GPS_EPOCH).total_seconds()
                      + psip.GPS_UTC_LEAP_SECONDS)
        self.events = [
            {"event_id": 1, "start_gps": now_gps - 1800,
             "length_sec": 3600, "title": "Currently airing"},
            {"event_id": 2, "start_gps": now_gps + 1800,
             "length_sec": 3600, "title": "Up next"},
        ]

    def test_returns_event_covering_now(self):
        ev = psip.find_current_event(self.events, now=self.now)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["title"], "Currently airing")
        # ~30 minutes (1800 s) remaining.
        self.assertAlmostEqual(ev["remaining_sec"], 1800, delta=2)

    def test_returns_none_when_nothing_airs(self):
        # Move "now" to a time before any event.
        before = self.now - datetime.timedelta(hours=10)
        self.assertIsNone(psip.find_current_event(self.events, now=before))

    def test_returns_none_for_empty_list(self):
        self.assertIsNone(psip.find_current_event([], now=self.now))

    def test_exposes_rating_and_description_when_present(self):
        self.events[0]["rating"] = "TV-PG"
        self.events[0]["description"] = "A test show"
        ev = psip.find_current_event(self.events, now=self.now)
        self.assertEqual(ev["rating"], "TV-PG")
        self.assertEqual(ev["description"], "A test show")

    def test_defaults_rating_and_description_to_empty(self):
        ev = psip.find_current_event(self.events, now=self.now)
        self.assertEqual(ev["rating"], "")
        self.assertEqual(ev["description"], "")


if __name__ == "__main__":
    unittest.main()
