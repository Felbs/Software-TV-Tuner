"""quality_judge.py — Windows port of the Linux quality_judge.sh apparatus.

Measures video + audio quality of the chain's live.ts and emits a 0-100 score
+ tier, so the bot can read decode quality OBJECTIVELY instead of asking the
user "is it glitchy?". Measures the CHAIN output directly (snapshot of the tail,
decode the program's video AND audio), so a high score here + a glitchy picture
means the PLAYER is the culprit, not the chain.

Method (faithful to quality_judge.sh):
  1. snapshot the last ~WINDOW*2 s of live.ts (shared read; chain holds it open)
  2. ffmpeg null-decode WINDOW s of the program's video+audio
  3. count decoded frames -> fps; count video decode errors; count audio errors
  4. score = fps*3, penalized by error rates; tier cable/watchable/glitchy/broken

Score tiers:  cable_quality 90+ | watchable 60-89 | glitchy 30-59 | broken <30

Usage:
    python quality_judge.py                 # one-shot, program 3, 15s window
    python quality_judge.py --program 3 --window 20
    python quality_judge.py --loop          # sample every 30s forever
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

LIVE_TS = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
STATE = Path(os.environ["TEMP"]) / "quality_state.json"
FFMPEG = "ffmpeg"
TS_RATE = 2_400_000   # bytes/s, for window -> bytes

VID_ERR_RE = re.compile(
    r"ac-tex damaged|MVs not available|mb incr damaged|Invalid mb type|"
    r"end mismatch|skipped MB|motion_type|concealing|corrupt", re.I)
AUD_ERR_RE = re.compile(
    r"(ac3|ac3_fixed).*error|error submitting.*ac3|exponent.*out-of-range|"
    r"coupling|incomplete frame", re.I)
FRAME_RE = re.compile(r"frame=\s*(\d+)")


def snapshot_tail(window):
    """Copy the last ~window*2.5 s of live.ts to a temp file using shared read."""
    want = int(window * 2.5 * TS_RATE)
    snap = Path(os.environ["TEMP"]) / "qj_snap.ts"
    with open(LIVE_TS, "rb", buffering=0) as f:
        f.seek(0, io.SEEK_END)
        size = f.tell()
        start = (max(0, size - want) // 188) * 188
        f.seek(start)
        data = f.read()
    with open(snap, "wb") as o:
        o.write(data)
    return snap


def measure_once(program, window):
    if not LIVE_TS.exists():
        emit(0, "broken", 0, 0, 0, 0, window, "no live.ts — chain not running")
        return 0
    snap = snapshot_tail(window)
    # decode the program's video+audio for `window` seconds to null
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "info",
         "-fflags", "+genpts+igndts", "-err_detect", "ignore_err",
         "-f", "mpegts", "-i", str(snap),
         "-map", f"0:p:{program}:v?", "-map", f"0:p:{program}:a?",
         "-t", str(window), "-f", "null", "-"],
        capture_output=True, text=True)
    log = (proc.stderr or "")
    frames = 0
    fm = FRAME_RE.findall(log)
    if fm:
        frames = int(fm[-1])
    v_errors = len(VID_ERR_RE.findall(log))
    a_errors = len(AUD_ERR_RE.findall(log))
    fps = frames / window if window else 0
    v_eps = v_errors / window if window else 0
    a_eps = a_errors / window if window else 0
    # score: fps*3 (90=30fps perfect), penalize errors
    score = fps * 3
    score -= min(30, v_eps)          # up to -30 for video errors
    score -= min(20, a_eps * 2)      # up to -20 for audio errors
    score = max(0, min(100, int(score)))
    tier = ("cable_quality" if score >= 90 else
            "watchable" if score >= 60 else
            "glitchy" if score >= 30 else "broken")
    reason = (f"{frames} frames in {window}s = {fps:.1f}fps; "
              f"video_err/s={v_eps:.2f}; audio_err/s={a_eps:.2f}")
    emit(score, tier, round(fps, 2), round(v_eps, 2), round(a_eps, 2), frames, window, reason)
    return score


def emit(score, tier, fps, v_eps, a_eps, frames, window, reason):
    obj = {"ts": time.strftime("%H:%M:%S"), "score": score, "tier": tier,
           "fps": fps, "video_errors_per_sec": v_eps, "audio_errors_per_sec": a_eps,
           "decoded_frames": frames, "window_sec": window, "reason": reason}
    STATE.write_text(json.dumps(obj, indent=2))
    print(f"[{obj['ts']}] score={score} tier={tier} fps={fps} "
          f"v_err={v_eps}/s a_err={a_eps}/s  ({reason})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--program", type=int, default=3)
    ap.add_argument("--window", type=int, default=15)
    ap.add_argument("--loop", action="store_true")
    args = ap.parse_args()
    if args.loop:
        while True:
            measure_once(args.program, args.window)
            time.sleep(30)
    else:
        measure_once(args.program, args.window)


if __name__ == "__main__":
    main()
