"""Closed-caption verification agent.

Reads CEA-608 captions DIRECTLY from the broadcast stream (live.ts)
in real time. This is the GROUND TRUTH of what the broadcaster
transmitted — independent of what VLC chooses to render. Comparing
to VLC tells you whether VLC is in sync.

What it does
============
1. Tails live.ts (last N MB — skips the equalizer-convergence burst).
2. Runs the bundled CEA-608 decoder (atsc_cc.py) on the stream.
3. Adds wall-clock timestamps to every caption line.
4. Watches for "stuck" captions — lines that don't change for >N
   seconds. That's the "frozen text on screen" symptom you've seen.
5. Logs everything to a transcript file for later review.

Usage:
    python tools/stvt_cc_verify.py [--channel 1|2]
                                   [--ts <path>]
                                   [--stale-warn 8.0]
                                   [--log <path>]

Output to console (and --log file):
    [16:42:31.45  +12.3s] Welcome to NewsCenter 4.
    [16:42:33.12  +14.0s] I'm Doreen Gentzler.
    [16:42:39.88  STALE]  caption unchanged 8.7s: 'I'm Doreen Gentzler.'

Run alongside VLC. If VLC's overlay text matches and updates at the
same time as this script's output, captions are good. If VLC lags or
shows frozen text while this script keeps moving, VLC is the problem.
If THIS script reports STALE while VLC also looks frozen, the chain
is dropping CC control codes upstream of VLC.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Thread, Event, Lock

REPO = Path(__file__).resolve().parents[1]
LIVE_TS_DEFAULT = REPO / "tools" / "data" / "tv_live" / "live.ts"
ATSC_CC_PY = Path(__file__).resolve().parent / "atsc_cc.py"


def tail_writer(path: Path, sink, stop: Event, tail_bytes: int):
    """Open path, seek to end-tail_bytes, write bytes to sink as the
    file grows. Equivalent of `tail -c N -F`."""
    while not stop.is_set():
        if path.exists() and path.stat().st_size >= tail_bytes:
            break
        time.sleep(0.5)
    if stop.is_set():
        return
    try:
        with open(path, "rb") as f:
            f.seek(-tail_bytes, os.SEEK_END)
            while not stop.is_set():
                chunk = f.read(64 * 1024)
                if chunk:
                    try:
                        sink.write(chunk)
                        sink.flush()
                    except (BrokenPipeError, OSError):
                        return
                else:
                    time.sleep(0.05)
    finally:
        try:
            sink.close()
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--channel", type=int, default=1, choices=[1, 2],
                    help="CEA-608 channel: 1=CC1 (EN), 2=CC2 (ES SAP)")
    ap.add_argument("--ts", type=Path, default=LIVE_TS_DEFAULT,
                    help="Path to live.ts")
    ap.add_argument("--tail-mb", type=int, default=25,
                    help="Read from last N MB (skip convergence burst)")
    ap.add_argument("--stale-warn", type=float, default=8.0,
                    help="Warn when caption text hasn't changed for N seconds")
    ap.add_argument("--log", type=Path, default=None,
                    help="Tee transcript to this file too")
    args = ap.parse_args()

    if not ATSC_CC_PY.exists():
        print(f"[verify] atsc_cc.py not found at {ATSC_CC_PY}", file=sys.stderr)
        return 1
    if not args.ts.exists():
        print(f"[verify] live.ts not found at {args.ts}", file=sys.stderr)
        print("[verify] start the chain first: "
              "python tools/tv_tuner.py --rf <N> --player vlc", file=sys.stderr)
        return 1

    log_fh = None
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(args.log, "w", encoding="utf-8")

    def out(line: str):
        print(line, flush=True)
        if log_fh:
            log_fh.write(line + "\n")
            log_fh.flush()

    out(f"[verify] reading captions from {args.ts}")
    out(f"[verify] CEA-608 channel {args.channel} "
        f"({'EN/primary' if args.channel == 1 else 'ES/SAP'})")
    out(f"[verify] stale threshold: {args.stale_warn}s")
    out(f"[verify] wall-clock timestamps — compare to the VLC window directly")
    out("")

    # atsc_cc.py already seeks to the live edge (last ~256 KB) on its
    # own — just pass the path. No need to pipe bytes ourselves.
    cc_proc = subprocess.Popen(
        [sys.executable, "-u", str(ATSC_CC_PY),
         "--channel", str(args.channel), str(args.ts)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=1,  # line-buffered
    )

    stop = Event()

    # Stale-caption tracker.
    state_lock = Lock()
    state = {"text": "", "last_change": time.time(), "warned": ""}

    def saw(text: str):
        text = text.strip()
        if not text:
            return
        with state_lock:
            if text != state["text"]:
                state["text"] = text
                state["last_change"] = time.time()
                state["warned"] = ""

    def stale_loop():
        while not stop.is_set():
            time.sleep(2)
            with state_lock:
                age = time.time() - state["last_change"]
                if (state["text"]
                        and age >= args.stale_warn
                        and state["warned"] != state["text"]):
                    state["warned"] = state["text"]
                    ts = time.strftime("%H:%M:%S")
                    out(f"[{ts}  STALE]  caption unchanged "
                        f"{age:.1f}s: {state['text']!r}")
    Thread(target=stale_loop, daemon=True).start()

    start = time.time()
    line_buf = ""

    def flush_line():
        nonlocal line_buf
        text = line_buf.strip()
        line_buf = ""
        if not text:
            return
        now = time.time()
        wall = time.strftime("%H:%M:%S") + f".{int((now*1000) % 1000):03d}"
        elapsed = now - start
        out(f"[{wall}  +{elapsed:6.1f}s] {text}")
        saw(text)

    try:
        # Roll-up CCs come through as a character stream without clean
        # newlines, but the bundled decoder DOES emit '\n' on EOC and on
        # explicit CR. Treat any line break as a caption boundary. Read
        # one byte at a time for prompt output (atsc_cc flushes after
        # every char).
        while not stop.is_set():
            byte = cc_proc.stdout.read(1)
            if not byte:
                if cc_proc.poll() is not None:
                    break
                continue
            ch = byte.decode("utf-8", errors="replace")
            if ch in ("\n", "\r"):
                flush_line()
            else:
                line_buf += ch
                # Safety: force flush if a line gets unreasonably long
                # (no newline received — broadcaster oddity).
                if len(line_buf) >= 200:
                    flush_line()
        flush_line()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        try:
            cc_proc.terminate()
            cc_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            cc_proc.kill()
        if log_fh:
            log_fh.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
