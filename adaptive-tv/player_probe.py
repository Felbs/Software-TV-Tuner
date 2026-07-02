"""player_probe.py — automated playback-quality test harness ("autobot").

Measures live-player smoothness OBJECTIVELY (no human eyes needed) so player
configs can be A/B'd and troubleshot programmatically. It captures the EXACT
stream the player receives — tail(live.ts) -> ffmpeg(-map 0:p:P [+discard?]) —
for N seconds, then machine-analyzes it for the three things a viewer notices:

  • VIDEO GLITCHES  = mpeg2video decode errors (damaged/concealing/corrupt)
  • AUDIO CUTOUTS   = ac3 decode errors + audio-frame gaps
  • STUTTER / LOSS  = transport continuity-counter (CC) breaks = dropped packets

It prints counts + a verdict and exits non-zero if quality is bad, so it can
gate a player change. Run it against the running chain; tweak the player config
flags below; re-run; compare numbers.

Usage:
    python player_probe.py --program 3 --seconds 30
    python player_probe.py --program 3 --seconds 30 --discard   # test +discardcorrupt
    python player_probe.py --program 3 --buffer 12 --seconds 45
"""
import argparse
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

TS_PATH = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
TSANALYZE = r"C:\Program Files\TSDuck\bin\tsanalyze.exe"
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
TS_RATE_BYTES = 2_400_000

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def capture_program(program, seconds, buffer_s, discard, out_path):
    """Replicate the player's front-end: tail from `buffer_s` behind the live
    edge and demux ONE program to out_path for `seconds`. Returns bytes written."""
    runway = int(buffer_s * TS_RATE_BYTES)
    while TS_PATH.stat().st_size < runway + 4 * 1024 * 1024:
        time.sleep(0.5)
    discard_flag = "+discardcorrupt" if discard else ""
    ff = subprocess.Popen(
        [FFMPEG, "-hide_banner", "-loglevel", "error",
         "-fflags", f"{discard_flag}+genpts+igndts",
         "-err_detect", "ignore_err",
         "-analyzeduration", "5000000", "-probesize", "5000000",
         "-f", "mpegts", "-i", "pipe:0",
         "-map", f"0:p:{program}", "-c", "copy",
         "-f", "mpegts", str(out_path)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    stop = threading.Event()

    def tail():
        with open(TS_PATH, "rb") as f:
            size = TS_PATH.stat().st_size
            f.seek(max(0, ((size - runway) // 188) * 188))
            while not stop.is_set():
                chunk = f.read(188 * 1024)
                if chunk:
                    try: ff.stdin.write(chunk)
                    except (BrokenPipeError, OSError): break
                else:
                    try: ff.stdin.flush()
                    except Exception: pass
                    time.sleep(0.1)
        try: ff.stdin.close()
        except Exception: pass

    t = threading.Thread(target=tail, daemon=True); t.start()
    time.sleep(seconds)
    stop.set()
    time.sleep(0.5)
    if ff.poll() is None:
        ff.terminate()
        try: ff.wait(timeout=3)
        except Exception: ff.kill()
    return out_path.stat().st_size if out_path.exists() else 0


def analyze_decode(path):
    """Decode the captured program to null, counting video/audio decode errors."""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "warning",
         "-err_detect", "explode",
         "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True)
    err = proc.stderr or ""
    vid_err = len(re.findall(r"(mpeg2video|h264).*(damaged|concealing|corrupt|error|invalid)", err, re.I))
    aud_err = len(re.findall(r"(ac-3|ac3|aac|mp2|mp3).*(error|corrupt|skip|missing|incomplete)", err, re.I))
    concealing = len(re.findall(r"concealing", err, re.I))
    return vid_err, aud_err, concealing, err


def probe_frames(path):
    """Count actually-decoded video frames + report stream resolution."""
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-count_frames", "-show_entries",
         "stream=codec_name,width,height,nb_read_frames,avg_frame_rate",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True).stdout
    d = dict(re.findall(r"(\w+)=([^\n]+)", out))
    return d


def cc_errors(path):
    """TSDuck continuity-counter error count = dropped/discontinuous packets."""
    if not os.path.exists(TSANALYZE):
        return None, None
    out = subprocess.run([TSANALYZE, str(path)], capture_output=True, text=True)
    txt = (out.stdout or "") + (out.stderr or "")
    m_cc = re.search(r"(?:Discontinuit|continuity).{0,40}?([\d,]+)", txt, re.I)
    m_te = re.search(r"transport error.*?([\d,]+)", txt, re.I)
    cc = int(m_cc.group(1).replace(",", "")) if m_cc else None
    te = int(m_te.group(1).replace(",", "")) if m_te else None
    return cc, te


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--program", type=int, default=3)
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--buffer", type=float, default=12.0)
    ap.add_argument("--discard", action="store_true",
                    help="test WITH ffmpeg +discardcorrupt (the suspected glitch cause)")
    args = ap.parse_args()

    if not TS_PATH.exists():
        print(f"[probe] no live.ts at {TS_PATH} — is tv_live running?", file=sys.stderr)
        sys.exit(2)

    out = Path(os.environ["TEMP"]) / f"probe_p{args.program}_{'disc' if args.discard else 'nodisc'}.ts"
    if out.exists():
        try: out.unlink()
        except Exception: pass

    print(f"[probe] capturing program {args.program} for {args.seconds}s "
          f"({'+discardcorrupt' if args.discard else 'NO discard'}, {args.buffer:.0f}s buffer)...")
    nbytes = capture_program(args.program, args.seconds, args.buffer, args.discard, out)
    if nbytes < 1_000_000:
        print(f"[probe] capture too small ({nbytes} B) — program {args.program} present?")
        sys.exit(3)

    secs_captured = nbytes / TS_RATE_BYTES
    info = probe_frames(out)
    vid_err, aud_err, concealing, _ = analyze_decode(out)
    cc, te = cc_errors(out)

    fps = info.get("avg_frame_rate", "?")
    nframes = int(info.get("nb_read_frames", 0) or 0)
    res = f"{info.get('codec_name','?')} {info.get('width','?')}x{info.get('height','?')}"
    # expected frames at nominal rate (29.97 for 1080i NTSC) over real capture span
    exp = 29.97 * args.seconds
    frame_completeness = (nframes / exp * 100) if exp else 0

    print("\n" + "=" * 56)
    print(f"  PLAYBACK PROBE — program {args.program}  "
          f"({'+discard' if args.discard else 'no-discard'})")
    print("=" * 56)
    print(f"  captured        {nbytes/1e6:6.1f} MB  (~{secs_captured:.0f}s of TS)")
    print(f"  video stream    {res}  @ {fps} fps")
    print(f"  frames decoded  {nframes}  (~{frame_completeness:.0f}% of {exp:.0f} expected)")
    print(f"  VIDEO errors    {vid_err}   (damaged/corrupt/concealing macroblocks)")
    print(f"  AUDIO errors    {aud_err}   (ac3 decode errors / gaps)")
    print(f"  concealing ops  {concealing}")
    if cc is not None:
        print(f"  CC errors       {cc}   (transport discontinuities = dropped packets)")
    if te is not None:
        print(f"  transport errs  {te}")
    # verdict
    bad = (vid_err > 2) or (aud_err > 2) or (cc or 0) > 2 or frame_completeness < 90
    verdict = "GLITCHY" if bad else "SMOOTH"
    print("-" * 56)
    print(f"  VERDICT: {verdict}")
    print("=" * 56)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
