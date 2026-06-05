"""Unit tests for tools/stvt_capacity.py — the per-mux analysis and
text-renderer functions. No SDR or GNU Radio required.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# Make tools/ importable so we can pull in stvt_capacity as a module.
TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import stvt_capacity as cap   # noqa: E402

REPO = Path(__file__).resolve().parents[2]
LIVE_SCAN = Path.home() / ".tv_tuner" / "scan.json"


# ----------------- fixture builders -----------------

def make_program(pnum: int, langs: list[str], video=("MPEG2", 1080, 60)):
    """Build a programs[i] dict in the shape scan.json uses."""
    codec, height, fps = video
    return {
        "program_num":  pnum,
        "video_codec":  codec,
        "video_height": height,
        "video_fps":    fps,
        "audio_streams": [{"lang": lang} for lang in langs],
    }


def make_psip_channel(pnum: int, major: int, minor: int, short: str):
    return {
        "program_number": pnum,
        "major": major,
        "minor": minor,
        "short_name": short,
    }


def make_mux(rf: int, callsign: str, programs: list[dict],
             psip_channels: list[dict] | None = None):
    return {
        "rf":         rf,
        "callsign":   callsign,
        "programs":   programs,
        "psip":       {"channels": psip_channels or []},
        "pat_count":  len(programs),
    }


# ----------------- tests -----------------

class TestAnalyzeMux(unittest.TestCase):
    """Pure-function tests for analyze_mux: no I/O, no scan.json."""

    def test_counts_programs_and_audio_streams(self):
        chan = make_mux(34, "WRC", [
            make_program(3, ["eng"]),
            make_program(4, ["eng", "spa"]),
        ], psip_channels=[
            make_psip_channel(3, 4, 1, "WRC"),
            make_psip_channel(4, 4, 2, "COZI"),
        ])
        result = cap.analyze_mux(chan)
        self.assertEqual(result["rf"], 34)
        self.assertEqual(result["program_count"], 2)
        self.assertEqual(result["total_audio_tracks"], 3)
        self.assertTrue(result["has_sap"], "eng+spa program -> SAP flag")
        self.assertFalse(result["has_likely_dvs"])

    def test_dvs_heuristic_fires_on_3plus_audio(self):
        chan = make_mux(35, "ION", [
            make_program(1, ["eng", "spa", "eng"]),  # 3 streams -> DVS hint
        ], psip_channels=[
            make_psip_channel(1, 8, 1, "ION"),
        ])
        result = cap.analyze_mux(chan)
        self.assertTrue(result["has_likely_dvs"])

    def test_virtual_uses_psip_major_minor(self):
        chan = make_mux(34, "WRC", [make_program(3, ["eng"])],
                        psip_channels=[
                            make_psip_channel(3, 4, 1, "WRC"),
                        ])
        result = cap.analyze_mux(chan)
        self.assertEqual(result["programs"][0]["virtual"], "4.1")
        self.assertEqual(result["programs"][0]["callsign"], "WRC")

    def test_virtual_falls_back_when_psip_missing(self):
        chan = make_mux(34, "WRC", [make_program(3, ["eng"])],
                        psip_channels=[])
        result = cap.analyze_mux(chan)
        # No PSIP -> "<rf>.<pnum>"
        self.assertEqual(result["programs"][0]["virtual"], "34.3")
        self.assertEqual(result["programs"][0]["callsign"], "WRC")

    def test_empty_programs_returns_zero_counts(self):
        chan = make_mux(40, "EMPTY", [])
        result = cap.analyze_mux(chan)
        self.assertEqual(result["program_count"], 0)
        self.assertEqual(result["total_audio_tracks"], 0)
        self.assertFalse(result["has_sap"])
        self.assertFalse(result["has_likely_dvs"])


class TestRenderText(unittest.TestCase):
    """render_text is a long string formatter — verify it produces
    coherent output, doesn't blow up on edge inputs, and respects
    the --mux filter."""

    def _report(self, muxes):
        return {
            "hard_limits": {
                "tuners": 1,
                "concurrent_muxes": 1,
                "sdr_bandwidth_mhz": 10,
                "mux_throughput_mbit_s": 19.39,
            },
            "muxes": muxes,
            "max_simultaneous_programs":
                max((m["program_count"] for m in muxes), default=0),
        }

    def test_empty_muxes_says_no_scan(self):
        text = cap.render_text(self._report([]), mux_filter=None)
        self.assertIn("no muxes locked", text)

    def test_renders_hard_limits_section(self):
        m = cap.analyze_mux(make_mux(34, "WRC",
                                     [make_program(3, ["eng"])],
                                     [make_psip_channel(3, 4, 1, "WRC")]))
        text = cap.render_text(self._report([m]), mux_filter=None)
        self.assertIn("HARD LIMITS", text)
        self.assertIn("19.39", text)
        self.assertIn("WRC", text)
        self.assertIn("4.1", text)

    def test_mux_filter_limits_output(self):
        muxes = []
        for rf, call in [(34, "WRC"), (36, "WTTG")]:
            muxes.append(cap.analyze_mux(
                make_mux(rf, call, [make_program(1, ["eng"])],
                         [make_psip_channel(1, rf//6, 1, call)])
            ))
        text = cap.render_text(self._report(muxes), mux_filter=34)
        self.assertIn("WRC", text)
        self.assertNotIn("WTTG", text)


@unittest.skipUnless(LIVE_SCAN.exists(),
                     f"requires real scan at {LIVE_SCAN}")
class TestLiveScanIntegration(unittest.TestCase):
    """End-to-end check against the user's actual scan.json. Asserts
    that the file we load round-trips through analyze_mux without
    raising on any channel. Catches schema drift early."""

    def test_every_decoded_channel_analyzes_cleanly(self):
        data = json.loads(LIVE_SCAN.read_text(encoding="utf-8"))
        decoded = [c for c in data.get("channels", [])
                   if c.get("callsign") and c.get("callsign") not in ("?", "None")
                   and c.get("programs")]
        self.assertGreater(len(decoded), 0,
                           "scan should have at least one decoded channel")
        for c in decoded:
            try:
                r = cap.analyze_mux(c)
            except (KeyError, TypeError, ValueError) as exc:
                self.fail(f"analyze_mux raised on RF{c.get('rf')} "
                          f"{c.get('callsign')}: {exc}")
            self.assertEqual(r["rf"], c["rf"])
            self.assertEqual(r["program_count"], len(c["programs"]))

    def test_total_audio_tracks_is_nonneg(self):
        data = json.loads(LIVE_SCAN.read_text(encoding="utf-8"))
        for c in data.get("channels", []):
            if c.get("callsign") and c.get("programs"):
                r = cap.analyze_mux(c)
                self.assertGreaterEqual(r["total_audio_tracks"], 0)


if __name__ == "__main__":
    unittest.main()
