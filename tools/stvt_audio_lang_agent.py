"""Autonomous audio-language verification + diagnosis agent.

What it does (no human in the loop)
====================================
1. Inventories every program in live.ts and what audio tracks they carry,
   with language tags + codecs + channel counts.
2. For each multi-language program, extracts 10 seconds of each language
   track to a tagged WAV file in a "samples" directory the user can play
   back manually if they need a sanity check.
3. Compares the waveform of each track against every other track in the
   same program — if two tracks have ~identical waveforms, the
   broadcaster is sending the same audio on both (which would explain
   any "I always hear English" complaint that survives a correct
   pipeline).
4. Runs the exact ffmpeg command the picker uses for each
   STVT_AUDIO_LANGUAGE setting, captures the output, and verifies what
   audio actually comes out (count, codec, language tag, volume).
5. Saves a complete diagnostic report to disk + prints a summary so
   the user can read it when they're back.

This is informational — it does NOT modify code or chain state.
The chain must already be running and have written enough live.ts
data (~50 MB) for the tests to mean anything.

Usage:
    python tools/stvt_audio_lang_agent.py
    python tools/stvt_audio_lang_agent.py --program 3  # force program
    python tools/stvt_audio_lang_agent.py --keep-samples  # keep WAVs
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIVE_TS = REPO / "tools" / "data" / "tv_live" / "live.ts"
SAMPLES_DIR = REPO / "tools" / "data" / "audio_lang_agent"
REPORT_FILE = SAMPLES_DIR / "report.txt"

FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
FFPROBE = r"C:\ffmpeg\bin\ffprobe.exe" if sys.platform == "win32" else "ffprobe"


# --------------------------- helpers --------------------------------------

def log(msg: str, *, report_fh=None):
    print(msg, flush=True)
    if report_fh:
        report_fh.write(msg + "\n")
        report_fh.flush()


def run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """Run cmd, return (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


# --------------------------- inventory ------------------------------------

@dataclass
class AudioStream:
    program: int
    pid: int | None
    lang: str
    codec: str
    channels: int
    stream_index: int


def inventory_live_ts() -> tuple[list[dict], list[AudioStream]]:
    """Return (programs, audio_streams) info from live.ts."""
    rc, out, err = run(
        [FFPROBE, "-v", "error", "-of", "json",
         "-show_programs", "-show_streams", str(LIVE_TS)],
        timeout=30,
    )
    if rc != 0:
        return [], []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [], []
    audios = []
    for s in data.get("streams", []):
        if s.get("codec_type") != "audio":
            continue
        tags = s.get("tags") or {}
        # Find which program owns this stream.
        owning_prog = None
        for p in data.get("programs", []):
            for ps in p.get("streams", []):
                if ps.get("index") == s.get("index"):
                    owning_prog = p.get("program_num")
                    break
            if owning_prog:
                break
        audios.append(AudioStream(
            program=owning_prog or -1,
            pid=s.get("id"),
            lang=tags.get("language", "und"),
            codec=s.get("codec_name", ""),
            channels=s.get("channels", 0),
            stream_index=s.get("index", -1),
        ))
    return data.get("programs", []), audios


# --------------------------- WAV utilities --------------------------------

def extract_wav(program: int, lang: str, duration: int, out_path: Path) -> bool:
    """Extract `duration` seconds of audio from program/lang via ffmpeg
    starting at 5s into the file (skip convergence). Returns True if OK."""
    if out_path.exists():
        out_path.unlink()
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", "5", "-t", str(duration),
        "-i", str(LIVE_TS),
        "-map", f"0:p:{program}:m:language:{lang}",
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        str(out_path),
    ]
    rc, _, _ = run(cmd, timeout=duration * 4 + 10)
    return rc == 0 and out_path.exists() and out_path.stat().st_size > 1000


def read_wav_pcm(path: Path) -> list[int]:
    """Read a 16-bit PCM WAV, return mono samples (average of channels).
    Caps at ~10s of audio so we don't OOM."""
    if not path.exists():
        return []
    with open(path, "rb") as f:
        header = f.read(44)
    if len(header) < 44 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        return []
    # fmt chunk
    n_channels = struct.unpack("<H", header[22:24])[0]
    sr = struct.unpack("<I", header[24:28])[0]
    bits = struct.unpack("<H", header[34:36])[0]
    if bits != 16:
        return []
    max_bytes = 10 * sr * n_channels * 2  # 10 seconds of stereo 16-bit
    with open(path, "rb") as f:
        f.seek(44)
        data = f.read(max_bytes)
    n_samples = len(data) // (2 * n_channels)
    samples = []
    for i in range(n_samples):
        offset = i * 2 * n_channels
        chan_sum = 0
        for c in range(n_channels):
            chan_sum += struct.unpack_from("<h", data, offset + c * 2)[0]
        samples.append(chan_sum // n_channels)
    return samples


def waveform_similarity(a_path: Path, b_path: Path) -> float:
    """Returns 0..1 where 1 = identical waveforms, 0 = uncorrelated.

    Uses normalized cross-correlation peak over the common prefix.
    Computes coarsely (downsample 4x) so a 10-second 48 kHz file
    is still fast.
    """
    a = read_wav_pcm(a_path)
    b = read_wav_pcm(b_path)
    if not a or not b:
        return -1.0
    n = min(len(a), len(b))
    if n < 1000:
        return -1.0
    # Downsample 4x
    a = a[:n:4]
    b = b[:n:4]
    # Normalize
    def norm(xs):
        mean = sum(xs) / len(xs)
        s = [x - mean for x in xs]
        denom = (sum(v*v for v in s)) ** 0.5
        if denom == 0:
            return None
        return [v / denom for v in s], denom
    aa = norm(a); bb = norm(b)
    if aa is None or bb is None:
        return -1.0
    a_n, _ = aa; b_n, _ = bb
    # Cross-correlation at zero lag (these were extracted from same start
    # second so they should be roughly time-aligned).
    s = 0.0
    for i in range(len(a_n)):
        s += a_n[i] * b_n[i]
    return max(-1.0, min(1.0, s))


def audio_level(path: Path) -> dict:
    """ffmpeg volumedetect -> dict with mean_db, max_db, n_samples."""
    rc, _, err = run([
        FFMPEG, "-hide_banner",
        "-i", str(path),
        "-af", "volumedetect", "-f", "null", "-",
    ], timeout=20)
    info = {"mean_db": None, "max_db": None, "n_samples": 0}
    for line in err.splitlines():
        if "mean_volume:" in line:
            try:
                info["mean_db"] = float(line.split("mean_volume:")[1]
                                        .split("dB")[0].strip())
            except (ValueError, IndexError):
                pass
        elif "max_volume:" in line:
            try:
                info["max_db"] = float(line.split("max_volume:")[1]
                                       .split("dB")[0].strip())
            except (ValueError, IndexError):
                pass
        elif "n_samples:" in line:
            try:
                info["n_samples"] = int(line.split("n_samples:")[1].strip())
            except (ValueError, IndexError):
                pass
    return info


# --------------------------- pipeline check -------------------------------

def test_picker_pipeline(program: int, lang: str, out_path: Path) -> dict:
    """Run the EXACT ffmpeg cmd the picker builds for STVT_AUDIO_LANGUAGE=lang,
    capture 8s to out_path, return analysis."""
    if out_path.exists():
        out_path.unlink()
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+igndts+discardcorrupt",
        "-err_detect", "ignore_err",
        "-analyzeduration", "5000000",
        "-probesize", "5000000",
        "-thread_queue_size", "4096",
        "-f", "mpegts", "-i", str(LIVE_TS),
    ]
    if lang in ("", "all"):
        cmd += ["-map", f"0:p:{program}"]
    elif lang in ("eng", "english", "en"):
        cmd += ["-map", f"0:p:{program}:v",
                "-map", f"0:p:{program}:a:0"]
    else:
        cmd += ["-map", f"0:p:{program}:v",
                "-map", f"0:p:{program}:m:language:{lang}"]
    cmd += ["-c", "copy", "-copy_unknown", "-t", "8",
            "-f", "mpegts", str(out_path)]
    rc, _, _ = run(cmd, timeout=45)
    res = {"ok": rc == 0 and out_path.exists() and out_path.stat().st_size > 1000,
           "size": out_path.stat().st_size if out_path.exists() else 0}
    if not res["ok"]:
        return res
    # Probe the captured output
    rc2, out2, _ = run(
        [FFPROBE, "-v", "error", "-of", "json", "-show_streams", str(out_path)],
        timeout=10,
    )
    if rc2 == 0:
        try:
            data = json.loads(out2)
            audios = [s for s in data.get("streams", [])
                      if s.get("codec_type") == "audio"]
            res["audio_count"] = len(audios)
            res["audio_langs"] = [(s.get("tags") or {}).get("language", "und")
                                  for s in audios]
            res["audio_channels"] = [s.get("channels", 0) for s in audios]
        except json.JSONDecodeError:
            pass
    return res


# --------------------------- main runner ----------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--program", type=int, default=None,
                    help="Target program (default: auto-pick multi-lang one)")
    ap.add_argument("--keep-samples", action="store_true",
                    help="Keep extracted WAV files for user listening")
    ap.add_argument("--sample-seconds", type=int, default=10,
                    help="WAV sample length (default 10s)")
    args = ap.parse_args()

    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    report = open(REPORT_FILE, "w", encoding="utf-8")

    log("=" * 72, report_fh=report)
    log(f"STVT audio-language verification agent — "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}",
        report_fh=report)
    log("=" * 72, report_fh=report)
    log("", report_fh=report)

    if not LIVE_TS.exists() or LIVE_TS.stat().st_size < 50_000_000:
        log("ERROR: live.ts is missing or too small (<50 MB). Start the "
            "chain first:", report_fh=report)
        log("    python tools/tv_tuner.py --rf 34 --player vlc",
            report_fh=report)
        return 1

    log(f"live.ts size: {LIVE_TS.stat().st_size/1e6:.1f} MB", report_fh=report)
    log("", report_fh=report)

    # ---- Step 1: inventory ----
    log("[1/4] inventorying programs + audio tracks in live.ts ...",
        report_fh=report)
    programs, audios = inventory_live_ts()
    if not programs:
        log("  ERROR: ffprobe found no programs", report_fh=report)
        return 1

    log(f"  found {len(programs)} programs:", report_fh=report)
    multi_lang_programs = []
    for p in programs:
        pn = p.get("program_num")
        prog_audios = [a for a in audios if a.program == pn]
        langs = sorted({a.lang for a in prog_audios})
        has_video = any(s.get("codec_type") == "video"
                        for s in p.get("streams", []))
        kind = "video+audio" if has_video and prog_audios else \
               ("audio-only" if prog_audios else "video-only")
        log(f"    program {pn:>2}: {kind}  langs=[{', '.join(langs)}]  "
            f"streams={len(p.get('streams', []))}",
            report_fh=report)
        if has_video and len(langs) >= 2:
            multi_lang_programs.append((pn, prog_audios))

    log("", report_fh=report)
    if args.program:
        target_program = args.program
        target_audios = [a for a in audios if a.program == target_program]
        log(f"  (user-pinned to program {target_program})", report_fh=report)
    elif multi_lang_programs:
        target_program, target_audios = multi_lang_programs[0]
        log(f"  picked program {target_program} for multi-language test",
            report_fh=report)
    else:
        log("  WARNING: no multi-language program found. Tests will run on "
            "the first video program but Spanish-test won't be meaningful.",
            report_fh=report)
        first = next((p.get("program_num") for p in programs
                      if any(s.get("codec_type") == "video"
                             for s in p.get("streams", []))), None)
        if first is None:
            log("  ERROR: no video program found at all", report_fh=report)
            return 1
        target_program = first
        target_audios = [a for a in audios if a.program == target_program]
    log("", report_fh=report)

    target_langs = sorted({a.lang for a in target_audios})

    # ---- Step 2: extract WAV samples ----
    log(f"[2/4] extracting {args.sample_seconds}s WAV samples of each "
        f"language from program {target_program} ...", report_fh=report)
    samples = {}
    for lang in target_langs:
        wav = SAMPLES_DIR / f"prog{target_program}_{lang}.wav"
        ok = extract_wav(target_program, lang, args.sample_seconds, wav)
        if ok:
            level = audio_level(wav)
            samples[lang] = {"path": wav, "level": level,
                             "size": wav.stat().st_size}
            log(f"  [OK] {lang}: wav={wav.name} "
                f"size={wav.stat().st_size//1024} KB  "
                f"mean={level['mean_db']} dB  max={level['max_db']} dB",
                report_fh=report)
        else:
            log(f"  [XX] {lang}: extract failed", report_fh=report)
    log("", report_fh=report)

    # ---- Step 3: compare waveforms ----
    log("[3/4] comparing waveforms between language tracks ...",
        report_fh=report)
    log("  (similarity near 1.0 = the two tracks are the SAME audio,",
        report_fh=report)
    log("   which would mean the broadcaster is sending identical content",
        report_fh=report)
    log("   on both 'eng' and 'spa' — and your 'always hear English' "
        "complaint", report_fh=report)
    log("   would be physical reality, not a VLC bug.)", report_fh=report)
    log("", report_fh=report)
    lang_list = list(samples.keys())
    sim_results = []
    for i in range(len(lang_list)):
        for j in range(i + 1, len(lang_list)):
            a_lang, b_lang = lang_list[i], lang_list[j]
            sim = waveform_similarity(samples[a_lang]["path"],
                                      samples[b_lang]["path"])
            verdict = ("IDENTICAL (broadcaster duplicated)"
                       if sim > 0.95 else
                       "VERY SIMILAR (probably same audio)"
                       if sim > 0.75 else
                       "DIFFERENT (real distinct languages)"
                       if abs(sim) < 0.4 else
                       "PARTIALLY CORRELATED")
            sim_results.append((a_lang, b_lang, sim, verdict))
            log(f"  {a_lang} vs {b_lang}: correlation={sim:.3f} -> {verdict}",
                report_fh=report)
    log("", report_fh=report)

    # ---- Step 4: picker pipeline test for each lang ----
    log("[4/4] running the picker's ffmpeg pipeline for each language ...",
        report_fh=report)
    for lang in target_langs + ["all"]:
        out_ts = SAMPLES_DIR / f"pipeline_{lang}.ts"
        res = test_picker_pipeline(target_program, lang, out_ts)
        if res.get("ok"):
            ac = res.get("audio_count", 0)
            langs = res.get("audio_langs", [])
            chans = res.get("audio_channels", [])
            log(f"  STVT_AUDIO_LANGUAGE={lang}: "
                f"audio_streams={ac}  langs={langs}  channels={chans}  "
                f"size={res['size']//1024} KB",
                report_fh=report)
        else:
            log(f"  STVT_AUDIO_LANGUAGE={lang}: pipeline FAILED  "
                f"({res.get('size', 0)//1024} KB)",
                report_fh=report)
    log("", report_fh=report)

    # ---- Conclusion ----
    log("=" * 72, report_fh=report)
    log("CONCLUSIONS", report_fh=report)
    log("=" * 72, report_fh=report)

    # The big diagnostic question: are the tracks actually different?
    any_identical = any(s > 0.95 for _, _, s, _ in sim_results)
    any_distinct = any(abs(s) < 0.4 for _, _, s, _ in sim_results)

    if any_identical:
        log("!! Two audio tracks have identical waveforms. The broadcaster",
            report_fh=report)
        log("  is sending the same audio on multiple 'language' tracks.",
            report_fh=report)
        log("  No amount of pipeline fix will give you actual Spanish.",
            report_fh=report)
        log("  Try a confirmed dual-language broadcast (RF 15 WFDC =",
            report_fh=report)
        log("  Univision; primary audio there is Spanish-language).",
            report_fh=report)
    elif any_distinct:
        log("OK The 'eng' and 'spa' tracks contain DIFFERENT audio. The",
            report_fh=report)
        log("  broadcaster is genuinely transmitting two languages.",
            report_fh=report)
        log("  If you keep hearing English in VLC even after the pipeline",
            report_fh=report)
        log("  is delivering only Spanish, the most likely cause is one of:",
            report_fh=report)
        log("    - You're listening to the wrong window (a stale VLC, or",
            report_fh=report)
        log("      browser audio, etc.). Check Get-Process vlc.",
            report_fh=report)
        log("    - You haven't relaunched tv_tuner.py since the latest",
            report_fh=report)
        log("      code edit landed. Kill and respawn.",
            report_fh=report)
        log("    - Your audio output device has a stale buffer from before.",
            report_fh=report)
        log("      Toggle the volume mixer for VLC.",
            report_fh=report)
        log("", report_fh=report)
        log("  Listen to the saved WAVs to verify each track for yourself:",
            report_fh=report)
        for lang, info in samples.items():
            log(f"    {lang}: {info['path']}", report_fh=report)
    else:
        log("?? Tracks are partially correlated. Inconclusive.",
            report_fh=report)
    log("", report_fh=report)
    log(f"Full report saved to: {REPORT_FILE}", report_fh=report)
    if args.keep_samples:
        log(f"Audio samples kept in: {SAMPLES_DIR}", report_fh=report)
    else:
        log("(Add --keep-samples to leave the WAVs for manual playback)",
            report_fh=report)
    report.close()

    if not args.keep_samples:
        for f in SAMPLES_DIR.glob("pipeline_*.ts"):
            try:
                f.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
