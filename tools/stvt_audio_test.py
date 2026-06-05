"""Autonomous audio-language test agent.

Verifies that the picker's `copy_mode` ffmpeg pipeline delivers the
RIGHT audio track(s) for each STVT_AUDIO_LANGUAGE setting — without
any human listening required. The chain itself must already be
producing live.ts; this agent runs ONLY ffmpeg + ffprobe against
that existing capture, so each test takes ~6 seconds instead of the
~30 seconds a full re-tune would.

What it does
============
1. Reads programs + audio tracks from the current live.ts.
2. Picks an HD program that carries multiple language tags
   (typically program 3 on the NBC/Telemundo style mux).
3. For each config in the test plan, runs the SAME ffmpeg command
   the picker would build, into a temp file.
4. ffprobes that temp file: how many audio streams, what language
   tags do they carry, what codec, what RMS volume.
5. Pass/fail vs the expectation: e.g. "spa" should produce exactly
   one audio stream tagged 'spa' with non-silent content.

Usage:
    python tools/stvt_audio_test.py                       # full plan
    python tools/stvt_audio_test.py --program 3 --rf 34   # pin both

Requirements:
    * live.ts must exist and have >100 MB of data (chain already
      converged). Run tv_tuner.py --rf <N> --player vlc first.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIVE_TS = REPO / "tools" / "data" / "tv_live" / "live.ts"
FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
FFPROBE = r"C:\ffmpeg\bin\ffprobe.exe" if sys.platform == "win32" else "ffprobe"


@dataclass
class TestCase:
    name: str
    lang: str
    expected_audio_count: int      # how many audio streams should appear
    expected_languages: list[str]  # required language tag(s) in audio streams
    expected_non_silent: bool      # at least one stream should have audio


@dataclass
class TestResult:
    case: TestCase
    audio_count: int = 0
    languages_seen: list[str] = field(default_factory=list)
    codecs_seen: list[str] = field(default_factory=list)
    mean_volumes_db: list[float] = field(default_factory=list)
    output_bytes: int = 0
    error: str = ""

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        if self.audio_count != self.case.expected_audio_count:
            return False
        for needed_lang in self.case.expected_languages:
            if needed_lang not in self.languages_seen:
                return False
        if self.case.expected_non_silent and self.audio_count > 0:
            # Mean volume of -90 dB or lower = silent. Pass if at least
            # one stream is well above that.
            if not any(v > -50.0 for v in self.mean_volumes_db):
                return False
        return True


# ----------------------------- probing ----------------------------------

def probe_live_ts() -> list[dict]:
    """Return program info from live.ts: [{program, video, audios:[{lang, codec, channels}]}]."""
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-of", "json",
         "-show_programs", "-show_streams", str(LIVE_TS)],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        return []
    data = json.loads(r.stdout)
    for s in data.get("streams", []):
        codec = s.get("codec_name", "")
        is_audio = s.get("codec_type") == "audio"
        if is_audio:
            tags = s.get("tags") or {}
            lang = tags.get("language", "und")
            channels = s.get("channels", 0)
            s["_audio_info"] = {
                "lang": lang, "codec": codec, "channels": channels,
                "index": s.get("index"),
            }
    out = []
    for p in data.get("programs", []):
        pn = p.get("program_num")
        streams = p.get("streams", [])
        audios = []
        has_video = False
        for s in streams:
            if s.get("codec_type") == "video":
                has_video = True
            elif s.get("codec_type") == "audio":
                tags = s.get("tags") or {}
                audios.append({
                    "lang": tags.get("language", "und"),
                    "codec": s.get("codec_name", ""),
                    "channels": s.get("channels", 0),
                    "index": s.get("index"),
                })
        out.append({"program": pn, "has_video": has_video, "audios": audios})
    return out


def pick_target_program(programs: list[dict]) -> dict | None:
    """Find a program with multiple language tags — best test target."""
    candidates = [p for p in programs
                  if p["has_video"]
                  and len({a["lang"] for a in p["audios"]}) >= 2]
    if candidates:
        # Prefer one with both eng and spa
        for p in candidates:
            langs = {a["lang"] for a in p["audios"]}
            if "eng" in langs and "spa" in langs:
                return p
        return candidates[0]
    # Fallback: any HD program with audio
    for p in programs:
        if p["has_video"] and p["audios"]:
            return p
    return None


# ----------------------------- testing ----------------------------------

def run_ffmpeg_with_lang(program: int, lang: str, seconds: int = 5,
                         out_path: Path | None = None) -> Path:
    """Run the same ffmpeg cmd the picker builds for STVT_AUDIO_LANGUAGE=lang,
    captures `seconds` of output to a file. Returns the output path."""
    if out_path is None:
        out_path = Path(tempfile.gettempdir()) / f"stvt_audio_test_{lang}.ts"
    if out_path.exists():
        out_path.unlink()
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+igndts+discardcorrupt",
        "-err_detect", "ignore_err",
        "-analyzeduration", "5000000",
        "-probesize", "5000000",
        "-f", "mpegts", "-i", str(LIVE_TS),
    ]
    if lang in ("", "all"):
        cmd += ["-map", f"0:p:{program}"]
    elif lang in ("eng", "english", "en"):
        cmd += [
            "-map", f"0:p:{program}:v",
            "-map", f"0:p:{program}:a:0",
        ]
    else:
        cmd += [
            "-map", f"0:p:{program}:v",
            "-map", f"0:p:{program}:m:language:{lang}",
        ]
    cmd += [
        "-c", "copy", "-copy_unknown",
        "-t", str(seconds),
        "-f", "mpegts", str(out_path),
    ]
    subprocess.run(cmd, capture_output=True, timeout=seconds * 4)
    return out_path


def analyze_capture(path: Path) -> tuple[list[dict], list[float]]:
    """Probe + measure volume of each audio stream in path. Returns
    ([{lang, codec, channels}], [mean_db_per_audio])."""
    if not path.exists() or path.stat().st_size < 1000:
        return [], []
    # Stream info
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-of", "json",
         "-show_streams", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return [], []
    data = json.loads(r.stdout)
    audios = []
    audio_indexes = []
    for s in data.get("streams", []):
        if s.get("codec_type") == "audio":
            tags = s.get("tags") or {}
            audios.append({
                "lang": tags.get("language", "und"),
                "codec": s.get("codec_name", ""),
                "channels": s.get("channels", 0),
            })
            audio_indexes.append(s.get("index"))
    # Volume per audio stream
    volumes = []
    for i, idx in enumerate(audio_indexes):
        r2 = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", str(path),
             "-map", f"0:{idx}", "-af", "volumedetect",
             "-t", "5", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        mean_vol = -99.0
        for line in r2.stderr.splitlines():
            if "mean_volume:" in line:
                try:
                    mean_vol = float(line.split("mean_volume:")[1]
                                     .split("dB")[0].strip())
                except (ValueError, IndexError):
                    pass
        volumes.append(mean_vol)
    return audios, volumes


def execute_case(case: TestCase, program: int) -> TestResult:
    res = TestResult(case=case)
    try:
        out = run_ffmpeg_with_lang(program, case.lang, seconds=5)
        if not out.exists() or out.stat().st_size < 1000:
            res.error = "ffmpeg produced empty/missing output"
            return res
        res.output_bytes = out.stat().st_size
        audios, vols = analyze_capture(out)
        res.audio_count = len(audios)
        res.languages_seen = [a["lang"] for a in audios]
        res.codecs_seen = [a["codec"] for a in audios]
        res.mean_volumes_db = vols
    except subprocess.TimeoutExpired:
        res.error = "ffmpeg/ffprobe timed out"
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
    return res


# ----------------------------- runner ----------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--program", type=int, default=None,
                    help="Force a specific program number to test")
    args = ap.parse_args()

    if not LIVE_TS.exists() or LIVE_TS.stat().st_size < 100_000_000:
        print("[audio-test] live.ts not ready (need >100 MB). Start the "
              "chain first:", file=sys.stderr)
        print("  python tools/tv_tuner.py --rf 34 --player vlc",
              file=sys.stderr)
        return 1

    print("[audio-test] probing live.ts for available programs + languages...")
    programs = probe_live_ts()
    if not programs:
        print("[audio-test] couldn't read programs from live.ts", file=sys.stderr)
        return 1

    print(f"[audio-test] found {len(programs)} programs:")
    for p in programs:
        if p["has_video"] and p["audios"]:
            langs = sorted({a["lang"] for a in p["audios"]})
            print(f"  program {p['program']}: video + audio({', '.join(langs)})")

    if args.program:
        target = next((p for p in programs if p["program"] == args.program), None)
        if target is None:
            print(f"[audio-test] program {args.program} not found",
                  file=sys.stderr)
            return 1
    else:
        target = pick_target_program(programs)
        if target is None:
            print("[audio-test] no suitable test program found", file=sys.stderr)
            return 1

    target_langs = sorted({a["lang"] for a in target["audios"]})
    print()
    print(f"[audio-test] testing against program {target['program']} "
          f"(languages present: {', '.join(target_langs)})")
    print()

    # Build test plan based on what languages this program actually has.
    plan: list[TestCase] = []
    if "eng" in target_langs:
        plan.append(TestCase(
            name="English-only",
            lang="eng",
            expected_audio_count=1,
            expected_languages=["eng"],
            expected_non_silent=True,
        ))
    if "spa" in target_langs:
        plan.append(TestCase(
            name="Spanish-only",
            lang="spa",
            expected_audio_count=1,
            expected_languages=["spa"],
            expected_non_silent=True,
        ))
    plan.append(TestCase(
        name="All-audio-tracks",
        lang="all",
        expected_audio_count=len(target["audios"]),
        expected_languages=target_langs,
        expected_non_silent=True,
    ))
    # Also test a language that ISN'T present to make sure the no-backstop
    # behavior is correct (should produce 0 audio streams, no English leak).
    missing = next((l for l in ("fra", "deu", "jpn") if l not in target_langs), "fra")
    plan.append(TestCase(
        name=f"Missing-lang-{missing} (must NOT silently fall back to English)",
        lang=missing,
        expected_audio_count=0,
        expected_languages=[],
        expected_non_silent=False,
    ))

    results: list[TestResult] = []
    for case in plan:
        print(f"[audio-test] running: {case.name}  (STVT_AUDIO_LANGUAGE={case.lang})")
        r = execute_case(case, target["program"])
        results.append(r)
        verdict = "PASS" if r.passed else "FAIL"
        marker = "[OK]" if r.passed else "[XX]"
        print(f"  {marker} {verdict}: streams={r.audio_count}  "
              f"langs={r.languages_seen}  codecs={r.codecs_seen}  "
              f"vols(dB)={[round(v, 1) for v in r.mean_volumes_db]}"
              f"  out={r.output_bytes//1024} KB")
        if r.error:
            print(f"      error: {r.error}")
        print()

    print("=" * 60)
    n_pass = sum(1 for r in results if r.passed)
    print(f"[audio-test] result: {n_pass}/{len(results)} passed")
    print()
    for r in results:
        marker = "[OK]" if r.passed else "[XX]"
        print(f"  {marker} {r.case.name}")
        if not r.passed:
            print(f"      expected {r.case.expected_audio_count} audio "
                  f"with langs={r.case.expected_languages}, "
                  f"got {r.audio_count} with langs={r.languages_seen}")
    return 0 if n_pass == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
