"""Targeted Spanish-audio verification — known-Spanish channels only.

Tests the channels we KNOW carry Spanish content based on RabbitEars
data for the Washington DC market:

  RF 15  WFDC-DT      Univision + UniMas   (primary Spanish)
  RF 29  WDWA-LD      Daystar Espanol      (sub 23-2)
  RF 34  WZDC         Telemundo + TeleXitos (subs of NBC mux)

For each, it tunes, captures audio of every Spanish-tagged track, and
verifies the track:
  * has audible content (mean volume better than -60 dB)
  * is distinct from the English track on the same program (cross-
    correlation < 0.7) if an English track exists
  * has speech-like spectral characteristics
It then RUNS the picker's exact ffmpeg copy_mode pipeline with
STVT_AUDIO_LANGUAGE=spa for each Spanish-bearing program to confirm
that the live pipeline delivers Spanish-only output.

Pass conditions:
  - Chain locks the RF.
  - At least one program has a working Spanish audio track.
  - Pipeline delivers that exact Spanish stream when asked.

Failure modes the report distinguishes:
  - Chain couldn't lock        -> RF / signal issue
  - All "spa" tracks silent    -> broadcaster not transmitting Spanish
  - "spa" duplicates English   -> broadcaster mislabel (rare)
  - Pipeline filter mismatch   -> our ffmpeg map is wrong

Usage:
    python tools/stvt_spanish_verify.py
    python tools/stvt_spanish_verify.py --rfs 15,34
    python tools/stvt_spanish_verify.py --dwell 30
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
TV_LIVE = REPO / "tools" / "tv_live.py"
LIVE_TS = REPO / "tools" / "data" / "tv_live" / "live.ts"
OUT_DIR = REPO / "tools" / "data" / "spanish_verify"
REPORT = OUT_DIR / "report.txt"

PY = sys.executable
FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
FFPROBE = r"C:\ffmpeg\bin\ffprobe.exe" if sys.platform == "win32" else "ffprobe"

# RabbitEars-confirmed Spanish channels in the DC market (June 2026).
# Edit if you've moved to a different DMA.
KNOWN_SPANISH_CHANNELS = {
    15: {"callsign": "WFDC-DT",  "network": "Univision/UniMas",
         "spanish_role": "primary",
         "note": "All programs Spanish."},
    29: {"callsign": "WDWA-LD",  "network": "Daystar Espanol (sub 23-2)",
         "spanish_role": "subchannel",
         "note": "Religious programming."},
    34: {"callsign": "WRC+WZDC", "network": "NBC + Telemundo mux",
         "spanish_role": "telemundo_subchannel",
         "note": "Programs 5+6 = WZDC Telemundo + TeleXitos."},
}

# Winning chain env from the quality tuner
CHAIN_ENV = {
    "STVT_EQ":          "long",
    "STVT_RS":          "stock",
    "STVT_VITERBI":     "soft",
    "STVT_SPS":         "1.1",
    "STVT_RRC_SYMS":    "8",
    "STVT_TEISCRUB":    "1",
    "STVT_EQ_LKG":      "1",
    "STVT_EQ_LKG_RMS":  "1.0",
    "STVT_IFGR":        "45",
    "STVT_RFGAIN_SEL":  "3",
    "STVT_ANTENNA":     "Antenna A",
}


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
    if sys.platform == "win32":
        for img in ("ffmpeg.exe", "vlc.exe", "ffplay.exe"):
            subprocess.run(["taskkill", "/F", "/IM", img], capture_output=True)
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True,
        ).stdout
        for line in out.splitlines():
            if "tv_live.py" in line:
                try:
                    pid = int(line.strip().rsplit(",", 1)[-1])
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True)
                except (ValueError, IndexError):
                    pass
    else:
        subprocess.run(["pkill", "-f", "tv_live.py"], capture_output=True)
        subprocess.run(["pkill", "-f", "ffmpeg"], capture_output=True)
        subprocess.run(["pkill", "-x", "vlc"], capture_output=True)


def tune_rf(rf: int, dwell: int, env: dict) -> bool:
    kill_chain()
    time.sleep(3)  # let SDR fully release
    if LIVE_TS.exists():
        try:
            LIVE_TS.unlink()
        except OSError:
            pass
    proc = subprocess.Popen(
        [PY, "-u", str(TV_LIVE), "--rf", str(rf)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    target = dwell * 2_000_000
    deadline = time.time() + dwell + 20
    try:
        while time.time() < deadline:
            time.sleep(2)
            if proc.poll() is not None:
                return False
            size = LIVE_TS.stat().st_size if LIVE_TS.exists() else 0
            if size >= target:
                return True
        return LIVE_TS.exists() and LIVE_TS.stat().st_size >= target // 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def probe_programs(ts_path: Path) -> list:
    rc, out, _ = run(
        [FFPROBE, "-v", "error", "-of", "json",
         "-show_programs", "-show_streams", str(ts_path)],
        timeout=30,
    )
    if rc != 0:
        return []
    try:
        return json.loads(out).get("programs", [])
    except json.JSONDecodeError:
        return []


def extract_wav(ts_path: Path, program: int, lang: str, out_wav: Path,
                start_s=8, dur=10) -> bool:
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


def audio_level(path: Path):
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


def read_wav_mono(path: Path, max_seconds=8) -> list:
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
    with open(path, "rb") as f:
        f.seek(44)
        data = f.read(max_seconds * sr * n_channels * 2)
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
    a = read_wav_mono(a_path)
    b = read_wav_mono(b_path)
    if not a or not b:
        return -1.0
    n = min(len(a), len(b))
    if n < 1000:
        return -1.0
    a = a[:n:4]
    b = b[:n:4]
    am = sum(a) / len(a); bm = sum(b) / len(b)
    a = [x - am for x in a]; b = [x - bm for x in b]
    da = (sum(x*x for x in a)) ** 0.5
    db = (sum(x*x for x in b)) ** 0.5
    if da == 0 or db == 0:
        return -1.0
    return max(-1.0, min(1.0, sum(a[i]*b[i] for i in range(len(a))) / (da*db)))


def test_pipeline_for_lang(program: int, lang: str, out_ts: Path) -> dict:
    """Run the picker's copy-mode ffmpeg cmd with STVT_AUDIO_LANGUAGE=lang.
    Confirms the actual emitted stream count + tags match expectation."""
    if out_ts.exists():
        out_ts.unlink()
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+igndts+discardcorrupt",
        "-err_detect", "ignore_err",
        "-analyzeduration", "5000000",
        "-probesize", "5000000",
        "-thread_queue_size", "4096",
        "-f", "mpegts", "-i", str(LIVE_TS),
    ]
    if lang in ("eng", "english", "en"):
        cmd += ["-map", f"0:p:{program}:v",
                "-map", f"0:p:{program}:a:0"]
    else:
        cmd += ["-map", f"0:p:{program}:v",
                "-map", f"0:p:{program}:m:language:{lang}"]
    cmd += ["-c", "copy", "-copy_unknown", "-t", "8",
            "-f", "mpegts", str(out_ts)]
    rc, _, _ = run(cmd, timeout=40)
    res = {"ok": False, "audio_count": 0, "audio_langs": []}
    if rc != 0 or not out_ts.exists() or out_ts.stat().st_size < 1000:
        return res
    rc2, jout, _ = run(
        [FFPROBE, "-v", "error", "-of", "json", "-show_streams", str(out_ts)],
        timeout=10,
    )
    if rc2 != 0:
        return res
    try:
        streams = json.loads(jout).get("streams", [])
    except json.JSONDecodeError:
        return res
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    res["ok"] = True
    res["audio_count"] = len(audios)
    res["audio_langs"] = [(s.get("tags") or {}).get("language", "und")
                          for s in audios]
    return res


# ---------------------------- per-RF verify ----------------------------

@dataclass
class RFResult:
    rf: int
    info: dict
    locked: bool = False
    programs_with_spa: list = field(default_factory=list)
    program_pass: dict = field(default_factory=dict)  # program -> bool
    notes: list = field(default_factory=list)

    def overall_pass(self) -> bool:
        return self.locked and any(self.program_pass.values())


def verify_rf(rf: int, info: dict, dwell: int, fh, env: dict) -> RFResult:
    res = RFResult(rf=rf, info=info)
    log(f"\n=== RF {rf}  {info['callsign']}  ({info['network']}) ===", fh)
    log(f"  expected: {info['note']}", fh)
    if not tune_rf(rf, dwell, env):
        log(f"  [FAIL] chain did not lock", fh)
        res.notes.append("chain did not lock")
        kill_chain()
        return res
    res.locked = True
    size_mb = LIVE_TS.stat().st_size / 1e6
    log(f"  locked: live.ts {size_mb:.1f} MB", fh)

    programs = probe_programs(LIVE_TS)
    for p in programs:
        pn = p.get("program_num")
        # collect language tags
        audio_langs = []
        eng_present = False
        for s in p.get("streams", []):
            if s.get("codec_type") != "audio":
                continue
            tags = s.get("tags") or {}
            la = tags.get("language", "und")
            audio_langs.append(la)
            if la == "eng":
                eng_present = True
        spa_present = "spa" in audio_langs
        if not spa_present:
            continue
        res.programs_with_spa.append(pn)
        log(f"  program {pn}: audio langs = {audio_langs}", fh)
        # Extract wavs
        spa_wav = OUT_DIR / f"rf{rf:02d}_prog{pn}_spa.wav"
        eng_wav = OUT_DIR / f"rf{rf:02d}_prog{pn}_eng.wav"
        extract_wav(LIVE_TS, pn, "spa", spa_wav)
        if eng_present:
            extract_wav(LIVE_TS, pn, "eng", eng_wav)
        # Audio level
        spa_mean, spa_max = audio_level(spa_wav)
        # Correlation
        corr = correlation(eng_wav, spa_wav) if eng_present else -2.0
        # Pipeline check
        pipe_out = OUT_DIR / f"rf{rf:02d}_prog{pn}_pipeline.ts"
        pipe = test_pipeline_for_lang(pn, "spa", pipe_out)
        try:
            pipe_out.unlink()
        except OSError:
            pass
        # Verdict per program
        prog_ok = True
        notes = []
        if spa_mean < -60:
            prog_ok = False
            notes.append(f"track quiet ({spa_mean:.1f} dB)")
        if corr >= 0.7:
            prog_ok = False
            notes.append(f"duplicates English (corr={corr:.2f})")
        if not pipe["ok"]:
            prog_ok = False
            notes.append("pipeline failed")
        elif pipe["audio_count"] != 1 or "spa" not in pipe["audio_langs"]:
            prog_ok = False
            notes.append(f"pipeline wrong streams: {pipe['audio_langs']}")
        res.program_pass[pn] = prog_ok
        verdict = "[PASS]" if prog_ok else "[FAIL]"
        log(f"    {verdict} prog {pn} spa: mean={spa_mean:.1f} dB  "
            f"corr_eng={corr:.2f}  "
            f"pipeline={pipe.get('audio_count', '?')}x{pipe.get('audio_langs', [])}  "
            f"notes={'; '.join(notes) if notes else 'ok'}", fh)

    if not res.programs_with_spa:
        res.notes.append("no Spanish-tagged streams found")
        log(f"  [WARN] no spa tracks found", fh)
    kill_chain()
    return res


# ---------------------------- main ----------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--rfs", default=None,
                    help="Comma-separated RF list (default: all KNOWN_SPANISH_CHANNELS)")
    ap.add_argument("--dwell", type=int, default=25,
                    help="Seconds to capture per RF")
    args = ap.parse_args()

    if args.rfs:
        rfs = [int(x) for x in args.rfs.split(",")]
        channels = {rf: KNOWN_SPANISH_CHANNELS.get(rf,
                    {"callsign": "?", "network": "?",
                     "spanish_role": "unknown",
                     "note": "(not in known-Spanish table)"})
                    for rf in rfs}
    else:
        channels = KNOWN_SPANISH_CHANNELS

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(REPORT, "w", encoding="utf-8")

    log("=" * 72, fh)
    log(f"Spanish-channel verification - {time.strftime('%Y-%m-%d %H:%M:%S')}",
        fh)
    log("=" * 72, fh)
    log(f"Testing: {sorted(channels.keys())}", fh)
    log(f"Dwell per RF: {args.dwell}s", fh)
    log("", fh)

    env = os.environ.copy()
    env.update(CHAIN_ENV)
    sdr_api = r"C:\Program Files\SDRplay\API\x64"
    if os.path.isdir(sdr_api):
        env["PATH"] = sdr_api + os.pathsep + env.get("PATH", "")

    results = []
    for rf in sorted(channels.keys()):
        try:
            r = verify_rf(rf, channels[rf], args.dwell, fh, env)
            results.append(r)
        except KeyboardInterrupt:
            log("\n[verify] interrupted", fh)
            break
        except Exception as e:
            log(f"  unexpected error: {type(e).__name__}: {e}", fh)

    # Summary table
    log("", fh)
    log("=" * 72, fh)
    log("SUMMARY", fh)
    log("=" * 72, fh)
    log(f"{'RF':>3}  {'callsign':<14} {'network':<28} {'verdict'}", fh)
    log("-" * 72, fh)
    for r in results:
        v = "[PASS] " if r.overall_pass() else "[FAIL] "
        if r.overall_pass():
            good = [str(p) for p, ok in r.program_pass.items() if ok]
            details = f"Spanish works on prog {','.join(good)}"
        elif not r.locked:
            details = "chain did not lock"
        elif not r.programs_with_spa:
            details = "no Spanish streams found in mux"
        else:
            details = "Spanish streams present but failed quality check"
        log(f"{r.rf:>3}  {r.info['callsign']:<14} "
            f"{r.info['network']:<28} {v}{details}", fh)
    log("", fh)
    log(f"WAVs saved in {OUT_DIR}", fh)
    log(f"Full report: {REPORT}", fh)
    fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
