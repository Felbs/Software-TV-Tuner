#!/usr/bin/env python3
"""iq_sweep.py — drive tv_live_filesrc.py with many configs against a
captured IQ file, measure decode quality, produce a leaderboard.

The goal: when live RF varies hour-to-hour, lock down a SAVED IQ as the
test fixture and run many chain configurations against THE SAME bytes.
A config that produces decoded frames here is a real win, repeatable.

Usage:
    python3 tools/iq_sweep.py --iq /tmp/cap_rf34.cf32 --seconds 30

Reads grid of configs (equalizer, viterbi, RS, NB on/off, etc.) and
for each:
  1. Spawn tv_live_filesrc.py with that config, writing to a fresh TS
  2. Wait `--seconds` for processing
  3. ffmpeg decode test → count frames per program
  4. Record result

Then prints best configs sorted by total decoded frames.

Writes /tmp/iq_sweep_results.csv with one row per config.
"""
from __future__ import annotations
import argparse
import itertools
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

STVT = Path("/home/user/Software-TV-Tuner")
FILESRC = STVT / "tools" / "tv_live_filesrc.py"


def decode_frames(ts_path: Path, program: int, sec: int = 5) -> int:
    """Count ffmpeg 'frame=' batch lines from decoding `sec` of program."""
    if not ts_path.exists() or ts_path.stat().st_size < 10_000_000:
        return 0
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-fflags", "+genpts+igndts+discardcorrupt",
             "-err_detect", "ignore_err",
             "-f", "mpegts", "-i", str(ts_path),
             "-map", f"0:p:{program}:v",
             "-t", str(sec), "-f", "null", "-"],
            capture_output=True, text=True, timeout=sec + 8,
        )
        return r.stderr.count("frame=")
    except subprocess.TimeoutExpired:
        return -1


def run_one(iq_path: Path, env: dict, ts_path: Path, seconds: int) -> dict:
    """Spawn tv_live_filesrc.py, wait, kill, measure."""
    if ts_path.exists():
        ts_path.unlink()

    cmd = ["python3", str(FILESRC),
           "--iq", str(iq_path),
           "--out", str(ts_path),
           "--log", "/tmp/iq_sweep_chain.log"]

    proc_env = os.environ.copy()
    proc_env.update(env)

    proc = subprocess.Popen(
        cmd,
        cwd=str(STVT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=proc_env,
        start_new_session=True,
    )
    try:
        time.sleep(seconds)
    finally:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError: pass
        time.sleep(1.0)
        try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError: pass
        proc.wait(timeout=5)

    sz = ts_path.stat().st_size if ts_path.exists() else 0
    p3 = decode_frames(ts_path, 3)
    p4 = decode_frames(ts_path, 4)
    p5 = decode_frames(ts_path, 5)
    return {"ts_size": sz, "p3": p3, "p4": p4, "p5": p5,
            "total": max(0,p3)+max(0,p4)+max(0,p5)}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--iq", required=True, help="CF32 IQ file (from record_iq.py)")
    p.add_argument("--seconds", type=int, default=30, help="seconds per config")
    p.add_argument("--out-csv", default="/tmp/iq_sweep_results.csv")
    args = p.parse_args()

    iq_path = Path(args.iq)
    if not iq_path.exists():
        print(f"[iq_sweep] IQ file not found: {iq_path}", file=sys.stderr)
        return 1
    print(f"[iq_sweep] IQ: {iq_path} ({iq_path.stat().st_size/1e9:.1f} GB)")

    # Grid of configs to try. Start small — expand based on findings.
    grid = list(itertools.product(
        ["long", "pilot", "pilot_dd", "cma"],     # STVT_EQ
        ["hard", "soft"],                          # STVT_VITERBI
        ["stock", "erasure"],                      # STVT_RS
        ["0", "1"],                                # STVT_NB
    ))

    results = []
    ts_path = Path("/tmp/iq_sweep_out.ts")

    csv = open(args.out_csv, "w")
    csv.write("eq,viterbi,rs,nb,ts_size,p3,p4,p5,total\n")
    csv.flush()

    for i, (eq, vit, rs, nb) in enumerate(grid):
        env = {
            "STVT_EQ": eq,
            "STVT_VITERBI": vit,
            "STVT_RS": rs,
            "STVT_NB": nb,
        }
        if rs == "erasure":
            env["STVT_RS_ERASURES"] = "14"
        label = f"eq={eq} vit={vit} rs={rs} nb={nb}"
        print(f"[iq_sweep] {i+1}/{len(grid)}: {label}", flush=True)
        r = run_one(iq_path, env, ts_path, args.seconds)
        print(f"   → ts={r['ts_size']/1e6:.1f}MB p3={r['p3']} p4={r['p4']} "
              f"p5={r['p5']} total={r['total']}", flush=True)
        csv.write(f"{eq},{vit},{rs},{nb},{r['ts_size']},{r['p3']},{r['p4']},{r['p5']},{r['total']}\n")
        csv.flush()
        r["label"] = label
        results.append(r)

    csv.close()

    # Leaderboard.
    results.sort(key=lambda x: x["total"], reverse=True)
    print("")
    print("==== LEADERBOARD ====")
    for r in results[:10]:
        print(f"  total={r['total']:>3}  p3={r['p3']:>3}  p4={r['p4']:>3}  "
              f"p5={r['p5']:>3}  {r['label']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
