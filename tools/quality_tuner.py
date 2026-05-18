#!/usr/bin/env python3
"""quality_tuner.py — autonomous knob-tuning agent for the STVT chain.

Runs tv_tuner.py --no-play in repeated cells (default 180s each), parses
the watchdog log for restart events and PAT droughts, decodes the TS via
ffmpeg, and hill-climbs over a knob grid that includes equalizer, sync,
SDR, and watchdog parameters.

Scoring (per cell):
    score = decoded_frames - 100 * restart_count - 5 * drought_strikes
                           - 0.5 * audio_errors
A watchdog restart = ~5s of black screen, weighted high. A PAT drought
strike = 5s of degraded content but no full restart.

State persisted to /tmp/tuner_state.json so the agent resumes mid-sweep
if the box reboots or the run is Ctrl+C'd.

Usage:
    python3 tools/quality_tuner.py [--budget MINUTES] [--cell-seconds N]
                                   [--rf 34] [--reset]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

STVT = Path(os.environ.get("STVT_HOME", "/home/user/Software-TV-Tuner"))
AB_VARIANT = STVT / "tools" / "tv_live_ab.py"
LIVE = STVT / "tools" / "tv_live.py"
LIVE_TS = STVT / "tools/data/tv_live/live.ts"
STATE_PATH = Path("/tmp/tuner_state.json")
LOG_PATH = Path("/tmp/tuner.log")
LEADERBOARD_PATH = Path("/tmp/tuner_leaderboard.txt")
RESULTS_DIR = Path("/tmp/tuner_results")
TUNER_STDOUT = Path("/tmp/tuner_cell_stdout.log")
LOCK_PATH = Path("/tmp/tuner.lock")

# ----------------------------------------------------------------------
#  Knobs & default config
# ----------------------------------------------------------------------

# Discrete knob values to explore. Centered on the current winner (cma
# equalizer + LOCK=3.5 + UNLOCK=2.0 + T6 family), perturbing values most
# likely to reduce equalizer soft-drift events that cause PAT droughts.
KNOBS = {
    # 2026-05-15 pass 5: include big architectural choices. Soft viterbi
    # + cma equalizer may be CPU-saturating one core and causing
    # continuous OsO; hard viterbi + long eq is the historic baseline.
    "STVT_VITERBI":                  ["hard", "soft"],
    "STVT_EQ":                       ["cma", "long", "multifs"],
    "ATSC_SYNC_SOFT_LOCK":           [3.0, 3.5, 4.0],
    "ATSC_SYNC_SOFT_UNLOCK":         [1.5, 1.8, 2.0, 2.2],
    "ATSC_SYNC_SOFT_STICKY":         [0.95, 0.98, 0.99],
    "STVT_AGC_REFERENCE":            [3.0, 4.0, 5.0, 6.0],
    "STVT_AGC_ALPHA":                ["1e-7", "1e-6", "1e-5"],
    "STVT_FPLL_ALPHA":               ["5e-4", "1e-3", "2e-3"],
    "STVT_FPLL_AFC_TAU":             [15, 25, 50],
    "STVT_DCR_TAPS":                 [32, 64, 128],
    "ATSC_T6_LEAK":                  ["1e-4", "5e-4", "1e-3", "5e-3"],
    "ATSC_T6_MU_LMS_FS":             ["1e-5", "5e-5", "1e-4"],
}

# Starting point = tuner pass 3 high-quality config. That config had
# the best decoded-frame count and zero forced restarts in 180 s cells,
# but caused continuous PAT droughts in real streaming. We now
# perturb from there with a drought-aware metric, searching for a config
# that keeps the quality but also delivers PSI continuously.
DEFAULT_CONFIG = {
    "STVT_EQ":                       "cma",
    "STVT_VITERBI":                  "hard",
    "ATSC_SYNC_SOFT_STICKY":         0.99,
    "ATSC_SYNC_SOFT_LOCK":           3.0,
    "ATSC_SYNC_SOFT_UNLOCK":         2.2,
    "ATSC_SYNC_SOFT_EMIT_UNLOCKED":  1,
    "STVT_IFGR":                     59,
    "STVT_RFGAIN_SEL":               5,
    "STVT_AGC_ALPHA":                "1e-6",
    "STVT_AGC_REFERENCE":            3.0,
    "STVT_FPLL_ALPHA":               "1e-3",
    "STVT_FPLL_AFC_TAU":             50,
    "STVT_DCR_TAPS":                 64,
    "ATSC_T6_MU_CMA":                "0",
    "ATSC_T6_MU_LMS_FS":             "5e-5",
    "ATSC_T6_MU_DFE":                "1e-4",
    "ATSC_T6_DFE_GATE":              1.0,
    "ATSC_T6_LEAK":                  "5e-4",
}

# Set high so the agent keeps grinding — we stop only at budget or
# 3 full passes with no improvement.
TARGET_MULTIPLIER = 100.0

# Stable env vars (chain chassis — never change).
CHASSIS = {
    "SDL_VIDEODRIVER":          "x11",
    "GR_VMCIRCBUF_BUFFER_TYPE": "mmap",
    "GR_MAX_BUFF_SIZE":         "8388608",
    "STVT_NATIVE_RATE":         "6000000",
    "STVT_RESAMP_INTERP":       "25",
    "STVT_RESAMP_DECIM":        "24",
    "STVT_FFPLAY_HWACCEL":      "none",
}

# ----------------------------------------------------------------------
#  Logging
# ----------------------------------------------------------------------

def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")

# ----------------------------------------------------------------------
#  Chain run + quality measurement
# ----------------------------------------------------------------------

def kill_chain() -> None:
    for pat in ("tv_tuner.py", "tv_live.py", "ffplay", "mpv"):
        subprocess.run(["pkill", "-9", "-f", pat], stderr=subprocess.DEVNULL)
    time.sleep(2)


def stage_ab_variant() -> Path:
    """Back up current tv_live.py and swap in the AB variant; return backup path."""
    backup = LIVE.with_suffix(".tuner_backup")
    shutil.copy(LIVE, backup)
    shutil.copy(AB_VARIANT, LIVE)
    return backup


def restore_chain(backup: Path) -> None:
    if backup.exists():
        shutil.copy(backup, LIVE)


def run_chain(cfg: dict, seconds: int, rf: int = 34) -> dict | None:
    """Run tv_tuner.py --no-play for `seconds`. Captures stdout so we can
    parse watchdog events (decoder droughts/restarts). Returns dict with
    `ts_path` and `stdout_path`, or None on failure.

    Using tv_tuner.py (not tv_live.py) means we get the convergence
    retry + decoder-watchdog logic that real users hit — that's the
    quality we want to optimize for.
    """
    kill_chain()

    env = os.environ.copy()
    env.update({k: str(v) for k, v in CHASSIS.items()})
    env.update({k: str(v) for k, v in cfg.items()})

    if cfg.get("STVT_EQ") == "pilot_dd":
        env["PYTHONPATH"]      = str(STVT / "gr-atscplus/build/test_modules") + ":" + env.get("PYTHONPATH", "")
        env["LD_LIBRARY_PATH"] = str(STVT / "gr-atscplus/build/lib") + ":" + env.get("LD_LIBRARY_PATH", "")
        env.setdefault("PILOT_DD_MU", "0")

    if LIVE_TS.exists():
        LIVE_TS.write_bytes(b"")

    # tv_tuner.py --no-play: spawns tv_live + watchdog, no ffplay window
    cmd = [
        "timeout", str(seconds + 5),
        "python3", "tools/tv_tuner.py",
        "--rf", str(rf), "--no-play",
    ]
    stdout_path = TUNER_STDOUT.with_suffix(f".{int(time.time())}.log")
    try:
        with stdout_path.open("w") as out:
            proc = subprocess.Popen(
                cmd, cwd=str(STVT), env=env,
                stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                proc.wait(timeout=seconds + 20)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: proc.kill()
    finally:
        kill_chain()

    if not LIVE_TS.exists() or LIVE_TS.stat().st_size < 1_000_000:
        return None
    return {"ts_path": LIVE_TS, "stdout_path": stdout_path}


# ffmpeg quality metrics
INVALID_MB = re.compile(r"Invalid mb type|invalid cbp|motion_type at|MVs not available|overread")
CONCEAL    = re.compile(r"concealing")
AUDIO_ERR  = re.compile(r"error decoding the audio block|exponent .* out-of-range|expacc .* out-of-range")
FRAME_LINE = re.compile(r"frame=\s*(\d+)\s+fps=\s*([\d.]+).*time=([\d:.]+)")


def parse_time(s: str) -> float:
    """Convert hh:mm:ss.xx → seconds."""
    parts = s.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except Exception:
        return 0.0


RESTART_RE  = re.compile(r"decoder drifted — restarting")
DROUGHT_RE  = re.compile(r"decoder quality low: PAT=0 \(strike (\d+)/")
RELOCKS_RE  = re.compile(r"sync_soft FINAL.*relocks=(\d+)")
ALIGNED_RE  = re.compile(r"sync_soft FINAL.*segs_aligned=\d+ \(([\d.]+)%\)")

# Watchdog checks PAT every DECODER_CHECK_INTERVAL_SEC (5s default in
# tv_tuner.py). Each strike = ~5 wall-seconds of PAT-absent in the rolling
# 5MB TS window, which the user sees as frozen/garbled video.
WATCHDOG_INTERVAL_SEC = 5.0


def parse_watchdog_events(stdout_path: Path) -> dict:
    """Parse tv_tuner stdout for drought events AND clean windows.
    longest_clean_window_sec is the user-relevant number — that's how
    long the cache has to fill before the next drought, so it caps how
    smoothly we can stream with any cache size."""
    restarts = 0
    droughts = 0
    max_drought_sec = 0.0
    total_drought_sec = 0.0
    longest_clean_window_sec = 0.0
    # Track times of every drought event start/end to compute clean gaps
    drought_events = []
    cell_end_t = 0.0
    if stdout_path and stdout_path.exists():
        text = stdout_path.read_text(errors="ignore")
        restarts = len(RESTART_RE.findall(text))
        # Walk strike sequence + corresponding wall-time. Status lines look
        # like "[  48s] tv=OK ..." — interleave them with strike lines.
        STATUS_RE = re.compile(r"^\[\s*(\d+)s\]", re.M)
        cur_time = 0.0
        last_status_t = 0.0
        prev_strike = 0
        cur_event_start_t = 0.0
        cur_event_strikes = 0
        for line in text.splitlines():
            sm = STATUS_RE.match(line)
            if sm:
                last_status_t = float(sm.group(1))
                cell_end_t = last_status_t
                continue
            dm = DROUGHT_RE.search(line)
            if dm:
                n = int(dm.group(1))
                if n <= prev_strike or prev_strike == 0:
                    # New event starts here
                    if cur_event_strikes > 0:
                        # Close prior event
                        event_dur = cur_event_strikes * WATCHDOG_INTERVAL_SEC
                        droughts += 1
                        total_drought_sec += event_dur
                        if event_dur > max_drought_sec:
                            max_drought_sec = event_dur
                        drought_events.append((cur_event_start_t,
                                               cur_event_start_t + event_dur))
                    cur_event_start_t = last_status_t
                    cur_event_strikes = n
                else:
                    cur_event_strikes = n
                prev_strike = n
        # Flush last drought event
        if cur_event_strikes > 0:
            event_dur = cur_event_strikes * WATCHDOG_INTERVAL_SEC
            droughts += 1
            total_drought_sec += event_dur
            if event_dur > max_drought_sec:
                max_drought_sec = event_dur
            drought_events.append((cur_event_start_t,
                                   cur_event_start_t + event_dur))
        # Compute the longest gap between droughts (clean window length).
        # Includes [0, first_event_start] and [last_event_end, cell_end].
        prev_end = 0.0
        for start, end in drought_events:
            gap = max(0.0, start - prev_end)
            if gap > longest_clean_window_sec:
                longest_clean_window_sec = gap
            prev_end = end
        tail_gap = max(0.0, cell_end_t - prev_end)
        if tail_gap > longest_clean_window_sec:
            longest_clean_window_sec = tail_gap
    relocks = 0
    aligned_pct = 0.0
    chain_log = STVT / "tools/data/tv_live/tv_tuner.tv_live.log"
    if chain_log.exists():
        try:
            tail = chain_log.read_text(errors="ignore").splitlines()[-5000:]
            for line in reversed(tail):
                m = RELOCKS_RE.search(line)
                if m and not relocks:
                    relocks = int(m.group(1))
                m2 = ALIGNED_RE.search(line)
                if m2 and not aligned_pct:
                    aligned_pct = float(m2.group(1))
                if relocks and aligned_pct:
                    break
        except Exception:
            pass
    return {"restarts": restarts, "droughts": droughts,
            "max_drought_sec": max_drought_sec,
            "total_drought_sec": total_drought_sec,
            "longest_clean_window_sec": longest_clean_window_sec,
            "relocks": relocks, "aligned_pct": aligned_pct}


def measure_quality(ts: Path, decode_seconds: int = 60) -> dict:
    """Decode TS via ffmpeg; return frame count, fps, error counts."""
    cmd = [
        "ffmpeg", "-hide_banner",
        "-analyzeduration", "100000000",
        "-probesize",       "100000000",
        "-err_detect", "ignore_err",
        "-fflags", "+genpts+igndts+discardcorrupt",
        "-t", str(decode_seconds),
        "-i", str(ts),
        "-f", "null", "-",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=decode_seconds * 4)
    except subprocess.TimeoutExpired:
        return dict(frames=0, video_err=0, audio_err=0, fps=0.0, ts_seconds=0.0, fail="timeout")

    stderr = r.stderr or ""
    video_err = len(INVALID_MB.findall(stderr)) + len(CONCEAL.findall(stderr))
    audio_err = len(AUDIO_ERR.findall(stderr))

    frames = 0; ts_seconds = 0.0
    for m in FRAME_LINE.finditer(stderr):
        frames = int(m.group(1)); ts_seconds = parse_time(m.group(3))
    # ffmpeg reports fps=0 when decoding to null faster than realtime —
    # compute it from frames/seconds instead.
    fps = (frames / ts_seconds) if ts_seconds > 0 else 0.0
    return dict(frames=frames, fps=fps, video_err=video_err,
                audio_err=audio_err, ts_seconds=ts_seconds)


def compute_score(m: dict) -> float:
    """Pass-5 metric: optimize for the player-cache use case. The user
    perceives smooth playback when a clean window is long enough to fill
    a player cache that covers the next drought. So we reward
    `longest_clean_window_sec` heavily and still penalize long droughts.

    Numerically the metric is: minutes of clean uptime worth a lot;
    drought duration counts against you; raw decoded frames give a
    secondary signal on actual quality during the clean windows.
    """
    return (30 * m.get("longest_clean_window_sec", 0)  # bigger clean → better
            + m.get("frames", 0) / 10                  # quality during clean
            -  20 * m.get("max_drought_sec", 0)        # worst single freeze
            -   2 * m.get("total_drought_sec", 0)      # cumulative freeze
            - 100 * m.get("restarts", 0)               # forced chain restart
            - 0.5 * m.get("audio_err", 0))


def evaluate(cfg: dict, cell_seconds: int, rf: int = 34,
             iterations: int = 1) -> dict:
    """Run the chain `iterations` times with cfg, return averaged metrics."""
    runs = []
    for it in range(iterations):
        sess = run_chain(cfg, cell_seconds, rf=rf)
        if sess is None:
            runs.append(dict(frames=0, video_err=0, audio_err=0, fps=0.0,
                             ts_seconds=0.0, restarts=0, droughts=0,
                             max_drought_sec=0.0, total_drought_sec=0.0,
                             longest_clean_window_sec=0.0,
                             relocks=0, aligned_pct=0.0, fail="no_ts"))
            continue
        m = measure_quality(sess["ts_path"], decode_seconds=cell_seconds)
        m.update(parse_watchdog_events(sess["stdout_path"]))
        m["score"] = compute_score(m)
        runs.append(m)

    if not runs:
        return {"cfg": dict(cfg),
                "metrics": dict(score=0.0, frames=0, restarts=0, droughts=0,
                                max_drought_sec=0.0, total_drought_sec=0.0,
                                longest_clean_window_sec=0.0,
                                video_err=0, audio_err=0, ts_seconds=0.0,
                                relocks=0, aligned_pct=0.0),
                "wall_time": time.time(), "iterations": 0}

    keys = ("score", "frames", "video_err", "audio_err", "ts_seconds",
            "fps", "restarts", "droughts", "max_drought_sec",
            "total_drought_sec", "longest_clean_window_sec",
            "relocks", "aligned_pct")
    avg = {k: sum(r.get(k, 0) for r in runs) / len(runs) for k in keys}
    return {"cfg": dict(cfg), "metrics": avg, "wall_time": time.time(),
            "iterations": len(runs), "individual": runs}


# ----------------------------------------------------------------------
#  Optimizer
# ----------------------------------------------------------------------

def perturb(cfg: dict, knob: str, history_keys: set[str]) -> dict | None:
    """Try next unused value for `knob`. Return new cfg or None if exhausted."""
    cur_val = cfg[knob]
    choices = KNOBS[knob]
    if cur_val not in choices:
        # not in grid; snap to nearest
        cur_val = choices[0]
    for v in choices:
        if v == cur_val:
            continue
        new_cfg = dict(cfg); new_cfg[knob] = v
        key = cfg_key(new_cfg)
        if key not in history_keys:
            return new_cfg
    return None


def cfg_key(cfg: dict) -> str:
    """Stable hash of a config — used to dedupe."""
    return json.dumps(cfg, sort_keys=True)


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(STATE_PATH)


def load_state() -> dict | None:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return None
    return None


# ----------------------------------------------------------------------
#  Main
# ----------------------------------------------------------------------

def fmt_metrics(m: dict) -> str:
    return (f"score={m.get('score', 0):>7.1f}  "
            f"clean_win={m.get('longest_clean_window_sec', 0):>5.1f}s  "
            f"frames={int(m.get('frames', 0)):>5}  "
            f"max_dry={m.get('max_drought_sec', 0):>5.1f}s  "
            f"tot_dry={m.get('total_drought_sec', 0):>5.1f}s  "
            f"restarts={int(m.get('restarts', 0))}  "
            f"relocks={int(m.get('relocks', 0)):>4}  "
            f"a_err={int(m.get('audio_err', 0)):>3}")


def write_leaderboard(state: dict) -> None:
    """Write top-20 configs by score to /tmp/tuner_leaderboard.txt."""
    hist = sorted(state["history"],
                  key=lambda h: h.get("metrics", {}).get("score", 0),
                  reverse=True)
    diff_keys = sorted({k for h in state["history"] for k in h["cfg"]})
    # Only show knobs that actually vary across explored configs
    varying = [k for k in diff_keys
               if len({json.dumps(h["cfg"].get(k)) for h in state["history"]}) > 1]
    lines = []
    lines.append(f"TUNER LEADERBOARD  ({len(state['history'])} configs explored)")
    lines.append(f"baseline: {fmt_metrics(state['baseline']['metrics'])}")
    lines.append("")
    lines.append(f"{'#':>3}  {'metrics':<110}  diff vs baseline")
    lines.append("-" * 180)
    base_cfg = state["baseline"]["cfg"]
    for i, h in enumerate(hist[:20]):
        diff = {k: h["cfg"].get(k) for k in varying
                if h["cfg"].get(k) != base_cfg.get(k)}
        diff_str = ", ".join(f"{k}={v}" for k, v in diff.items()) or "(baseline)"
        lines.append(f"{i+1:>3}  {fmt_metrics(h['metrics']):<110}  {diff_str}")
    LEADERBOARD_PATH.write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget",       type=int, default=360, help="minutes of wall-time budget")
    ap.add_argument("--cell-seconds", type=int, default=300, help="seconds per chain run")
    ap.add_argument("--rf",           type=int, default=34,  help="ATSC RF channel")
    ap.add_argument("--reset",        action="store_true",   help="discard saved state")
    args = ap.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # Single-instance lock: if another tuner is running, bail. Stale lock
    # (pid no longer alive) is overwritten.
    if LOCK_PATH.exists():
        try:
            other_pid = int(LOCK_PATH.read_text().strip())
            os.kill(other_pid, 0)   # signal 0 = check liveness only
            print(f"[quality_tuner] another instance already running "
                  f"(pid={other_pid}). Kill it first or rm {LOCK_PATH}.",
                  file=sys.stderr)
            sys.exit(1)
        except (OSError, ValueError):
            pass   # stale; we'll overwrite
    LOCK_PATH.write_text(str(os.getpid()))

    LOG_PATH.write_text("")

    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()

    state = load_state()
    if state is None:
        state = dict(
            baseline=None, target=None,
            best=None, current=dict(DEFAULT_CONFIG),
            history=[],
            knob_idx=0, knob_attempts=0,
            started_at=time.time(),
        )

    backup = stage_ab_variant()
    cleanup = lambda *a: (restore_chain(backup), kill_chain(), sys.exit(0))
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        deadline = state["started_at"] + args.budget * 60

        if state["baseline"] is None:
            log(f"baseline cell with: {DEFAULT_CONFIG}")
            res = None
            for attempt in range(1, 4):
                res = evaluate(DEFAULT_CONFIG, args.cell_seconds, rf=args.rf)
                if res["metrics"]["frames"] > 0:
                    break
                log(f"baseline attempt {attempt}/3 produced 0 frames "
                    f"(no chain lock?). Retrying...")
                kill_chain()
                time.sleep(5)
            state["baseline"] = res
            state["best"]     = res
            # Floor the target so the agent can't satisfy "score >= 0" trivially
            # when baseline is poor.
            state["target"]   = max(res["metrics"]["score"], 100) * TARGET_MULTIPLIER
            state["history"].append(res)
            save_state(state)
            write_leaderboard(state)
            log(f"baseline: {fmt_metrics(res['metrics'])}")
            if res["metrics"]["frames"] == 0:
                log("FATAL: baseline never locked. Check SDR + chain. Exiting.")
                return

        knob_list = list(KNOBS.keys())
        history_keys = {cfg_key(h["cfg"]) for h in state["history"]}
        cycles_no_improve = 0

        while time.time() < deadline:
            if state["best"]["metrics"]["score"] >= state["target"]:
                log(f"TARGET REACHED — score={state['best']['metrics']['score']:.1f}")
                break

            knob = knob_list[state["knob_idx"] % len(knob_list)]
            base = dict(state["best"]["cfg"])
            new_cfg = perturb(base, knob, history_keys)

            if new_cfg is None:
                state["knob_idx"] += 1
                state["knob_attempts"] = 0
                if state["knob_idx"] % len(knob_list) == 0:
                    cycles_no_improve += 1
                    if cycles_no_improve >= 3:
                        log("3 full passes with no improvement — stopping")
                        break
                    log(f"pass {cycles_no_improve} done, looping (best: {state['best']['metrics']['score']:.1f})")
                save_state(state)
                continue

            log(f"try {knob}={new_cfg[knob]!r}  (best: {state['best']['metrics']['score']:.1f})")
            res = evaluate(new_cfg, args.cell_seconds, rf=args.rf)
            history_keys.add(cfg_key(new_cfg))
            state["history"].append(res)

            cur = res["metrics"]
            best = state["best"]["metrics"]
            improvement = cur["score"] - best["score"]
            tag = "(NEW BEST)" if cur["score"] > best["score"] else ""
            log(f"  -> {fmt_metrics(cur)}  Δ={improvement:+.1f} {tag}")

            if cur["score"] > best["score"]:
                state["best"] = res
                cycles_no_improve = 0
                log(f"  cfg: {res['cfg']}")
            else:
                state["knob_attempts"] += 1

            save_state(state)
            write_leaderboard(state)

        log("=" * 60)
        log("TUNING SESSION COMPLETE")
        log("=" * 60)
        log(f"baseline: {fmt_metrics(state['baseline']['metrics'])}")
        log(f"best:     {fmt_metrics(state['best']['metrics'])}")
        if state["baseline"]["metrics"]["score"] > 0:
            ratio = state["best"]["metrics"]["score"] / state["baseline"]["metrics"]["score"]
            log(f"improvement: {ratio:.2f}x")
        log(f"best config:")
        for k, v in sorted(state["best"]["cfg"].items()):
            log(f"  {k}={v}")

        env_path = Path("/tmp/tuner_best.env")
        with env_path.open("w") as f:
            for k, v in state["best"]["cfg"].items():
                f.write(f"export {k}={v}\n")
        log(f"winning env written to {env_path}")
        write_leaderboard(state)

    finally:
        restore_chain(backup)
        kill_chain()
        try: LOCK_PATH.unlink()
        except FileNotFoundError: pass


if __name__ == "__main__":
    main()
