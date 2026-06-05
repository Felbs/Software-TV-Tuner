#!/usr/bin/env python3
"""psi_repair_tuner.py — autonomous tuner for ts_psi_repair.py.

Same architecture as player_tuner.py but inserts ts_psi_repair.py into
the pipeline and sweeps both repair-filter env vars AND the player's
knobs that interact with PSI delivery.

Pipeline tested per cell:
    tail -F live.ts | ts_psi_repair.py | tv_player.py --headless

Requires the chain to be running in another terminal.

Usage:
    python3 tools/psi_repair_tuner.py [--cell-seconds 60] [--reset]
"""
from __future__ import annotations
import argparse
import json
import os
import re
import signal
import subprocess
import time
import threading
from pathlib import Path

STVT       = Path(os.environ.get("STVT_HOME", "/home/user/Software-TV-Tuner"))
LIVE_TS    = STVT / "tools/data/tv_live/live.ts"
PSI_REPAIR = STVT / "tools/ts_psi_repair.py"
PLAYER     = STVT / "tools/tv_player.py"

STATE_PATH       = Path("/tmp/psi_repair_tuner_state.json")
LOG_PATH         = Path("/tmp/psi_repair_tuner.log")
LEADERBOARD_PATH = Path("/tmp/psi_repair_tuner_leaderboard.txt")

# Knobs for the repair filter (env vars) plus a couple of player knobs
# that interact with PSI delivery latency.
KNOBS = {
    # repair filter
    "TS_PSI_MISS_PKTS":   ["500", "1500", "5000", "15000"],
    "TS_PSI_CHECK_PKTS":  ["50", "200", "1000"],
    "TS_PSI_MODE":        ["miss", "always"],
    "TS_PSI_WARMUP":      ["0", "1"],
    "TS_PSI_GROUPED":     ["0", "1"],
    # player (kept narrow; player_tuner already explored these)
    "PLAYER_VIDEO_BUF":   ["60", "120", "240"],
    "PLAYER_FPS":         ["24", "30"],
}

DEFAULT_CONFIG = {
    "TS_PSI_MISS_PKTS":  "1500",
    "TS_PSI_CHECK_PKTS": "200",
    "TS_PSI_MODE":       "miss",
    "TS_PSI_WARMUP":     "0",
    "TS_PSI_GROUPED":    "1",
    "PLAYER_VIDEO_BUF":  "120",
    "PLAYER_FPS":        "24",
}


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


STAT_RE = re.compile(
    r"\[t=\s*([\d.]+)s\]\s+st=(\S+)\s+v=\s*(\d+)\s+a=\s*(\d+)\s+"
    r"vbuf=\s*(\d+)\s+abuf=\s*(\d+)\s+age=\s*(\d+)ms\s+idle=\s*(\d+)ms\s+"
    r"resp=(\d+)\s+under=(\d+)"
)
# Also capture stats from the repair filter for diagnostics.
PSI_STAT_RE = re.compile(
    r"\[psi_repair\] t=\s*([\d.]+)s\s+in=([\d.]+)MB.*"
    r"pat_inj=(\d+)\s+pmt_inj=(\d+)"
)


def run_cell(cfg: dict, seconds: int) -> dict:
    if not LIVE_TS.exists() or LIVE_TS.stat().st_size < 1_000_000:
        return {"error": "live.ts missing or empty — start ~/run_stvt_winner.sh first"}

    # Build the bash pipeline. PSI env vars are exported in the bash subshell.
    env_exports = " ".join(
        f"{k}={cfg[k]}" for k in KNOBS if k.startswith("TS_PSI_")
    )
    cmd = (
        f"{env_exports} tail -F {LIVE_TS} "
        f"| python3 {PSI_REPAIR} "
        f"| python3 {PLAYER} - --headless "
        f"--max-seconds {seconds + 2} "
        f"--video-buf {cfg['PLAYER_VIDEO_BUF']} "
        f"--audio-buf 800 "
        f"--stale-ms 100 --dry-ms 1500 "
        f"--fps {cfg['PLAYER_FPS']}"
    )
    proc = subprocess.Popen(
        ["bash", "-c", cmd],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )

    # Watchdog: kill on timeout.
    def killer():
        time.sleep(seconds + 15)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            time.sleep(1)
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    threading.Thread(target=killer, daemon=True).start()

    stats = []
    psi_stats = []
    raw = []
    try:
        for line in proc.stdout:
            raw.append(line.rstrip())
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
            pm = PSI_STAT_RE.search(line)
            if pm:
                _, _, pat_inj, pmt_inj = pm.groups()
                psi_stats.append({"pat_inj": int(pat_inj),
                                  "pmt_inj": int(pmt_inj)})
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            time.sleep(0.5)
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass

    if len(stats) < 5:
        tail = "\n  ".join(raw[-25:]) if raw else "(no output)"
        return {"error": f"only got {len(stats)} stat lines. tail:\n  {tail}"}

    last = stats[-1]
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
        "pat_inj":         psi_stats[-1]["pat_inj"] if psi_stats else 0,
        "pmt_inj":         psi_stats[-1]["pmt_inj"] if psi_stats else 0,
    }


def compute_score(m: dict) -> float:
    if "error" in m:
        return -1e6
    return (m["frames_decoded"]
            -  20 * m["ffmpeg_respawns"]
            -   5 * m["freeze_seconds"]
            - 0.1 * m["audio_underruns"])


def fmt_metrics(m: dict) -> str:
    if "error" in m:
        return f"ERROR: {m['error'][:80]}"
    return (f"frames={m['frames_decoded']:>5}  "
            f"uptime={m['uptime_pct']:>5.1f}%  "
            f"freezes={m['freeze_seconds']:>3}s  "
            f"resp={m['ffmpeg_respawns']}  "
            f"under={m['audio_underruns']:>4}  "
            f"pat_inj={m.get('pat_inj', 0):>3}  "
            f"pmt_inj={m.get('pmt_inj', 0):>3}  "
            f"status={m['final_status']}")


def cfg_key(cfg): return json.dumps(cfg, sort_keys=True)


def perturb(cfg, knob, history):
    cur = cfg[knob]
    for v in KNOBS[knob]:
        if v == cur: continue
        new = dict(cfg); new[knob] = v
        if cfg_key(new) not in history: return new
    return None


def write_leaderboard(history, baseline):
    ordered = sorted(history, key=lambda r: compute_score(r["metrics"]), reverse=True)
    diff_keys = sorted({k for h in history for k in h["cfg"]})
    varying = [k for k in diff_keys
               if len({h["cfg"].get(k) for h in history}) > 1]
    lines = [f"PSI REPAIR LEADERBOARD  ({len(history)} configs)",
             f"baseline:  {fmt_metrics(baseline['metrics'])}", "",
             f"{'#':>3}  score    metrics                                                                                       diff vs baseline",
             "-" * 220]
    base = baseline["cfg"]
    for i, h in enumerate(ordered[:20]):
        s = compute_score(h["metrics"])
        diff = {k: h["cfg"].get(k) for k in varying
                if h["cfg"].get(k) != base.get(k)}
        diff_str = ", ".join(f"{k}={v}" for k, v in diff.items()) or "(baseline)"
        lines.append(f"{i+1:>3}  {s:>7.0f}  {fmt_metrics(h['metrics']):<110}  {diff_str}")
    LEADERBOARD_PATH.write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell-seconds", type=int, default=60)
    ap.add_argument("--budget", type=int, default=60)
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    LOG_PATH.write_text("")
    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()

    history = []
    if STATE_PATH.exists() and not args.reset:
        try: history = json.loads(STATE_PATH.read_text())
        except Exception: history = []

    log(f"psi_repair_tuner starting. live.ts at {LIVE_TS}")
    log(f"cell={args.cell_seconds}s, budget={args.budget}min")

    if not history:
        log(f"baseline {DEFAULT_CONFIG}")
        m = run_cell(DEFAULT_CONFIG, args.cell_seconds)
        history.append({"cfg": DEFAULT_CONFIG, "metrics": m,
                        "score": compute_score(m)})
        STATE_PATH.write_text(json.dumps(history, indent=2))
        log(f"baseline -> {fmt_metrics(m)} score={compute_score(m):.0f}")

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
                log(f"pass done. best={best.get('score', -1):.0f}")
            continue

        log(f"try {knob}={new_cfg[knob]!r} (best={best.get('score', -1):.0f})")
        m = run_cell(new_cfg, args.cell_seconds)
        score = compute_score(m)
        entry = {"cfg": new_cfg, "metrics": m, "score": score}
        history.append(entry)
        seen.add(cfg_key(new_cfg))
        log(f"  -> {fmt_metrics(m)} score={score:.0f}")
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
    Path("/tmp/psi_repair_best.json").write_text(json.dumps(best, indent=2))


if __name__ == "__main__":
    main()
