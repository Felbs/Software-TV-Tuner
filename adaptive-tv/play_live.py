"""play_live.py — smooth live-edge player for the STVT chain's live.ts.

Fixes the two things that make live playback glitch on a FLAWLESS chain:

  1. NO PACKET DISCARDING. ffmpeg `+discardcorrupt` (used by the older players)
     throws away packets it flags as suspect — on a live tail that's motion
     (P/B-frame) data and sparse AC-3 audio packets, which is exactly why you
     see "glitches when people move" + "audio cuts out". We pass every packet.
  2. REAL BUFFER. The old players sat right at the live edge with ~1s of cache,
     so a single high-bitrate motion frame underran the player. We start the
     player a chosen number of seconds BEHIND live and give VLC a fat cache, so
     motion spikes are absorbed. Costs a few seconds of latency — fine for TV.

Pipeline:  tail(live.ts, N s behind edge, no-discard) -> ffmpeg(-map 0:p:P -c copy)
           -> VLC (fat file/live cache, no drop)

Usage:
    python play_live.py                 # program 3, ~12s buffer
    python play_live.py 3 --buffer 15
    python play_live.py 5 --buffer 8
"""
import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

TS_PATH = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
VLC = r"C:\Program Files\VideoLAN\VLC\vlc.exe"
FFMPEG = "ffmpeg"   # resolved from PATH
TS_RATE_BYTES = 2_400_000     # ~full ATSC TS byte-rate (for s -> bytes runway)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("program", type=int, nargs="?", default=3)
    ap.add_argument("--buffer", type=float, default=12.0,
                    help="seconds behind the live edge to start (the safety buffer). default 12")
    ap.add_argument("--ts", default=str(TS_PATH))
    args = ap.parse_args()

    ts = Path(args.ts)
    if not ts.exists():
        print(f"[live] no live.ts at {ts} — is tv_live running?", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(VLC):
        print(f"[live] VLC not found at {VLC}", file=sys.stderr)
        sys.exit(2)

    runway = int(args.buffer * TS_RATE_BYTES)
    cache_ms = int(args.buffer * 1000)

    # wait until the file has at least the runway we want behind us
    while ts.stat().st_size < runway + 4 * 1024 * 1024:
        time.sleep(0.5)

    # ffmpeg: demux ONE program, copy through, NO discard, regenerate PTS so
    # VLC's clock is monotonic. nobuffer OFF — we WANT ffmpeg to buffer.
    ff = subprocess.Popen(
        [FFMPEG, "-hide_banner", "-loglevel", "error",
         "-fflags", "+genpts+igndts",
         "-err_detect", "ignore_err",
         "-analyzeduration", "5000000", "-probesize", "5000000",
         "-f", "mpegts", "-i", "pipe:0",
         "-map", f"0:p:{args.program}", "-c", "copy",
         "-f", "mpegts", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    # VLC: fat caches so a motion spike never starves playback. No drop options.
    vlc = subprocess.Popen(
        [VLC, "--no-video-title-show", "--no-osd",
         "--meta-title", f"STVT Live (prog {args.program})",
         f"--file-caching={cache_ms}",
         f"--live-caching={cache_ms}",
         f"--network-caching={cache_ms}",
         f"--sout-mux-caching={cache_ms}",
         "--clock-jitter=0", "--clock-synchro=0",
         "-"],
        stdin=ff.stdout)
    ff.stdout.close()

    stop = threading.Event()

    def tail():
        # start `buffer` seconds behind the live edge, 188-aligned, then follow
        with open(ts, "rb") as f:
            size = ts.stat().st_size
            start = ((size - runway) // 188) * 188
            f.seek(max(0, start))
            print(f"[live] starting {args.buffer:.0f}s behind edge "
                  f"({runway//1024} KB runway), program {args.program}, "
                  f"VLC cache {cache_ms} ms")
            idle = 0
            while not stop.is_set():
                chunk = f.read(188 * 1024)
                if chunk:
                    idle = 0
                    try:
                        ff.stdin.write(chunk)
                    except (BrokenPipeError, OSError):
                        break
                else:
                    idle += 1
                    if idle > 600:        # 60s no data = chain stopped
                        print("[live] no data for 60s — chain stopped")
                        break
                    try: ff.stdin.flush()
                    except Exception: pass
                    time.sleep(0.1)
        try: ff.stdin.close()
        except Exception: pass

    t = threading.Thread(target=tail, daemon=True)
    t.start()
    try:
        vlc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for p in (vlc, ff):
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=3)
                except Exception: p.kill()


if __name__ == "__main__":
    main()
