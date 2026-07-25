"""quality_judge.py — Windows port of the Linux quality_judge.sh apparatus.

Measures video + audio quality of the chain's live.ts and emits a 0-100 score
+ tier, so the bot can read decode quality OBJECTIVELY instead of asking the
user "is it glitchy?". Measures the CHAIN output directly (snapshot of the tail,
decode the program's video AND audio), so a high score here + a glitchy picture
means the PLAYER is the culprit, not the chain.

Method:
  1. snapshot the last ~WINDOW*2.5 s of live.ts (shared read; chain holds it open)
  2. ffprobe the program's DECLARED video frame rate (the broadcast rate, from
     stream headers — robust even when decode is failing)
  3. ffmpeg null-decode WINDOW s of the program's video+audio -> decoded fps +
     video/audio decode-error counts
  4. score = DELIVERY RATIO (decoded_fps / broadcast_fps) * 100, penalized by
     error rates. Normalizing by the native rate is what makes 24 fps film that
     decodes perfectly score 100, while a 60 fps channel that drops half its
     frames scores 50 — and the error penalty is scaled so a channel dropping
     >~3 corrupt frames/s cannot reach "cable". (Pre-2026-07-22 the score was a
     flat fps*3, which rated a glitchy 60 fps channel "cable" and a clean 24 fps
     channel "glitchy" — the stress test caught both; see the framerate-
     normalized rewrite.)

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
FFPROBE = "ffprobe"
TS_RATE = 2_400_000   # bytes/s, for window -> bytes

# Standard broadcast frame rates; the declared rate is snapped to the nearest
# of these when it lands within 8 % (guards against odd container metadata).
STD_RATES = [23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0]
# A healthy channel delivers ~88-94 % of its native rate through a WINDOW-second
# null-decode (the snapshot is cut mid-GOP, so the first ~1 s of frames is lost).
# Treat >= this fraction of native as full delivery so the window artifact does
# not cost clean channels their "cable" rating; real frame loss shows up below it
# AND carries corruption errors that the penalty catches.
NATIVE_HEADROOM = 0.92

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


def _snap_to_std(fps):
    """Snap a measured rate to the nearest standard broadcast rate (within 8%)."""
    if not fps or fps <= 0:
        return None
    best = min(STD_RATES, key=lambda r: abs(r - fps))
    return best if abs(best - fps) / best < 0.08 else round(fps, 3)


def native_fps(source, program):
    """DECLARED broadcast frame rate of the program's video, via ffprobe.
    Reads stream metadata (not decoded frames), so it reports the true native
    rate even when the channel is glitching. Returns None if unknown."""
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", f"p:{program}:v:0",
             "-show_entries", "stream=avg_frame_rate,r_frame_rate",
             "-of", "default=nw=1:nk=1", str(source)],
            capture_output=True, text=True, timeout=25)
    except Exception:
        return None
    rates = []
    for tok in (r.stdout or "").split():
        if "/" in tok:
            n, d = tok.split("/", 1)
            try:
                n, d = float(n), float(d)
                if d:
                    rates.append(n / d)
            except ValueError:
                pass
    # avg_frame_rate is listed first; prefer it, fall back to r_frame_rate
    fps = next((v for v in rates if v > 0), 0.0)
    return _snap_to_std(fps)


def compute_score(decoded_fps, broadcast_fps, v_eps, a_eps):
    """Framerate-normalized quality score, 0-100.

    decoded_fps   frames/s actually decoded in the window
    broadcast_fps declared native rate (None/0 -> judge on errors only)
    v_eps, a_eps  video / audio decode errors per second
    """
    if decoded_fps < 2:                 # no real video came through
        return 0
    if broadcast_fps and broadcast_fps > 0:
        delivery = min(1.0, decoded_fps / (broadcast_fps * NATIVE_HEADROOM))
    else:
        delivery = 1.0                  # unknown rate -> don't punish framerate
    base = delivery * 100.0
    # error penalty: v_eps*3 (so cable, >=90, needs < ~3 corrupt frames/s),
    # audio a_eps*2; capped so a broken channel floors at 0, not negative noise.
    base -= min(60.0, v_eps * 3.0)
    base -= min(25.0, a_eps * 2.0)
    return max(0, min(100, int(round(base))))


def tier_of(score):
    return ("cable_quality" if score >= 90 else
            "watchable" if score >= 60 else
            "glitchy" if score >= 30 else "broken")


def measure_once(program, window):
    if not LIVE_TS.exists():
        emit(0, "broken", 0, 0, 0, 0, None, window, "no live.ts — chain not running")
        return 0
    snap = snapshot_tail(window)
    broadcast = native_fps(snap, program)
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
    score = compute_score(fps, broadcast, v_eps, a_eps)
    tier = tier_of(score)
    delivery = (min(1.0, fps / (broadcast * NATIVE_HEADROOM))
                if broadcast else None)
    reason = (f"{frames} frames in {window}s = {fps:.1f}fps of "
              f"{broadcast or '?'}fps native "
              f"(delivery {('%.0f%%' % (delivery * 100)) if delivery is not None else '?'}); "
              f"video_err/s={v_eps:.2f}; audio_err/s={a_eps:.2f}")
    emit(score, tier, round(fps, 2), round(v_eps, 2), round(a_eps, 2),
         frames, broadcast, window, reason)
    return score


def emit(score, tier, fps, v_eps, a_eps, frames, broadcast, window, reason):
    obj = {"ts": time.strftime("%H:%M:%S"), "score": score, "tier": tier,
           "fps": fps, "broadcast_fps": broadcast,
           "video_errors_per_sec": v_eps, "audio_errors_per_sec": a_eps,
           "decoded_frames": frames, "window_sec": window, "reason": reason}
    STATE.write_text(json.dumps(obj, indent=2))
    print(f"[{obj['ts']}] score={score} tier={tier} fps={fps}/{broadcast} "
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
