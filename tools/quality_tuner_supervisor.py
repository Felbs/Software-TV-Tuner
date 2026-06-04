"""Supervises quality_tuner_win.py — restarts it if it dies.

Why: the tuner orchestrator has died at least once with no error logged
(probably a Windows-level kill or memory-allocator stumble). This wrapper
keeps it alive by spawning it, waiting for the exit code, logging what
happened, and respawning until either:
  - we hit the restart cap (default 100), or
  - the user Ctrl-C's the supervisor itself.

The tuner is fully resumable, so a restart costs only the seconds since
the last successful run.

Usage:  python tools/quality_tuner_supervisor.py [--rf 34] [--seconds 60]

Output:
  - supervisor stdout/stderr is the tuner's own stream (line-buffered)
  - tools/data/quality_tuner_win/supervisor.log records every spawn,
    exit code, restart, and any anomaly.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TUNER = REPO / "tools" / "quality_tuner_win.py"
PY = sys.executable  # supervisor and tuner share the same interpreter
STATE_DIR = REPO / "tools" / "data" / "quality_tuner_win"
SUPERVISOR_LOG = STATE_DIR / "supervisor.log"
MAX_RESTARTS = 100
RESTART_BACKOFF_S = 5
RAPID_FAIL_WINDOW_S = 30   # if tuner dies within this many seconds, treat as rapid fail
RAPID_FAIL_THRESHOLD = 5   # after this many rapid fails in a row, give up


def log(msg: str):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(SUPERVISOR_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=34)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--max-restarts", type=int, default=MAX_RESTARTS)
    ap.add_argument("--start-fresh", action="store_true")
    args = ap.parse_args()

    log(f"=== supervisor starting (rf={args.rf} seconds={args.seconds}) ===")
    log(f"tuner: {TUNER}")

    cmd = [PY, "-u", str(TUNER), "--rf", str(args.rf),
           "--seconds", str(args.seconds)]
    if args.start_fresh:
        cmd.append("--start-fresh")

    stop = {"flag": False}

    def _sigint(sig, frame):
        if stop["flag"]:
            log("second Ctrl-C — hard exit")
            os._exit(1)
        stop["flag"] = True
        log("Ctrl-C received — will exit after current tuner exits")
    signal.signal(signal.SIGINT, _sigint)

    restarts = 0
    rapid_fails = 0
    while restarts < args.max_restarts and not stop["flag"]:
        log(f"spawn #{restarts + 1}: {' '.join(cmd)}")
        t0 = time.time()
        try:
            # Inherit stdout/stderr so the user sees tuner output live
            proc = subprocess.Popen(cmd)
        except OSError as e:
            log(f"spawn failed: {e}")
            break

        # Forward Ctrl-C to the tuner
        try:
            ret = proc.wait()
        except KeyboardInterrupt:
            log("Ctrl-C — asking tuner to stop")
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=30)
            except (subprocess.TimeoutExpired, OSError):
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            return

        elapsed = time.time() - t0
        log(f"tuner exited code={ret} after {elapsed:.1f}s")
        if stop["flag"]:
            log("stop flag set — supervisor exiting")
            return

        if elapsed < RAPID_FAIL_WINDOW_S:
            rapid_fails += 1
            log(f"rapid-fail {rapid_fails}/{RAPID_FAIL_THRESHOLD}")
            if rapid_fails >= RAPID_FAIL_THRESHOLD:
                log("too many rapid fails — exiting")
                return
        else:
            rapid_fails = 0   # ran long enough to be "real" work

        restarts += 1
        log(f"restart in {RESTART_BACKOFF_S}s (restarts so far: {restarts})")
        for _ in range(RESTART_BACKOFF_S):
            if stop["flag"]:
                return
            time.sleep(1)

    log(f"=== supervisor exiting (restarts={restarts}, max={args.max_restarts}) ===")


if __name__ == "__main__":
    main()
