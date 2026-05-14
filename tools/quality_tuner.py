#!/usr/bin/env python3
"""quality_tuner.py — autonomous knob-tuning agent for the STVT chain.

Goal: navigate the parameter space (equalizer, viterbi, sync params, SDR gain)
toward a quality score 2x the baseline. Hill-climbing on one knob at a time;
accept changes that improve quality, revert otherwise.

Quality score = (successful_video_frames + good_audio_seconds) / window_time
- video frames: decoded by ffmpeg without "Invalid mb type" / "concealing"
- good_audio_seconds: derived from absence of "error decoding the audio block"

State is persisted to /tmp/tuner_state.json so the agent can resume after
crashes/freezes.

Usage:
    python3 tools/quality_tuner.py [--budget MINUTES] [--cell-seconds N]
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
RESULTS_DIR = Path("/tmp/tuner_results")

# ----------------------------------------------------------------------
#  Knobs & default config
# ----------------------------------------------------------------------

# Discrete knob values to explore. Pruned to values likely worth trying:
# - past sweeps already converged on STVT_EQ=long, hard viterbi, STICKY=0.98
# - skip alternate equalizers (all worse than long), keep long-only
# - keep sync knobs (LOCK=3.0 emerged from first agent run)
# - add chassis knobs (AGC, FPLL, DCR) that were never explored
KNOBS = {
    "ATSC_SYNC_SOFT_LOCK":           [2.5, 3.0, 3.5, 4.0],
    "ATSC_SYNC_SOFT_UNLOCK":         [1.5, 2.0, 2.5],
    "STVT_AGC_ALPHA":                ["3e-7", "1e-6", "3e-6", "1e-5"],
    "STVT_AGC_REFERENCE":            [2.0, 4.0, 6.0],
    "STVT_FPLL_ALPHA":               ["5e-4", "1e-3", "2e-3", "5e-3"],
    "STVT_FPLL_AFC_TAU":             [10, 25, 50, 100],
    "STVT_DCR_TAPS":                 [16, 32, 64, 128],
    "STVT_IFGR":                     [50, 55, 59],
}

# Starting point = current winners (long + LOCK=3.0 + defaults elsewhere).
DEFAULT_CONFIG = {
    "STVT_EQ":                       "long",
    "STVT_VITERBI":                  "hard",
    "ATSC_SYNC_SOFT_STICKY":         0.98,
    "ATSC_SYNC_SOFT_LOCK":           3.0,
    "ATSC_SYNC_SOFT_UNLOCK":         2.5,
    "ATSC_SYNC_SOFT_EMIT_UNLOCKED":  1,
    "STVT_IFGR":                     59,
    "STVT_RFGAIN_SEL":               5,
    "STVT_AGC_ALPHA":                "1e-6",
    "STVT_AGC_REFERENCE":            4.0,
    "STVT_FPLL_ALPHA":               "1e-3",
    "STVT_FPLL_AFC_TAU":             25,
    "STVT_DCR_TAPS":                 32,
}

# Multiplier above baseline score that the agent treats as "good enough"
# to stop. Set high so it keeps grinding — we stop only at budget or
# full-pass-no-improvement instead.
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


def run_chain(cfg: dict, seconds: int) -> Path | None:
    """Run the chain with the given config for `seconds`. Returns path to live.ts.
    Returns None on failure.
    """
    kill_chain()

    # Build env
    env = os.environ.copy()
    env.update({k: str(v) for k, v in CHASSIS.items()})
    env.update({k: str(v) for k, v in cfg.items()})

    if cfg.get("STVT_EQ") == "pilot_dd":
        # patched pilot_dd lives in build dir
        env["PYTHONPATH"]      = str(STVT / "gr-atscplus/build/test_modules") + ":" + env.get("PYTHONPATH", "")
        env["LD_LIBRARY_PATH"] = str(STVT / "gr-atscplus/build/lib") + ":" + env.get("LD_LIBRARY_PATH", "")
        env.setdefault("PILOT_DD_MU", "0")    # DD diverges; keep disabled

    # Truncate live.ts so we measure only this run
    if LIVE_TS.exists():
        LIVE_TS.write_bytes(b"")

    # Spawn the chain directly via tv_live.py (which produces live.ts).
    # tv_tuner.py is a wrapper that also spawns ffmpeg/ffplay — overkill here.
    # Skip `chrt -f 99` because the subprocess doesn't inherit the shell's
    # rlimit-rtprio; would fail silently and exit chain instantly.
    cmd = [
        "timeout", str(seconds),
        "python3", "tools/tv_live.py",
        "--rf", "34",
        "--out", str(LIVE_TS),
    ]
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(STVT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=seconds + 10)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        kill_chain()

    if not LIVE_TS.exists() or LIVE_TS.stat().st_size < 1_000_000:
        return None
    return LIVE_TS


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


def measure_quality(ts: Path, decode_seconds: int = 25) -> dict:
    """Decode `decode_seconds` of TS via ffmpeg, return quality dict.

    Quality score = video_frames_decoded - 0.1*video_errors - audio_errors,
    normalized by decode_seconds.
    """
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
        return dict(score=0.0, frames=0, video_err=0, audio_err=0, fps=0.0, ts_seconds=0.0, fail="timeout")

    stderr = r.stderr or ""
    video_err = len(INVALID_MB.findall(stderr)) + len(CONCEAL.findall(stderr))
    audio_err = len(AUDIO_ERR.findall(stderr))

    # Last "frame= N fps= F time= T" line.
    frames = 0; fps = 0.0; ts_seconds = 0.0
    for m in FRAME_LINE.finditer(stderr):
        frames = int(m.group(1)); fps = float(m.group(2)); ts_seconds = parse_time(m.group(3))

    # Score formula:
    # - reward frame count (more frames = less drop)
    # - penalize video errors (each MB error costs 0.1 frames)
    # - penalize audio errors (each costs 1 frame)
    # All normalized to per-second.
    norm = max(1.0, ts_seconds)
    score = (frames - 0.1 * video_err - audio_err) / norm
    return dict(
        score=score, frames=frames, fps=fps,
        video_err=video_err, audio_err=audio_err,
        ts_seconds=ts_seconds,
    )


def evaluate(cfg: dict, cell_seconds: int, iterations: int = 2) -> dict:
    """Run the chain `iterations` times with cfg, return averaged metrics.

    Scoring uses RAW frame count (less variance than per-second rate, which
    inflates short bursts of dense decode).
    """
    runs = []
    for it in range(iterations):
        ts = run_chain(cfg, cell_seconds)
        if ts is None:
            runs.append(dict(score=0.0, frames=0, video_err=0, audio_err=0, fps=0.0, ts_seconds=0.0, fail="no_ts"))
            continue
        m = measure_quality(ts, decode_seconds=int(cell_seconds * 0.8))
        # Recompute score as RAW frames - 0.1*v_err - a_err (no time normalization).
        m["score"] = m["frames"] - 0.1 * m["video_err"] - m["audio_err"]
        runs.append(m)
    # Average key metrics across runs.
    if not runs:
        return {"cfg": dict(cfg), "metrics": dict(score=0.0, frames=0, video_err=0, audio_err=0, ts_seconds=0.0), "wall_time": time.time(), "iterations": 0}
    avg = {k: sum(r.get(k, 0) for r in runs) / len(runs) for k in ("score", "frames", "video_err", "audio_err", "ts_seconds", "fps")}
    return {"cfg": dict(cfg), "metrics": avg, "wall_time": time.time(), "iterations": len(runs), "individual": runs}


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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget",       type=int, default=60, help="minutes of wall-time budget")
    ap.add_argument("--cell-seconds", type=int, default=30, help="seconds per chain run")
    ap.add_argument("--reset",        action="store_true",  help="discard saved state")
    args = ap.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
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

    # Stage AB variant
    backup = stage_ab_variant()
    cleanup = lambda *a: (restore_chain(backup), kill_chain(), sys.exit(0))
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        deadline = state["started_at"] + args.budget * 60

        # Baseline
        if state["baseline"] is None:
            log(f"establishing baseline with: {DEFAULT_CONFIG}")
            res = evaluate(DEFAULT_CONFIG, args.cell_seconds)
            state["baseline"] = res
            state["best"]     = res
            state["target"]   = res["metrics"]["score"] * TARGET_MULTIPLIER
            state["history"].append(res)
            save_state(state)
            log(f"baseline score={res['metrics']['score']:.2f}/s  frames={res['metrics']['frames']}  "
                f"video_err={res['metrics']['video_err']}  audio_err={res['metrics']['audio_err']}  "
                f"target=2x baseline = {state['target']:.2f}/s")

        # Optimizer loop: coordinate descent
        knob_list = list(KNOBS.keys())
        history_keys = {cfg_key(h["cfg"]) for h in state["history"]}
        cycles_no_improve = 0

        while time.time() < deadline:
            if state["best"]["metrics"]["score"] >= state["target"]:
                log(f"TARGET REACHED — score={state['best']['metrics']['score']:.2f}/s >= {state['target']:.2f}/s")
                break

            knob = knob_list[state["knob_idx"] % len(knob_list)]
            base = dict(state["best"]["cfg"])
            new_cfg = perturb(base, knob, history_keys)

            if new_cfg is None:
                # All values for this knob tried — advance
                state["knob_idx"] += 1
                state["knob_attempts"] = 0
                if state["knob_idx"] % len(knob_list) == 0:
                    cycles_no_improve += 1
                    if cycles_no_improve >= 3:
                        log("3 full passes with no improvement — stopping")
                        break
                    log(f"completed pass {cycles_no_improve}, looping again (best so far: {state['best']['metrics']['score']:.2f}/s)")
                save_state(state)
                continue

            log(f"trying knob={knob} value={new_cfg[knob]!r} (best so far: score={state['best']['metrics']['score']:.2f}/s)")
            res = evaluate(new_cfg, args.cell_seconds)
            history_keys.add(cfg_key(new_cfg))
            state["history"].append(res)

            cur = res["metrics"]
            best = state["best"]["metrics"]
            improvement = cur["score"] - best["score"]
            tag = "(better!)" if cur["score"] > best["score"] else ""
            log(f"  → score={cur['score']:.2f}/s  frames={cur['frames']}  fps={cur['fps']:.1f}  "
                f"video_err={cur['video_err']}  audio_err={cur['audio_err']}  Δ={improvement:+.2f}/s  {tag}")

            if cur["score"] > best["score"]:
                state["best"] = res
                cycles_no_improve = 0
                log(f"NEW BEST: {res['cfg']}")
            else:
                state["knob_attempts"] += 1

            save_state(state)

        # Report
        log("=" * 60)
        log("TUNING SESSION COMPLETE")
        log("=" * 60)
        b = state["baseline"]["metrics"]
        w = state["best"]["metrics"]
        log(f"baseline:  score={b['score']:.2f}/s  frames={b['frames']}  errors=v{b['video_err']} a{b['audio_err']}")
        log(f"best:      score={w['score']:.2f}/s  frames={w['frames']}  errors=v{w['video_err']} a{w['audio_err']}")
        log(f"improvement: {w['score']/b['score']:.2f}x")
        log(f"best config: {json.dumps(state['best']['cfg'], indent=2)}")

        # Write winning env file
        env_path = Path("/tmp/tuner_best.env")
        with env_path.open("w") as f:
            for k, v in state["best"]["cfg"].items():
                f.write(f"export {k}={v}\n")
        log(f"winning env written to {env_path}")

    finally:
        restore_chain(backup)
        kill_chain()


if __name__ == "__main__":
    main()
