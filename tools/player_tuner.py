#!/usr/bin/env python3
"""player_tuner.py — autonomous tuner for tv_player.py knobs.

Assumes the chain is already running (tv_live writing live.ts). Runs
tv_player.py --headless with various param combos, parses its stderr
stats, scores each combo, hill-climbs to the best.

Usage:
    # Terminal 1
    ~/run_stvt_winner.sh

    # Terminal 2
    python3 tools/player_tuner.py [--cell-seconds 60] [--budget 60]
"""
from __future__ import annotations
import argparse
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

STVT = Path(os.environ.get("STVT_HOME", "/home/user/Software-TV-Tuner"))
LIVE_TS = STVT / "tools/data/tv_live/live.ts"
PLAYER = STVT / "tools/tv_player.py"

STATE_PATH = Path("/tmp/player_tuner_state.json")
LOG_PATH = Path("/tmp/player_tuner.log")
LEADERBOARD_PATH = Path("/tmp/player_tuner_leaderboard.txt")

# tv_player.py CLI knobs we'll sweep. Values chosen to bracket the
# default and probe both more-aggressive and more-conservative settings.
KNOBS = {
    "video-buf":  [60, 120, 240, 480, 960],   # frames of video ring
    "audio-buf":  [100, 200, 400, 800],       # chunks of audio ring
    "stale-ms":   [100, 500, 2000, 5000],     # ms before frame "stale"
    "dry-ms":     [500, 1500, 5000, 15000],   # ms before decoder "dry"
    "fps":        [24, 30, 60],               # display fps
    "interp":     [False, True],              # interpolate stale frames
}

DEFAULT_CONFIG = {
    "video-buf": 240,
    "audio-buf": 200,
    "stale-ms":  100,
    "dry-ms":    1500,
    "fps":       30,
    "interp":    False,
}

def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


# Parse stat lines like:
# [t= 10.0s] st=PLAYING   v= 240 a= 480 vbuf= 30 abuf=120 age=  16ms idle=  10ms resp=0 under=2 vbytes=12,345,678
STAT_RE = re.compile(
    r"\[t=\s*([\d.]+)s\]\s+st=(\S+)\s+v=\s*(\d+)\s+a=\s*(\d+)\s+"
    r"vbuf=\s*(\d+)\s+abuf=\s*(\d+)\s+age=\s*(\d+)ms\s+idle=\s*(\d+)ms\s+"
    r"resp=(\d+)\s+under=(\d+)"
)


def run_cell(cfg: dict, seconds: int) -> dict:
    """Run tv_player.py headless against `tail -F live.ts` for `seconds`
    seconds, parse stderr stats, return metrics."""
    if not LIVE_TS.exists() or LIVE_TS.stat().st_size < 1_000_000:
        return {"error": "live.ts missing or empty — is the chain running?"}

    # Build the command. tv_player.py reads stdin when source='-'.
    player_args = [
        "python3", str(PLAYER), "-",
        "--headless",
        "--max-seconds", str(seconds + 2),
        "--video-buf",   str(cfg["video-buf"]),
        "--audio-buf",   str(cfg["audio-buf"]),
        "--stale-ms",    str(cfg["stale-ms"]),
        "--dry-ms",      str(cfg["dry-ms"]),
        "--fps",         str(cfg["fps"]),
    ]
    if cfg["interp"]:
        player_args.append("--interp")

    # Use a shell pipeline: tail -F live.ts | python3 tv_player.py ...
    cmd = f"tail -F {LIVE_TS} | " + " ".join(player_args)
    proc = subprocess.Popen(
        ["bash", "-c", cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    # Spawn a watchdog thread that kills the subprocess after the budget,
    # so a hung tv_player.py can't make us wait forever.
    import threading
    def killer():
        time.sleep(seconds + 15)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            time.sleep(1)
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    t = threading.Thread(target=killer, daemon=True)
    t.start()

    stats = []
    raw_lines = []
    try:
        for line in proc.stdout:
            raw_lines.append(line.rstrip())
            m = STAT_RE.search(line)
            if m:
                t_, status, v, a, vbuf, abuf, age, idle, resp, under = m.groups()
                stats.append({
                    "t": float(t_), "status": status,
                    "v": int(v), "a": int(a),
                    "vbuf": int(vbuf), "abuf": int(abuf),
                    "age_ms": int(age), "idle_ms": int(idle),
                    "resp": int(resp), "under": int(under),
                })
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            time.sleep(0.5)
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass

    if len(stats) < 5:
        # Dump last 20 lines of subprocess output so we can see WHY it
        # produced no stats (cv2 import error, ffmpeg crash, etc.).
        tail = "\n  ".join(raw_lines[-20:]) if raw_lines else "(no output)"
        return {"error": f"only got {len(stats)} stat lines. subprocess tail:\n  {tail}"}

    last = stats[-1]
    # Count seconds where age_ms > 500 (frame stale → user sees freeze)
    freeze_secs = sum(1 for s in stats if s["age_ms"] > 500)
    total_secs = len(stats)
    return {
        "frames_decoded":  last["v"],
        "audio_decoded":   last["a"],
        "ffmpeg_respawns": last["resp"],
        "audio_underruns": last["under"],
        "freeze_seconds":  freeze_secs,
        "total_seconds":   total_secs,
        "uptime_pct":      100.0 * (total_secs - freeze_secs) / max(1, total_secs),
        "final_status":    last["status"],
    }


def compute_score(m: dict) -> float:
    """Reward continuous playback (low freeze fraction), penalize ffmpeg
    respawns and audio underruns."""
    if "error" in m:
        return -1e6
    return (m["frames_decoded"]
            -  20 * m["ffmpeg_respawns"]
            -   5 * m["freeze_seconds"]
            - 0.1 * m["audio_underruns"])


def fmt_metrics(m: dict) -> str:
    if "error" in m:
        return f"ERROR: {m['error']}"
    return (f"frames={m['frames_decoded']:>5} "
            f"uptime={m['uptime_pct']:>5.1f}%  "
            f"freezes={m['freeze_seconds']:>3}s  "
            f"resp={m['ffmpeg_respawns']:>2}  "
            f"under={m['audio_underruns']:>3}  "
            f"status={m['final_status']}")


def cfg_key(cfg: dict) -> str:
    return json.dumps(cfg, sort_keys=True)


def perturb(cfg: dict, knob: str, history: set[str]) -> dict | None:
    cur = cfg[knob]
    for v in KNOBS[knob]:
        if v == cur: continue
        new = dict(cfg); new[knob] = v
        if cfg_key(new) not in history:
            return new
    return None


def write_leaderboard(history: list, baseline: dict) -> None:
    ordered = sorted(history, key=lambda r: compute_score(r["metrics"]),
                     reverse=True)
    diff_keys = sorted({k for h in history for k in h["cfg"]})
    varying = [k for k in diff_keys
               if len({json.dumps(h["cfg"].get(k)) for h in history}) > 1]
    lines = [f"PLAYER TUNER LEADERBOARD  ({len(history)} configs explored)",
             f"baseline:  {fmt_metrics(baseline['metrics'])}", "",
             f"{'#':>3}  score    metrics                                                                       diff vs baseline",
             "-" * 200]
    base_cfg = baseline["cfg"]
    for i, h in enumerate(ordered[:20]):
        s = compute_score(h["metrics"])
        diff = {k: h["cfg"].get(k) for k in varying
                if h["cfg"].get(k) != base_cfg.get(k)}
        diff_str = ", ".join(f"{k}={v}" for k, v in diff.items()) or "(baseline)"
        lines.append(f"{i+1:>3}  {s:>7.0f}  {fmt_metrics(h['metrics']):<90}  {diff_str}")
    LEADERBOARD_PATH.write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell-seconds", type=int, default=60)
    ap.add_argument("--budget", type=int, default=60, help="minutes")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    LOG_PATH.write_text("")
    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()

    history = []
    if STATE_PATH.exists() and not args.reset:
        try:
            history = json.loads(STATE_PATH.read_text())
        except Exception:
            history = []

    log(f"player_tuner starting. live.ts at {LIVE_TS}")
    log(f"cell_seconds={args.cell_seconds}, budget={args.budget} min")

    # Baseline
    if not history:
        log(f"baseline with {DEFAULT_CONFIG}")
        m = run_cell(DEFAULT_CONFIG, args.cell_seconds)
        history.append({"cfg": DEFAULT_CONFIG, "metrics": m,
                        "score": compute_score(m)})
        STATE_PATH.write_text(json.dumps(history, indent=2))
        log(f"baseline -> {fmt_metrics(m)}  score={compute_score(m):.0f}")

    baseline = history[0]
    best = max(history, key=lambda h: h.get("score", -1e6))
    seen = {cfg_key(h["cfg"]) for h in history}
    knob_idx = 0
    knob_list = list(KNOBS.keys())
    cycles_no_improve = 0
    deadline = time.time() + args.budget * 60

    while time.time() < deadline:
        knob = knob_list[knob_idx % len(knob_list)]
        new_cfg = perturb(best["cfg"], knob, seen)
        if new_cfg is None:
            knob_idx += 1
            if knob_idx % len(knob_list) == 0:
                cycles_no_improve += 1
                if cycles_no_improve >= 2:
                    log("2 full passes no improvement — stopping")
                    break
                log(f"pass done. best score={best.get('score', -1):.0f}")
            continue

        log(f"try {knob}={new_cfg[knob]!r}  (best score={best.get('score', -1):.0f})")
        m = run_cell(new_cfg, args.cell_seconds)
        score = compute_score(m)
        entry = {"cfg": new_cfg, "metrics": m, "score": score}
        history.append(entry)
        seen.add(cfg_key(new_cfg))
        log(f"  -> {fmt_metrics(m)}  score={score:.0f}")
        if score > best.get("score", -1e6):
            best = entry
            cycles_no_improve = 0
            log(f"  NEW BEST: {new_cfg}")
        STATE_PATH.write_text(json.dumps(history, indent=2))
        write_leaderboard(history, baseline)

    log("=" * 60)
    log("DONE")
    log(f"baseline: {fmt_metrics(baseline['metrics'])}")
    log(f"best:     {fmt_metrics(best['metrics'])}")
    log(f"best cfg: {best['cfg']}")
    write_leaderboard(history, baseline)
    Path("/tmp/player_tuner_best.json").write_text(json.dumps(best, indent=2))


if __name__ == "__main__":
    main()
