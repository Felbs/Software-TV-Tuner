#!/usr/bin/env python3
"""pipe_throttle.py — rate-limit stdin → stdout to match ATSC chain rate.

Forces the prebuffer dump (dd of historic live.ts) to flow at the same
~1.5 MB/s rate as the live chain, so ffmpeg can't process the prebuffer
faster than real-time. Prevents the "ffmpeg output PTS jumps ahead of
wall-clock → mpv plays per-source-PTS = visible skipping" pattern.

Usage:
    dd if=live.ts ... | pipe_throttle.py 1500 | ffmpeg ...
                                          ^^^^
                                          KB/s rate cap
"""
import sys, time

KB_PER_SEC = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
CHUNK_BYTES = 8 * 1024  # 8KB chunks

bytes_per_chunk = CHUNK_BYTES
target_rate = KB_PER_SEC * 1024
chunk_interval = bytes_per_chunk / target_rate  # seconds per chunk

fin = sys.stdin.buffer
fout = sys.stdout.buffer

next_send = time.monotonic()
total_sent = 0
start = time.monotonic()

while True:
    chunk = fin.read(CHUNK_BYTES)
    if not chunk:
        break
    try:
        fout.write(chunk)
        fout.flush()
    except (BrokenPipeError, OSError):
        sys.exit(0)
    total_sent += len(chunk)

    next_send += chunk_interval
    now = time.monotonic()
    sleep_for = next_send - now
    if sleep_for > 0:
        time.sleep(sleep_for)
    else:
        next_send = now  # don't accumulate behind-schedule debt
