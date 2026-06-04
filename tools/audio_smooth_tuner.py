#!/usr/bin/env python3
"""audio_smooth_tuner.py — overnight tuner for smooth audio/video playback.

Runs tv_player.py headless against the *currently-running* chain's live.ts,
measuring frames decoded + audio underruns + ffmpeg respawns. Sweeps player
and ts_psi_repair env knobs via coordinate descent. Saves the best config
to /tmp/audio_smooth_winner.env so the user can `source` it before launching
the real player.

Loops forever (or until --max-iters): after one full coordinate-descent
pass, re-runs from the current best with fresh trials, so RF drift over
the night doesn't lock us into an old winner.

Usage:
    nohup python3 tools/audio_smooth_tuner.py \\
        > /tmp/audio_smooth_tuner_stdout.log 2>&1 & disown
    tail -f /tmp/audio_smooth_tuner.log
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TS   = REPO / "tools" / "data" / "tv_live" / "live.ts"
PLAYER = REPO / "tools" / "tv_player.py"
PSI    = REPO / "tools" / "ts_psi_repair.py"

LOG_PATH    = Path("/tmp/audio_smooth_tuner.log")
WINNER_PATH = Path("/tmp/audio_smooth_winner.env")
STATE_PATH  = Path("/tmp/audio_smooth_state.json")
LOCK_PATH   = Path("/tmp/audio_smooth_tuner.lock")

# Trial timing.
TRIAL_SEC     = 60          # how long each player run lasts
BETWEEN_SEC   = 3           # cooldown between trials so file handles release

# Coordinate-descent sweep grids. Order = priority — most-likely-impactful first.
# Each entry: (env_key_or_arg, kind, values, default)
#   kind = "env" → goes into env (TS_PSI_*)
#   kind = "arg" → goes into tv_player.py args (--audio-buf, etc.)
SWEEPS = [
    ("--audio-buf",      "arg", [800, 1600, 2400, 3200, 4800, 6400], 800),
    ("TS_PSI_MISS_PKTS", "env", [2000, 5000, 10000, 15000, 25000],   15000),
    ("TS_PSI_CHECK_PKTS","env", [200, 500, 1000, 2000],               1000),
    ("--dry-ms",         "arg", [500, 1500, 3000, 5000, 10000],       1500),
    ("--video-buf",      "arg", [60, 120, 240, 480, 720],             120),
    ("--stale-ms",       "arg", [50, 100, 200, 500],                  100),
]


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def acquire_lock() -> None:
    if LOCK_PATH.exists():
        try:
            pid = int(LOCK_PATH.read_text().strip())
            os.kill(pid, 0)
            log(f"another instance running (PID {pid}) — exiting")
            sys.exit(1)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    LOCK_PATH.write_text(str(os.getpid()))


def release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


def chain_alive() -> bool:
    """Check that the chain is producing TS bytes."""
    if not TS.exists():
        return False
    sz1 = TS.stat().st_size
    time.sleep(2)
    if not TS.exists():
        return False
    sz2 = TS.stat().st_size
    return (sz2 - sz1) > 100_000   # >50KB/s


def run_trial(args_dict: dict, env_dict: dict) -> dict:
    """Run tv_player.py headless against live.ts for TRIAL_SEC seconds.
    Returns dict with metrics: frames, audio_chunks, underruns, respawns, score.
    """
    env = os.environ.copy()
    env["TS_PSI_MODE"]     = "miss"
    env["TS_PSI_WARMUP"]   = "0"
    env["TS_PSI_GROUPED"]  = "1"
    for k, v in env_dict.items():
        env[k] = str(v)

    player_args = [
        "python3", str(PLAYER), "-",
        "--headless",
        "--max-seconds", str(TRIAL_SEC + 5),
        "--fps", "30",
    ]
    for k, v in args_dict.items():
        player_args += [k, str(v)]

    # tail -F -c 0 → start at live position
    pipe_cmd = (
        f"tail -F -c 0 {shlex.quote(str(TS))} "
        f"| python3 {shlex.quote(str(PSI))} "
        f"| {' '.join(shlex.quote(a) for a in player_args)}"
    )

    start = time.time()
    proc = subprocess.Popen(
        ["bash", "-c", pipe_cmd],
        env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )

    last_stats = {"v": 0, "a": 0, "under": 0, "resp": 0}
    try:
        deadline = start + TRIAL_SEC + 10
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            # Match: [t=  10.0s] st=RUNNING   v=  300 a=  500 ... under=2 ...
            m = re.search(
                r"v=\s*(\d+)\s+a=\s*(\d+).*resp=(\d+)\s+under=(\d+)",
                line)
            if m:
                last_stats = {
                    "v":     int(m.group(1)),
                    "a":     int(m.group(2)),
                    "resp":  int(m.group(3)),
                    "under": int(m.group(4)),
                }
            if time.time() > deadline:
                break
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()

    # Also kill stray ffmpeg/tail processes from this trial.
    subprocess.run(["pkill", "-9", "-f", "tv_player.py.*--headless"],
                   stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "-f", "ts_psi_repair.py"],
                   stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

    frames    = last_stats["v"]
    audio     = last_stats["a"]
    underruns = last_stats["under"]
    respawns  = last_stats["resp"]

    # Quality score. Frames is the primary signal (more is better).
    # Audio underruns cost frames-equivalent. ffmpeg respawns are
    # catastrophic (mid-trial reset). Audio chunks contribute mildly so
    # an all-frozen run with no audio scores < a slightly skippy run.
    score = frames + 0.5 * audio - 5 * underruns - 100 * respawns

    return {
        "frames": frames, "audio": audio,
        "underruns": underruns, "respawns": respawns,
        "score": round(score, 1),
    }


def fmt_combo(args_dict: dict, env_dict: dict) -> str:
    parts = []
    for k, v in args_dict.items():
        parts.append(f"{k}={v}")
    for k, v in env_dict.items():
        parts.append(f"{k}={v}")
    return " ".join(parts)


def save_winner(args_dict: dict, env_dict: dict, score: float) -> None:
    lines = [
        f"# audio_smooth_tuner winner — score={score} "
        f"saved {time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    for k, v in env_dict.items():
        lines.append(f"export {k}={v}")
    # Player args go in a single string the launcher can read.
    arg_str = " ".join(f'{k} {v}' for k, v in args_dict.items())
    lines.append(f'export STVT_PLAYER_ARGS="{arg_str}"')
    WINNER_PATH.write_text("\n".join(lines) + "\n")


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def load_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return None


def coord_descent_pass(args: dict, env: dict, iteration: int) -> tuple[dict, dict, float]:
    """One full pass: for each sweep param, try every value with all others
    held at current best, take the winner."""
    best_score = float("-inf")
    best_args, best_env = dict(args), dict(env)

    for sweep_idx, (key, kind, values, _default) in enumerate(SWEEPS):
        log(f"  iter={iteration} sweep {sweep_idx+1}/{len(SWEEPS)}: "
            f"{key} over {values}")
        round_best = None
        for v in values:
            if not chain_alive():
                log("  chain dead — waiting 30s for auto_play_forever to recover")
                time.sleep(30)
                if not chain_alive():
                    log("  chain still dead — pausing tuning")
                    return best_args, best_env, best_score

            trial_args = dict(best_args)
            trial_env  = dict(best_env)
            if kind == "arg":
                trial_args[key] = v
            else:
                trial_env[key]  = v

            result = run_trial(trial_args, trial_env)
            log(f"    {key}={v:>6}  → frames={result['frames']:4d} "
                f"audio={result['audio']:4d} under={result['underruns']:3d} "
                f"resp={result['respawns']} score={result['score']:.1f}")

            if round_best is None or result["score"] > round_best["score"]:
                round_best = {**result, "value": v}
            time.sleep(BETWEEN_SEC)

        if round_best is None:
            continue
        # Adopt this round's winner.
        if kind == "arg":
            best_args[key] = round_best["value"]
        else:
            best_env[key]  = round_best["value"]
        best_score = round_best["score"]
        log(f"  → pick {key}={round_best['value']} (score={best_score})")
        save_winner(best_args, best_env, best_score)
        save_state({
            "iteration": iteration,
            "best_args": best_args,
            "best_env": best_env,
            "best_score": best_score,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    return best_args, best_env, best_score


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-iters", type=int, default=99,
                    help="iterations of coord-descent; default 99")
    ap.add_argument("--reset", action="store_true",
                    help="start from defaults, not saved state")
    args = ap.parse_args()

    LOG_PATH.write_text("") if args.reset else None
    acquire_lock()
    try:
        log(f"audio_smooth_tuner starting. trial={TRIAL_SEC}s. "
            f"sweeps={[s[0] for s in SWEEPS]}")
        log(f"chain check: live.ts={TS}")
        if not chain_alive():
            log("ERROR: chain not producing TS. Start ~/auto_play_forever.sh "
                "or ~/run_stvt_winner.sh first.")
            return 2

        state = None if args.reset else load_state()
        if state:
            best_args  = state["best_args"]
            best_env   = state["best_env"]
            best_score = state["best_score"]
            start_iter = state["iteration"] + 1
            log(f"resumed from iter={state['iteration']} "
                f"score={best_score} combo={fmt_combo(best_args, best_env)}")
        else:
            # Start from current run_stvt_player.sh defaults.
            best_args = {
                "--audio-buf": 800,
                "--video-buf": 120,
                "--stale-ms":  100,
                "--dry-ms":    1500,
            }
            best_env = {
                "TS_PSI_MISS_PKTS":  15000,
                "TS_PSI_CHECK_PKTS": 1000,
            }
            best_score = float("-inf")
            start_iter = 1

        for iteration in range(start_iter, args.max_iters + 1):
            log(f"=== iteration {iteration} ===")
            best_args, best_env, best_score = coord_descent_pass(
                best_args, best_env, iteration)
            log(f"=== iter {iteration} done. best_score={best_score} "
                f"combo={fmt_combo(best_args, best_env)} ===")
            log(f"winner saved to {WINNER_PATH}")

        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
