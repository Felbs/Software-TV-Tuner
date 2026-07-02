"""Fixed live player — uses the project's PROVEN tail mechanism.

The bug in my earlier players: I seeked from the START (size-N // 188 * 188)
and used aggressive low-latency flags. The project's stvt_play_hd.py tail
seeks from the END (SEEK_END) and uses GENEROUS buffering — that's what
actually parses the live TS. This replicates that exactly, then renders with
ffplay (simplest reliable renderer).

Usage:
    python liveplay_fixed.py [program] [tailMB]
"""
import os, sys, time, threading, subprocess

TS = r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts"
FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
FFPLAY = r"C:\ffmpeg\bin\ffplay.exe"
if not os.path.exists(FFMPEG):
    FFMPEG = "ffmpeg"
if not os.path.exists(FFPLAY):
    FFPLAY = r"C:\Users\user\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffplay.exe"

prog = int(sys.argv[1]) if len(sys.argv) > 1 else 9
tail_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 25
tail_bytes = tail_mb * 1_000_000

# Wait for the file to have enough cushion
while not (os.path.exists(TS) and os.path.getsize(TS) >= tail_bytes):
    time.sleep(0.5)

print(f"[play] tail {tail_mb}MB -> ffmpeg(-map 0:p:{prog}) -> ffplay")

# ffmpeg: demux ONE program to clean mpegts. Generous probe, discard corrupt.
ffmpeg = subprocess.Popen([
    FFMPEG, "-hide_banner", "-loglevel", "warning",
    "-fflags", "+discardcorrupt",
    "-probesize", "5M", "-analyzeduration", "5M",
    "-err_detect", "ignore_err",
    "-i", "pipe:0",
    "-map", f"0:p:{prog}", "-c", "copy",
    "-f", "mpegts", "pipe:1",
], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

# ffplay: GENEROUS buffer (no low-delay flags — those starve playback).
player = subprocess.Popen([
    FFPLAY, "-loglevel", "warning",
    "-window_title", f"LIVE TV (prog {prog})",
    "-fflags", "+genpts+discardcorrupt",
    "-i", "pipe:0",
], stdin=ffmpeg.stdout)
ffmpeg.stdout.close()

stop = threading.Event()
def tail_to_pipe():
    # PROJECT'S WORKING METHOD: seek from END, then follow growth.
    with open(TS, "rb") as f:
        f.seek(-tail_bytes, os.SEEK_END)
        while not stop.is_set():
            chunk = f.read(65536)
            if chunk:
                try:
                    ffmpeg.stdin.write(chunk)
                except (BrokenPipeError, OSError):
                    break
            else:
                try: ffmpeg.stdin.flush()
                except Exception: pass
                time.sleep(0.1)
    try: ffmpeg.stdin.close()
    except Exception: pass

t = threading.Thread(target=tail_to_pipe, daemon=True)
t.start()
try:
    player.wait()
except KeyboardInterrupt:
    pass
finally:
    stop.set()
    for p in (player, ffmpeg):
        if p.poll() is None:
            p.terminate()
