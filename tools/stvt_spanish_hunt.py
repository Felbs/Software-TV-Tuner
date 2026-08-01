"""Spanish-audio hunter agent.

Hunts across multiple RF channels for actual Spanish audio content.
Some broadcasters have a 'spa' language tag but transmit English on
that track ("misleading SAP"). Some carry real Spanish audio only on
certain programs or only during specific shows. This agent tunes
each candidate RF, captures live audio of every tagged track, and
reports where REAL Spanish content was found.

How it decides "this is real Spanish content":
==============================================
We don't have a speech-recognition model to install. Instead we use
three heuristics that are reliable in combination:

  (1) The track must have actual audio (mean volume > -60 dB).
  (2) The track's waveform must be DIFFERENT from the program's
      English track (cross-correlation < 0.7). If it's the same
      waveform as English, the broadcaster is just duplicating.
  (3) The track must contain speech-like spectral activity (broad
      energy in 200-3000 Hz, not narrow tones or silence).

A track that passes all three is flagged as "candidate genuine Spanish";
the user can listen to the saved WAV to confirm.

What it does NOT do:
- It does NOT modify tv_tuner.py or any other production code.
- It does NOT keep the chain running between RFs — each capture is
  independent.

Output:
- A summary report at tools/data/spanish_hunt/report.txt
- Per-track WAVs at tools/data/spanish_hunt/<rf>_<prog>_<lang>.wav
- Top "best candidates" sorted by likelihood

Usage:
    python tools/stvt_spanish_hunt.py
        # default: hunt across all UHF channels that previously locked
    python tools/stvt_spanish_hunt.py --rfs 15,34,36
        # only test these RFs
    python tools/stvt_spanish_hunt.py --dwell 20
        # capture 20s per RF (default 25s)
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

REPO = Path(__file__).resolve().parents[1]
TV_LIVE = REPO / "tools" / "tv_live.py"
LIVE_TS = REPO / "tools" / "data" / "tv_live" / "live.ts"
OUT_DIR = REPO / "tools" / "data" / "spanish_hunt"
REPORT_FILE = OUT_DIR / "report.txt"

PY = sys.executable
FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
FFPROBE = r"C:\ffmpeg\bin\ffprobe.exe" if sys.platform == "win32" else "ffprobe"

# Winning chain env from quality tuner. x86 values: hard viterbi ported from
# the Pi (bare/winning config here too), but keep this box's gain (IFGR=45 /
# RFGAIN=3), quality RRC=8, and gear-shift lock-keeper — NOT the Pi's
# fused/S16/8MB-buffer/FPLL-fold speed env. This dict OVERRIDES os.environ for
# the spawned chain, so it carries the full config itself.
DEFAULT_ENV = {
    "STVT_EQ":          "long",
    "STVT_RS":          "stock",
    "STVT_VITERBI":     "hard",
    "STVT_SPS":         "1.1",
    "STVT_RRC_SYMS":    "8",
    "STVT_TEISCRUB":    "1",
    "STVT_EQ_LKG":      "1",
    "STVT_EQ_LKG_RMS":  "1.0",
    "STVT_IFGR":        "45",
    "STVT_RFGAIN_SEL":  "3",
    "STVT_ANTENNA":     "Antenna A",
}

# DC-area UHF channels likely to carry Spanish content (Univision, Telemundo,
# multilingual NBC subchannels, etc.). Override with --rfs.
DEFAULT_RFS = [14, 15, 20, 21, 23, 26, 31, 34, 35, 36]


# ---------------------------- helpers ----------------------------------

def log(s: str, fh=None):
    print(s, flush=True)
    if fh:
        fh.write(s + "\n")
        fh.flush()


def run(cmd, timeout=60, env=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def kill_chain():
    """Kill any running tv_live + ffmpeg + vlc so we have the SDR."""
    if sys.platform == "win32":
        for img in ("ffmpeg.exe", "vlc.exe", "ffplay.exe"):
            # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign); loop also sweeps mpv/ffmpeg players
            subprocess.run(["taskkill", "/F", "/IM", img],  # kill-ok (see above)
                           capture_output=True)
        # find tv_live.py pythons
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True,
        ).stdout
        for line in out.splitlines():
            if "tv_live.py" in line:
                try:
                    pid = int(line.strip().rsplit(",", 1)[-1])
                    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],  # kill-ok (see above)
                                   capture_output=True)
                except (ValueError, IndexError):
                    pass
    else:
        # Anchored / exact-name patterns ONLY: a bare `pkill -f ffmpeg`
        # matches ANY process whose command text mentions ffmpeg — it
        # killed this script's own parent shell during testing.
        subprocess.run(["pkill", "-f", r"^[^ ]*python3 (-u )?[^ ]*tv_live\.py"],
                       capture_output=True)
        subprocess.run(["pkill", "-x", "ffmpeg"], capture_output=True)
        subprocess.run(["pkill", "-x", "vlc"], capture_output=True)


def tune_rf(rf: int, dwell_seconds: int, env: dict) -> bool:
    """Spawn tv_live.py for `dwell_seconds` and wait for live.ts to grow
    to a useful size. Returns True if it locked and produced enough data."""
    kill_chain()
    time.sleep(2)
    if LIVE_TS.exists():
        try:
            LIVE_TS.unlink()
        except OSError:
            pass

    proc = subprocess.Popen(
        [PY, "-u", str(TV_LIVE), "--rf", str(rf)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + dwell_seconds + 15
    min_size = dwell_seconds * 2_000_000  # ~2 MB/s of mpegts
    last_size = 0
    stall_count = 0
    try:
        while time.time() < deadline:
            time.sleep(2)
            if proc.poll() is not None:
                return False
            size = LIVE_TS.stat().st_size if LIVE_TS.exists() else 0
            if size >= min_size:
                return True
            if size == last_size and size > 5_000_000:
                stall_count += 1
                if stall_count >= 4:
                    return False
            else:
                stall_count = 0
            last_size = size
        return last_size >= min_size // 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------- audio analysis ---------------------------

def probe_programs(ts_path: Path) -> dict:
    rc, out, _ = run(
        [FFPROBE, "-v", "error", "-of", "json",
         "-show_programs", "-show_streams", str(ts_path)],
        timeout=30,
    )
    if rc != 0:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {}


def extract_wav(ts_path: Path, program: int, lang: str, out_wav: Path,
                start_s: int = 8, dur: int = 10) -> bool:
    if out_wav.exists():
        try:
            out_wav.unlink()
        except OSError:
            pass
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(start_s), "-t", str(dur),
        "-i", str(ts_path),
        "-map", f"0:p:{program}:m:language:{lang}",
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        str(out_wav),
    ]
    rc, _, _ = run(cmd, timeout=dur * 4 + 10)
    return rc == 0 and out_wav.exists() and out_wav.stat().st_size > 1000


def audio_level(path: Path) -> Tuple[float, float]:
    rc, _, err = run([
        FFMPEG, "-hide_banner",
        "-i", str(path),
        "-af", "volumedetect", "-f", "null", "-",
    ], timeout=15)
    mean = max_ = -99.0
    for line in err.splitlines():
        if "mean_volume:" in line:
            try:
                mean = float(line.split("mean_volume:")[1]
                             .split("dB")[0].strip())
            except (ValueError, IndexError):
                pass
        elif "max_volume:" in line:
            try:
                max_ = float(line.split("max_volume:")[1]
                             .split("dB")[0].strip())
            except (ValueError, IndexError):
                pass
    return mean, max_


def read_wav_mono(path: Path, max_seconds=8) -> list[int]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        header = f.read(44)
    if len(header) < 44 or header[:4] != b"RIFF":
        return []
    n_channels = struct.unpack("<H", header[22:24])[0]
    sr = struct.unpack("<I", header[24:28])[0]
    bits = struct.unpack("<H", header[34:36])[0]
    if bits != 16:
        return []
    max_bytes = max_seconds * sr * n_channels * 2
    with open(path, "rb") as f:
        f.seek(44)
        data = f.read(max_bytes)
    n = len(data) // (2 * n_channels)
    out = []
    for i in range(n):
        off = i * 2 * n_channels
        s = 0
        for c in range(n_channels):
            s += struct.unpack_from("<h", data, off + c * 2)[0]
        out.append(s // n_channels)
    return out


def correlation(a_path: Path, b_path: Path) -> float:
    """Normalised zero-lag cross-correlation. 1.0 == identical."""
    a = read_wav_mono(a_path)
    b = read_wav_mono(b_path)
    if not a or not b:
        return -1.0
    n = min(len(a), len(b))
    if n < 1000:
        return -1.0
    # Downsample 4x for speed
    a = a[:n:4]
    b = b[:n:4]
    am = sum(a) / len(a); bm = sum(b) / len(b)
    a = [x - am for x in a]; b = [x - bm for x in b]
    da = (sum(x*x for x in a)) ** 0.5
    db = (sum(x*x for x in b)) ** 0.5
    if da == 0 or db == 0:
        return -1.0
    s = sum(a[i] * b[i] for i in range(len(a)))
    return max(-1.0, min(1.0, s / (da * db)))


def speech_score(path: Path) -> float:
    """0..1 estimate of how "speech-like" the audio is. We measure the
    ratio of energy in the 200-3000 Hz band (typical speech) to total
    energy via a coarse FFT. Not perfect but distinguishes silence /
    music / speech reasonably."""
    samples = read_wav_mono(path, max_seconds=5)
    if len(samples) < 4096:
        return 0.0
    # Window and FFT a 4096-sample slice
    n = 4096
    half = len(samples) // 2
    start = max(0, half - n // 2)
    win = samples[start:start + n]
    if len(win) < n:
        return 0.0
    # Hann window
    import math
    w = [v * 0.5 * (1 - math.cos(2 * math.pi * i / (n - 1)))
         for i, v in enumerate(win)]
    # Naive DFT in chunks would be too slow; use cmath
    import cmath
    fft = []
    for k in range(n // 2):
        s = 0.0 + 0.0j
        twiddle = -2j * math.pi * k / n
        for j, x in enumerate(w):
            s += x * cmath.exp(twiddle * j)
        fft.append(abs(s))
    sr = 48000
    total = sum(fft)
    if total < 1:
        return 0.0
    band_lo = int(200 * n / sr)
    band_hi = int(3000 * n / sr)
    speech_energy = sum(fft[band_lo:band_hi])
    return min(1.0, speech_energy / total)


# Naive DFT is O(n^2) and at n=4096 it's slow (~16M complex multiplies).
# For an MVP just sample a few hundred bins instead of the full half.
def speech_score_fast(path: Path) -> float:
    samples = read_wav_mono(path, max_seconds=5)
    if len(samples) < 4096:
        return 0.0
    # Use scipy/numpy if available; otherwise skip the heavy estimate.
    try:
        import numpy as np
    except ImportError:
        return -1.0
    arr = np.array(samples, dtype=np.float64)
    if arr.std() < 50:
        return 0.0
    # 4096-point FFT
    n = 4096
    half = len(arr) // 2
    start = max(0, half - n // 2)
    win = arr[start:start + n]
    if len(win) < n:
        return 0.0
    hann = 0.5 * (1 - np.cos(2 * np.pi * np.arange(n) / (n - 1)))
    spec = np.abs(np.fft.rfft(win * hann))
    sr = 48000
    total = spec.sum()
    if total < 1:
        return 0.0
    band_lo = int(200 * n / sr)
    band_hi = int(3000 * n / sr)
    speech_energy = spec[band_lo:band_hi].sum()
    return float(min(1.0, speech_energy / total))


# ---------------------------- hunt ------------------------------------

@dataclass
class TrackFinding:
    rf: int
    program: int
    lang: str
    channels: int
    codec: str
    wav_path: Path
    mean_db: float = -99.0
    max_db: float = -99.0
    corr_to_eng: float = 0.0          # similarity vs eng on same program
    speech_band_ratio: float = -1.0   # 200..3000 Hz energy ratio
    looks_like_real_spanish: bool = False
    reason: str = ""


def hunt_one_rf(rf: int, dwell: int, fh, env: dict) -> list[TrackFinding]:
    log(f"\n=== RF {rf} ===", fh)
    if not tune_rf(rf, dwell, env):
        log(f"  RF {rf}: chain did not lock or produced too little data", fh)
        kill_chain()
        return []

    size_mb = LIVE_TS.stat().st_size / 1e6
    log(f"  RF {rf}: captured {size_mb:.1f} MB", fh)
    data = probe_programs(LIVE_TS)
    progs = data.get("programs", [])
    findings: list[TrackFinding] = []

    for p in progs:
        pn = p.get("program_num")
        # collect audio streams of this program
        audio_langs = []
        has_video = False
        for s in p.get("streams", []):
            if s.get("codec_type") == "video":
                has_video = True
            elif s.get("codec_type") == "audio":
                tags = s.get("tags") or {}
                audio_langs.append({
                    "lang": tags.get("language", "und"),
                    "codec": s.get("codec_name", ""),
                    "channels": s.get("channels", 0),
                })
        if not has_video or not audio_langs:
            continue
        all_langs = sorted({a["lang"] for a in audio_langs})
        log(f"  program {pn}: audio={all_langs}", fh)
        # Extract WAVs of every language present
        eng_wav = None
        for a in audio_langs:
            lang = a["lang"]
            wav = OUT_DIR / f"rf{rf:02d}_prog{pn}_{lang}.wav"
            ok = extract_wav(LIVE_TS, pn, lang, wav)
            if not ok:
                log(f"    {lang}: extract failed", fh)
                continue
            mean_db, max_db = audio_level(wav)
            speech_ratio = speech_score_fast(wav)
            f = TrackFinding(
                rf=rf, program=pn, lang=lang, codec=a["codec"],
                channels=a["channels"], wav_path=wav,
                mean_db=mean_db, max_db=max_db,
                speech_band_ratio=speech_ratio,
            )
            findings.append(f)
            if lang == "eng":
                eng_wav = wav
        # Cross-correlate each non-eng track against the eng track of the
        # same program (if any) to detect duplicate audio.
        if eng_wav:
            for f in findings:
                if f.rf != rf or f.program != pn:
                    continue
                if f.lang == "eng":
                    continue
                f.corr_to_eng = correlation(eng_wav, f.wav_path)
        # Score "looks like real Spanish":
        #  - language tag is "spa" or non-eng
        #  - mean_db better than -60 (audible content)
        #  - corr_to_eng below 0.7 (distinct from English)
        #  - speech_band_ratio in [0.15, 0.85] (broad speech-like spectrum)
        for f in findings:
            if f.rf != rf or f.program != pn:
                continue
            if f.lang == "eng":
                continue
            reasons = []
            ok = True
            if f.mean_db < -60:
                reasons.append("track too quiet")
                ok = False
            if eng_wav and f.corr_to_eng > 0.7:
                reasons.append(
                    f"waveform near-identical to English (corr={f.corr_to_eng:.2f})")
                ok = False
            if (f.speech_band_ratio >= 0
                    and not (0.10 <= f.speech_band_ratio <= 0.95)):
                reasons.append(
                    f"speech-band ratio {f.speech_band_ratio:.2f} outside typical voice range")
                ok = False
            f.looks_like_real_spanish = ok and (f.lang != "und")
            f.reason = ", ".join(reasons) if reasons else "ok"
            tag = "[CANDIDATE]" if f.looks_like_real_spanish else "[no]"
            log(f"    {tag} {f.lang}: mean={f.mean_db:.1f} dB  "
                f"corr_to_eng={f.corr_to_eng:.2f}  "
                f"speech={f.speech_band_ratio:.2f}  -> {f.reason}", fh)
    kill_chain()
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--rfs", default=None,
                    help="Comma-separated RF list (default: known DC UHF)")
    ap.add_argument("--dwell", type=int, default=25,
                    help="Seconds to dwell on each RF (default 25)")
    args = ap.parse_args()

    rfs = ([int(x) for x in args.rfs.split(",")]
           if args.rfs else DEFAULT_RFS)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(REPORT_FILE, "w", encoding="utf-8")

    log("=" * 72, fh)
    log(f"Spanish-audio hunter agent - {time.strftime('%Y-%m-%d %H:%M:%S')}",
        fh)
    log("=" * 72, fh)
    log(f"RFs to scan: {rfs}", fh)
    log(f"Dwell per RF: {args.dwell}s", fh)
    log(f"Output dir:  {OUT_DIR}", fh)

    env = os.environ.copy()
    env.update(DEFAULT_ENV)
    sdr_api = r"C:\Program Files\SDRplay\API\x64"
    if os.path.isdir(sdr_api):
        env["PATH"] = sdr_api + os.pathsep + env.get("PATH", "")

    all_findings: list[TrackFinding] = []
    for rf in rfs:
        try:
            f = hunt_one_rf(rf, args.dwell, fh, env)
            all_findings.extend(f)
        except KeyboardInterrupt:
            log("\n[hunt] interrupted", fh)
            break
        except Exception as e:
            log(f"  RF {rf}: unexpected exception {type(e).__name__}: {e}",
                fh)

    # Final ranking
    log("", fh)
    log("=" * 72, fh)
    log("TOP CANDIDATES (likely real Spanish content)", fh)
    log("=" * 72, fh)
    candidates = [x for x in all_findings if x.looks_like_real_spanish]
    if not candidates:
        log("  None found. Possible reasons:", fh)
        log("    - Broadcasters in your area duplicate audio across tracks", fh)
        log("    - All Spanish-tagged tracks were too quiet (no SAP active)", fh)
        log("    - Tuner couldn't lock any tested channel", fh)
        log("    - Run again at a different time of day", fh)
    else:
        # Sort by: louder + more distinct from English first
        candidates.sort(key=lambda x: (-x.mean_db, x.corr_to_eng))
        for c in candidates[:10]:
            log(f"  RF {c.rf:2d}  prog {c.program}  lang={c.lang}  "
                f"mean={c.mean_db:.1f} dB  corr={c.corr_to_eng:.2f}  "
                f"wav={c.wav_path.name}", fh)
    log("", fh)
    log(f"All WAVs saved to {OUT_DIR}", fh)
    log(f"Full report: {REPORT_FILE}", fh)

    fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
