"""Windows live-edge player for the STVT TV stream (equivalent of
stvt_play_hd.sh on Linux).

Why: when tv_live.py starts, the LMS equalizer takes ~10s to converge.
During that window the chain emits ~4k RS-uncorrectable + ~2k silently-
miscorrected packets, which freeze any player that reads live.ts from
byte 0. The Linux script avoids this by reading the LAST N bytes of the
file (`tail -c N -F`) so the player only ever sees post-convergence
clean output. This script does the same on Windows.

Usage:
    python tools/stvt_play_hd.py [program] [tailMB]

Defaults:
    program = 3   (the HD 1920x1080 feed; check ffprobe -show_programs)
    tailMB  = 25  (~20 seconds of cushion at 19.4 Mbps)
"""
from __future__ import annotations
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TS_PATH = REPO / "tools" / "data" / "tv_live" / "live.ts"
FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
FFPLAY = r"C:\ffmpeg\bin\ffplay.exe"
VLC = r"C:\Program Files\VideoLAN\VLC\vlc.exe"


USAGE = (
    "usage: stvt_play_hd.py [program] [tailMB]\n"
    "  program  ATSC program number to play (default: 3 = the HD feed)\n"
    "  tailMB   how many MB at the end of live.ts to read "
    "(default: 25 ~= 20s)\n"
    "\n"
    "Reads the LAST N MB of live.ts so the player never sees the\n"
    "equalizer-convergence packet burst at the start of the chain.\n"
)


def parse_args():
    # We support `-h`/`--help` even though we don't use argparse so the
    # user gets a usable hint instead of a stack trace from int('-h').
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(USAGE)
        sys.exit(0)
    try:
        prog = int(sys.argv[1]) if len(sys.argv) > 1 else 3
        tail_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    except ValueError as exc:
        print(f"[hd] bad numeric argument: {exc}\n\n{USAGE}",
              file=sys.stderr)
        sys.exit(2)
    return prog, tail_mb


def tail_to_pipe(file_path: Path, tail_bytes: int, sink, stop_evt: threading.Event):
    """Open file_path, seek to len-tail_bytes, then write to sink as the file
    grows. Mirrors `tail -c N -F` on Linux."""
    # Wait until the file exists and has at least tail_bytes
    while not stop_evt.is_set():
        if file_path.exists() and file_path.stat().st_size >= tail_bytes:
            break
        time.sleep(0.5)
    if stop_evt.is_set():
        return

    with open(file_path, "rb") as f:
        f.seek(-tail_bytes, os.SEEK_END)
        while not stop_evt.is_set():
            chunk = f.read(64 * 1024)
            if chunk:
                try:
                    sink.write(chunk)
                    sink.flush()
                except (BrokenPipeError, OSError):
                    return
            else:
                time.sleep(0.05)


def main():
    prog, tail_mb = parse_args()
    tail_bytes = tail_mb * 1_000_000

    if not TS_PATH.exists():
        print(f"live.ts not found at {TS_PATH}", file=sys.stderr)
        print("Start the chain first: python tools/tv_live.py --rf 34", file=sys.stderr)
        sys.exit(1)

    sz = TS_PATH.stat().st_size
    print(f"live.ts size: {sz/1e6:.1f} MB  reading last {tail_mb} MB  program={prog}",
          file=sys.stderr)
    if sz < tail_bytes:
        print(f"  (will start from beginning until file reaches {tail_mb} MB)", file=sys.stderr)
        tail_bytes = sz

    # Demux program N to a clean mpegts stream that the player can buffer.
    # NO -fflags nobuffer / -flags low_delay / -framedrop on the player side —
    # those tradeoffs make sense for a real-time camera, but for broadcast
    # HD we want generous buffering so playback never starves.
    ffmpeg = subprocess.Popen([
        FFMPEG,
        "-hide_banner", "-loglevel", "warning",
        "-fflags", "+discardcorrupt",
        "-probesize", "5M", "-analyzeduration", "5M",
        "-err_detect", "ignore_err",
        "-i", "pipe:0",
        "-map", f"0:p:{prog}", "-c", "copy",
        "-f", "mpegts", "pipe:1",
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    # Prefer VLC if installed — it handles live TS with a real buffer.
    if os.path.exists(VLC):
        player = subprocess.Popen([
            VLC,
            "--no-video-title-show",
            "--meta-title", f"STVT Live (prog {prog})",
            "--file-caching", "3000",      # 3s file cache
            "--network-caching", "3000",   # 3s on stdin pipe (VLC treats it as net)
            "--live-caching", "3000",
            "--sout-mux-caching", "3000",
            "--clock-jitter", "1000",
            "--no-osd",
            "-",                            # read from stdin
        ], stdin=ffmpeg.stdout)
    else:
        player = subprocess.Popen([
            FFPLAY,
            "-loglevel", "warning",
            "-window_title", f"STVT Live (prog {prog})",
            "-fflags", "+genpts+discardcorrupt",
            "-i", "pipe:0",
        ], stdin=ffmpeg.stdout)

    ffmpeg.stdout.close()

    stop = threading.Event()
    tailer = threading.Thread(
        target=tail_to_pipe, args=(TS_PATH, tail_bytes, ffmpeg.stdin, stop),
        daemon=True,
    )
    tailer.start()

    try:
        player.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for p in (ffmpeg, player):
            try:
                p.terminate()
            except OSError:
                pass
        for p in (ffmpeg, player):
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
