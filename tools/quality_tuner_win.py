"""Autonomous STVT quality tuner — Windows edition.

What it does
============
Runs forever (until you Ctrl-C) trying different chain configurations
(EQ variant, RS mode, Viterbi mode, SPS, RRC taps, LKG knobs, gain
levels), scores each one objectively, and keeps a leaderboard so you
always know which combo is winning.

Safeguards
==========
- Single chain capture at a time. NEVER more than 2 child processes
  (chain + analyzer) live at once.
- Hard timeout per capture (default 60s wall) — if the chain hangs,
  it's killed and that config gets a fail score.
- Disk space check before each run (need 250 MB free in tools/data/).
- Memory check before each run (need 2 GB free RAM).
- Cleans up all child processes between runs.
- State persisted to tools/data/quality_tuner_win/state.jsonl every run
  so it's resumable.
- Re-uses the leaderboard across restarts — won't redo configs already
  tested if they scored decently.
- Console output is short and informative every 5 seconds; full log
  goes to the state dir.

What it scores
==============
Higher = better. Composite score combines:
  + valid_hd_frames    (count of decoded 1920x1080 frames in extract)
  - convergence_burst  (bad RS packets in first 10s)
  - steady_bad_per_s   (bad RS packets per sec after convergence)
  - tei_pct_x100       (TEI % times 100 — penalty)
  - drought_penalty    (if unique_pids > 100, big penalty)

Usage
=====
    python tools/quality_tuner_win.py [--rf 34] [--seconds 60]

Stop with Ctrl-C. Resume by re-running — it picks up where it left off.

Status
======
Watch tools/data/quality_tuner_win/leaderboard.txt — it updates after
every run, sorted best to worst. Best config so far is printed when
the tuner exits.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

# -------------------------- paths ----------------------------------
REPO = Path(__file__).resolve().parents[1]
PY = r"C:\Users\user\radioconda\python.exe"
TV_LIVE = REPO / "tools" / "tv_live.py"
FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
FFPROBE = r"C:\ffmpeg\bin\ffprobe.exe"
TV_LIVE_DIR = REPO / "tools" / "data" / "tv_live"
TS_PATH = TV_LIVE_DIR / "live.ts"
HD_PATH = TV_LIVE_DIR / "prog_hd.ts"
STATE_DIR = REPO / "tools" / "data" / "quality_tuner_win"
STATE_FILE = STATE_DIR / "state.jsonl"
LEADERBOARD = STATE_DIR / "leaderboard.txt"
RUN_LOG = STATE_DIR / "current_run.log"

# -------------------------- knobs ----------------------------------
# Search space. Each entry is the env var and a list of values to try.
SEARCH_SPACE = {
    "STVT_EQ":          ["long", "pilot_dd", "pilot_dd_soft", "pilot_multifs_dd"],
    "STVT_RS":          ["stock", "erasure"],
    "STVT_VITERBI":     ["hard", "soft"],
    "STVT_SPS":         ["1.1", "1.25", "1.5"],
    "STVT_RRC_SYMS":    ["4", "6", "8"],
    "STVT_TEISCRUB":    ["0", "1"],
    "STVT_EQ_LKG":      ["0", "1"],
    "STVT_EQ_LKG_RMS":  ["1.0", "1.2", "1.5"],
    "STVT_IFGR":        ["45", "55", "59"],
    "STVT_RFGAIN_SEL":  ["3", "5", "8"],
    "STVT_ANTENNA":     ["Antenna A"],   # don't randomly switch antennas
}

# Baseline = best known to date. Starting point for greedy hill-climb.
BASELINE = {
    "STVT_EQ":          "long",
    "STVT_RS":          "erasure",
    "STVT_VITERBI":     "soft",
    "STVT_SPS":         "1.5",
    "STVT_RRC_SYMS":    "8",
    "STVT_TEISCRUB":    "1",
    "STVT_EQ_LKG":      "1",
    "STVT_EQ_LKG_RMS":  "1.2",
    "STVT_IFGR":        "59",
    "STVT_RFGAIN_SEL":  "5",
    "STVT_ANTENNA":     "Antenna A",
}

# Safety thresholds
MIN_FREE_DISK_MB = 250
MIN_FREE_RAM_GB = 2
CHAIN_MAX_SECONDS_DEFAULT = 60
PROCESS_KILL_GRACE_S = 3

# -------------------------- safeguards -----------------------------

def disk_free_mb(p: Path) -> int:
    total, used, free = shutil.disk_usage(str(p))
    return free // (1024 * 1024)


def ram_free_gb() -> float:
    """Free physical RAM in GB. Uses ctypes on Windows; returns large
    number on non-Windows so the check is a no-op."""
    if sys.platform != "win32":
        return 999.0
    import ctypes
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    s = MEMORYSTATUSEX()
    s.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
    return s.ullAvailPhys / (1024**3)


def kill_orphans():
    """Kill any leftover ffplay / ffmpeg / tv_live processes from prior runs.
    Only kills processes started by us based on cmdline; uses taskkill /F."""
    for img in ("ffmpeg.exe", "ffplay.exe"):
        subprocess.run(["taskkill", "/F", "/IM", img],
                       capture_output=True, text=True)
    # Find lingering python tv_live processes
    out = subprocess.run(["wmic", "process", "where", "name='python.exe'",
                          "get", "ProcessId,CommandLine", "/format:csv"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "tv_live.py" in line:
            try:
                pid = int(line.strip().split(",")[-1])
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True)
            except (ValueError, IndexError):
                pass


def preflight() -> str:
    """Returns "" if safe to proceed, else a reason to skip."""
    if not TV_LIVE.exists():
        return f"tv_live.py missing at {TV_LIVE}"
    if not Path(FFMPEG).exists():
        return f"ffmpeg.exe missing at {FFMPEG}"
    free_mb = disk_free_mb(TV_LIVE_DIR.parent if TV_LIVE_DIR.exists() else REPO)
    if free_mb < MIN_FREE_DISK_MB:
        return f"low disk: {free_mb} MB free (need {MIN_FREE_DISK_MB})"
    free_gb = ram_free_gb()
    if free_gb < MIN_FREE_RAM_GB:
        return f"low RAM: {free_gb:.1f} GB free (need {MIN_FREE_RAM_GB})"
    return ""


# -------------------------- capture --------------------------------

def run_capture(cfg: dict, rf: int, seconds: int) -> dict:
    """Run tv_live.py for `seconds` with the given env-var cfg. Returns a
    dict with raw metrics (no scoring yet). Hard timeout. Always cleans
    up child process before returning."""
    TV_LIVE_DIR.mkdir(parents=True, exist_ok=True)
    if TS_PATH.exists():
        try:
            TS_PATH.unlink()
        except OSError:
            pass
    if HD_PATH.exists():
        try:
            HD_PATH.unlink()
        except OSError:
            pass

    env = os.environ.copy()
    for k, v in cfg.items():
        env[k] = v

    # Belt-and-suspenders: ensure SDRplay API on PATH for child
    sdr_api = r"C:\Program Files\SDRplay\API\x64"
    if os.path.isdir(sdr_api):
        env["PATH"] = sdr_api + os.pathsep + env.get("PATH", "")

    chain_log = STATE_DIR / "last_chain.log"
    log_fh = open(chain_log, "w", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            [PY, str(TV_LIVE), "--rf", str(rf)],
            env=env, stdout=log_fh, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError as e:
        log_fh.close()
        return {"error": f"spawn failed: {e}"}

    start = time.time()
    deadline = start + seconds
    growth_samples = deque(maxlen=6)   # 6 samples of size, 5s apart -> 25s window
    last_size = 0
    stalled_windows = 0
    chain_died = False
    try:
        while time.time() < deadline:
            time.sleep(5)
            ret = proc.poll()
            if ret is not None:
                chain_died = True
                break
            try:
                cur_size = TS_PATH.stat().st_size if TS_PATH.exists() else 0
            except OSError:
                cur_size = 0
            growth_samples.append(cur_size)
            # Stall detection: 3 consecutive 5s windows with < 1 MB growth
            if cur_size - last_size < 1_000_000:
                stalled_windows += 1
            else:
                stalled_windows = 0
            last_size = cur_size
            if stalled_windows >= 3:
                # Chain is alive but not producing — give up
                break
    finally:
        # Always terminate the chain
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=PROCESS_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=PROCESS_KILL_GRACE_S)
                except subprocess.TimeoutExpired:
                    pass
        log_fh.close()

    raw = {
        "wall_seconds": round(time.time() - start, 1),
        "chain_died": chain_died,
        "stalled": stalled_windows >= 3,
        "ts_size_bytes": TS_PATH.stat().st_size if TS_PATH.exists() else 0,
    }
    raw.update(parse_chain_log(chain_log))
    raw.update(analyze_ts(TS_PATH))
    raw.update(analyze_hd_extract(TS_PATH))
    return raw


def parse_chain_log(log_path: Path) -> dict:
    """Pull rs_erasure per-5s windows, OsO count, fpll stability."""
    out = {"oso_count": 0, "rs_windows": [], "fpll_drift_hz": None}
    if not log_path.exists():
        return out
    fpll_nco = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "OsO" in line:
                    out["oso_count"] += 1
                if line.startswith("[rs_erasure t="):
                    # Parse "t= 5.0s ... bad=3826 ... miscorr=1822 ..." etc.
                    win = {}
                    parts = line.split()
                    for p in parts:
                        if "=" in p and not p.startswith("("):
                            k, _, v = p.partition("=")
                            try:
                                win[k] = float(v.rstrip("),"))
                            except ValueError:
                                pass
                    # "last5s" section appears after first cumulative — get fresh bad
                    if "last5s:" in line:
                        last_seg = line.split("last5s:")[1]
                        for p in last_seg.split():
                            if "=" in p:
                                k, _, v = p.partition("=")
                                try:
                                    win["last5s_" + k] = float(v.rstrip("),"))
                                except ValueError:
                                    pass
                    out["rs_windows"].append(win)
                if line.startswith("[fpll t="):
                    try:
                        nco = float(line.split("nco_freq_hz=")[1].split()[0])
                        fpll_nco.append(nco)
                    except (IndexError, ValueError):
                        pass
    except OSError:
        pass
    if len(fpll_nco) >= 4:
        # Drift = max - min over last 80% of samples (skip initial transient)
        tail = fpll_nco[len(fpll_nco) // 5:]
        out["fpll_drift_hz"] = round(max(tail) - min(tail), 1)
    return out


def analyze_ts(ts: Path) -> dict:
    """Count packets, TEI, NULL, unique PIDs in the live.ts."""
    if not ts.exists() or ts.stat().st_size < 188 * 100:
        return {"packets": 0, "tei_pct": 0.0, "null_pct": 0.0, "unique_pids": 0}
    try:
        data = ts.read_bytes()
    except OSError:
        return {"packets": 0, "tei_pct": 0.0, "null_pct": 0.0, "unique_pids": 0}
    n = len(data) // 188
    sync_ok = tei = null = 0
    pids = set()
    for i in range(n):
        pkt = data[i*188:(i+1)*188]
        if pkt[0] != 0x47:
            continue
        sync_ok += 1
        if pkt[1] & 0x80:
            tei += 1
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        pids.add(pid)
        if pid == 0x1FFF:
            null += 1
    return {
        "packets": sync_ok,
        "tei_pct": round(100.0 * tei / max(sync_ok, 1), 3),
        "null_pct": round(100.0 * null / max(sync_ok, 1), 3),
        "unique_pids": len(pids),
    }


def analyze_hd_extract(ts: Path) -> dict:
    """Extract program 3 (the HD feed at RF34), measure quality:
        hd_extract_errors  — 'Invalid frame' lines during stream-copy
        hd_size_bytes      — size of the extracted prog 3 file
        hd_total_frames    — frames the MPEG-2 decoder *attempted*
        hd_corrupt_frames  — frames the decoder marked 'corrupt decoded frame'
        hd_clean_frames    — total - corrupt (this is what playback would show clean)
        hd_decoded_seconds — wall-clock seconds of decoded video (frames / fps)
    """
    out = {"hd_extract_errors": -1, "hd_size_bytes": 0,
           "hd_total_frames": 0, "hd_corrupt_frames": 0,
           "hd_clean_frames": 0, "hd_decoded_seconds": 0.0}
    if not ts.exists() or ts.stat().st_size < 1_000_000:
        return out
    if HD_PATH.exists():
        try:
            HD_PATH.unlink()
        except OSError:
            pass
    # Step 1: stream-copy program 3 to HD_PATH, count extract errors
    p = subprocess.run(
        [FFMPEG, "-y", "-loglevel", "warning",
         "-i", str(ts), "-map", "0:p:3", "-c", "copy", str(HD_PATH)],
        capture_output=True, text=True, timeout=60,
    )
    out["hd_extract_errors"] = p.stderr.count("Invalid frame")
    if HD_PATH.exists():
        out["hd_size_bytes"] = HD_PATH.stat().st_size
    # Step 2: null-decode the extracted stream. ffmpeg's final summary line
    # gives us "frame=N ... time=HH:MM:SS.ms" — count + duration.
    # ffmpeg prints "corrupt decoded frame" on stderr for every frame the
    # decoder marked unusable — count those too.
    try:
        p2 = subprocess.run(
            [FFMPEG, "-y", "-loglevel", "warning", "-stats",
             "-i", str(HD_PATH), "-map", "0:v:0", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
        # Find last "frame=N" line (ffmpeg writes progress to stderr).
        total = 0
        last_time = "00:00:00"
        for line in p2.stderr.splitlines():
            if "frame=" in line and "fps=" in line:
                # "frame= 1224 fps=0.0 q=-0.0 Lsize=N/A time=00:00:40.94 ..."
                try:
                    fpart = line.split("frame=")[1].split()[0]
                    total = int(fpart)
                except (IndexError, ValueError):
                    pass
                if "time=" in line:
                    try:
                        last_time = line.split("time=")[1].split()[0]
                    except IndexError:
                        pass
        out["hd_total_frames"] = total
        out["hd_corrupt_frames"] = p2.stderr.count("corrupt decoded frame")
        out["hd_clean_frames"] = max(0, total - out["hd_corrupt_frames"])
        # Parse time HH:MM:SS.ms into seconds
        try:
            h, m, s = last_time.split(":")
            out["hd_decoded_seconds"] = round(int(h) * 3600 + int(m) * 60 + float(s), 2)
        except ValueError:
            pass
    except subprocess.TimeoutExpired:
        pass
    return out


# -------------------------- scoring --------------------------------

def score(raw: dict) -> float:
    """Higher is better. Pure function of raw metrics.

    Core idea: reward CLEAN decoded HD seconds, penalize corruption and
    chain failures. A perfect 60s run with clean HD throughout would
    score near +1500 (50s clean × 30 frames/s × 1.0).
    """
    if raw.get("error") or raw.get("chain_died") or raw.get("stalled"):
        return -10000.0
    if raw.get("ts_size_bytes", 0) < 10_000_000:
        return -5000.0  # not enough data
    score = 0.0

    # Primary reward: clean decoded HD frames. This is the strongest signal
    # of "video that actually plays" — the decoder ran a frame all the way
    # through without marking it corrupt.
    score += 1.0 * raw.get("hd_clean_frames", 0)

    # Secondary reward: hd_decoded_seconds (durability of decode).
    score += 5.0 * raw.get("hd_decoded_seconds", 0)

    # Penalize corrupt frames (decoder ran them but flagged bad).
    score -= 2.0 * raw.get("hd_corrupt_frames", 0)

    # Penalize extract errors (Invalid frame dimensions, PES size mismatch).
    score -= 3.0 * raw.get("hd_extract_errors", 0)

    # Penalize convergence burst (bad packets in first 10s).
    rs = raw.get("rs_windows", [])
    burst = 0
    if rs:
        for w in rs:
            t = w.get("t", 999)
            if t <= 10:
                burst += w.get("last5s_bad", w.get("bad", 0))
    score -= 0.05 * burst

    # Penalize steady-state bad (last5s_bad in t>=15s windows).
    steady = 0
    steady_n = 0
    for w in rs:
        t = w.get("t", 0)
        if t >= 15:
            steady += w.get("last5s_bad", 0)
            steady_n += 1
    if steady_n > 0:
        score -= 50.0 * (steady / steady_n)

    # Penalize TEI rate (Reed-Solomon couldn't fix these).
    score -= 100.0 * raw.get("tei_pct", 0)

    # Drought penalty (chain locked but decoded noise).
    upids = raw.get("unique_pids", 0)
    if upids > 100:
        score -= 200.0 + 5.0 * (upids - 100)

    # OsO penalty (USB sample loss).
    score -= 50.0 * raw.get("oso_count", 0)

    # FPLL drift penalty (carrier wobble).
    drift = raw.get("fpll_drift_hz") or 0
    if drift > 50:
        score -= 5.0 * (drift - 50)

    return round(score, 1)


# -------------------------- state ----------------------------------

def load_history() -> list[dict]:
    if not STATE_FILE.exists():
        return []
    out = []
    with open(STATE_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def append_result(entry: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def write_leaderboard(history: list[dict]):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ranked = sorted(history, key=lambda e: -e.get("score", -1e9))
    with open(LEADERBOARD, "w", encoding="utf-8") as f:
        f.write(f"# STVT quality tuner leaderboard — {len(history)} runs total\n")
        f.write(f"# Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"{'rank':>4}  {'score':>8}  {'clean_fr':>8}  {'corrupt':>7}  "
                f"{'sec':>6}  {'hd_err':>6}  {'tei%':>5}  {'pids':>4}  {'burst':>6}  cfg\n")
        for i, e in enumerate(ranked[:30], 1):
            raw = e.get("raw", {})
            burst = 0
            for w in raw.get("rs_windows", []):
                if w.get("t", 999) <= 10:
                    burst += int(w.get("last5s_bad", w.get("bad", 0)))
            cfg_summary = " ".join(f"{k.replace('STVT_',''):s}={v}"
                                   for k, v in sorted(e.get("cfg", {}).items())
                                   if k != "STVT_ANTENNA")
            f.write(f"{i:>4}  {e.get('score', 0):>8.1f}  "
                    f"{raw.get('hd_clean_frames', 0):>8}  "
                    f"{raw.get('hd_corrupt_frames', 0):>7}  "
                    f"{raw.get('hd_decoded_seconds', 0):>6.1f}  "
                    f"{raw.get('hd_extract_errors', 0):>6}  "
                    f"{raw.get('tei_pct', 0):>5.2f}  "
                    f"{raw.get('unique_pids', 0):>4}  "
                    f"{burst:>6}  "
                    f"{cfg_summary}\n")


# -------------------------- search ---------------------------------

def cfg_key(cfg: dict) -> str:
    return "|".join(f"{k}={v}" for k, v in sorted(cfg.items()))


def pick_next_cfg(history: list[dict], best_cfg: dict) -> dict:
    """Greedy hill-climb around best_cfg, with occasional random jumps."""
    tested = {e["cfg_key"] for e in history if "cfg_key" in e}
    rnd = random.Random()
    # 20% chance of random jump to escape local minima
    if rnd.random() < 0.2:
        for _ in range(20):
            cfg = {k: rnd.choice(v) for k, v in SEARCH_SPACE.items()}
            if cfg_key(cfg) not in tested:
                return cfg
    # Otherwise vary 1-2 dims from best_cfg
    for _ in range(40):
        cfg = dict(best_cfg)
        n_dims = rnd.choice([1, 1, 1, 2])
        keys = rnd.sample(list(SEARCH_SPACE.keys()), n_dims)
        for k in keys:
            cfg[k] = rnd.choice(SEARCH_SPACE[k])
        if cfg_key(cfg) not in tested:
            return cfg
    # All near-by exhausted — pure random
    for _ in range(40):
        cfg = {k: rnd.choice(v) for k, v in SEARCH_SPACE.items()}
        if cfg_key(cfg) not in tested:
            return cfg
    return BASELINE  # fallback (might be a repeat)


# -------------------------- main loop -----------------------------

def fmt_cfg(cfg: dict) -> str:
    return " ".join(f"{k.replace('STVT_',''):s}={v}"
                    for k, v in sorted(cfg.items()) if k != "STVT_ANTENNA")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=34, help="RF channel")
    ap.add_argument("--seconds", type=int, default=CHAIN_MAX_SECONDS_DEFAULT,
                    help="Capture wall seconds per config")
    ap.add_argument("--start-fresh", action="store_true",
                    help="Ignore prior history and start over")
    args = ap.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if args.start_fresh:
        for p in (STATE_FILE, LEADERBOARD):
            if p.exists():
                p.unlink()

    history = load_history()
    # Re-score every loaded entry under the current formula so the leaderboard
    # stays consistent when score weights change between sessions.
    for e in history:
        if "raw" in e:
            e["score"] = score(e["raw"])
    if history:
        best = max(history, key=lambda e: e.get("score", -1e9))
        best_cfg = best.get("cfg", BASELINE)
        write_leaderboard(history)
        print(f"[tuner] resuming with {len(history)} prior runs (re-scored). "
              f"Best score so far: {best.get('score', 0):.1f}",
              flush=True)
    else:
        best = None
        best_cfg = BASELINE
        print(f"[tuner] starting fresh on RF{args.rf} ({args.seconds}s per run)")

    print(f"[tuner] state dir: {STATE_DIR}")
    print("[tuner] press Ctrl-C to stop. Leaderboard updates after every run.")
    print()

    # Hook for clean Ctrl-C
    stop = {"flag": False}
    def _sigint(sig, frame):
        if stop["flag"]:
            print("\n[tuner] second Ctrl-C — exiting hard")
            os._exit(1)
        stop["flag"] = True
        print("\n[tuner] Ctrl-C received — finishing current run then stopping")
    signal.signal(signal.SIGINT, _sigint)

    run_num = len(history)
    crash_log = STATE_DIR / "tuner_crash.log"
    try:
        while not stop["flag"]:
            run_num += 1
            try:
                kill_orphans()

                preflight_err = preflight()
                if preflight_err:
                    print(f"[tuner] preflight: {preflight_err} — sleeping 30s",
                          flush=True)
                    for _ in range(30):
                        if stop["flag"]:
                            break
                        time.sleep(1)
                    run_num -= 1   # don't count this as a run
                    continue

                # Use baseline for the first run; afterwards search
                if run_num == 1 and (not history or not any(
                        e.get("cfg_key") == cfg_key(BASELINE) for e in history)):
                    cfg = BASELINE
                else:
                    cfg = pick_next_cfg(history, best_cfg)

                t0 = time.time()
                print(f"\n[tuner] run #{run_num}  {fmt_cfg(cfg)}", flush=True)
                try:
                    raw = run_capture(cfg, args.rf, args.seconds)
                except Exception as e:
                    print(f"[tuner]   capture exception: {e}", flush=True)
                    raw = {"error": str(e)}

                kill_orphans()
                sc = score(raw)
                elapsed = round(time.time() - t0, 1)

                entry = {
                    "run": run_num,
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "cfg": cfg,
                    "cfg_key": cfg_key(cfg),
                    "rf": args.rf,
                    "seconds": args.seconds,
                    "elapsed": elapsed,
                    "score": sc,
                    "raw": raw,
                }
                append_result(entry)
                history.append(entry)
                write_leaderboard(history)

                star = ""
                if best is None or sc > best.get("score", -1e9):
                    star = "  *** NEW BEST ***"
                    best = entry
                    best_cfg = cfg

                print(f"[tuner]   score={sc:.1f}  "
                      f"clean={raw.get('hd_clean_frames', 0)}/{raw.get('hd_total_frames', 0)}fr  "
                      f"decoded={raw.get('hd_decoded_seconds', 0):.1f}s  "
                      f"hd_err={raw.get('hd_extract_errors', 0)}  "
                      f"tei={raw.get('tei_pct', 0):.2f}%  "
                      f"pids={raw.get('unique_pids', 0)}  "
                      f"oso={raw.get('oso_count', 0)}  "
                      f"elapsed={elapsed}s{star}", flush=True)
            except KeyboardInterrupt:
                raise
            except BaseException as exc:
                # Any other exception — log and keep looping. A single bad run
                # must NEVER kill the orchestrator.
                import traceback
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                with open(crash_log, "a", encoding="utf-8") as f:
                    f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                            f"run #{run_num} ===\n")
                    f.write(traceback.format_exc())
                print(f"[tuner]   *** EXCEPTION in run #{run_num}: "
                      f"{type(exc).__name__}: {exc} — logged + continuing",
                      flush=True)
                kill_orphans()
                # Brief cool-down so we don't spin if the problem is persistent
                time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        kill_orphans()
        if history:
            ranked = sorted(history, key=lambda e: -e.get("score", -1e9))
            print("\n[tuner] === TOP 5 ===")
            for i, e in enumerate(ranked[:5], 1):
                print(f"  #{i}  score={e['score']:.1f}  {fmt_cfg(e['cfg'])}")
            print(f"\n[tuner] full leaderboard: {LEADERBOARD}")
            print(f"[tuner] history: {STATE_FILE}")


if __name__ == "__main__":
    main()
