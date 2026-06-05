#!/usr/bin/env python3
"""pipe_buffer.py — large in-memory buffer between two pipes.

Usage:
    producer | pipe_buffer.py --buffer-mb 1024 | consumer

Reads stdin into a circular memory buffer up to --buffer-mb, writes
to stdout. This lets a consumer DRAIN the buffer faster than the
producer fills it, which is exactly what we need to let ffmpeg's
probe phase get 30+ seconds of stream data quickly even though the
chain only writes at 1.5 MB/s.

Why bash pipes don't work for this: bash pipes have a small kernel
buffer (~64KB). If the consumer is slow during probe, the producer
blocks. With an in-memory deque holding 1+ GB, the consumer can
drain at memory-copy speed for the duration of its probe.
"""
import sys
import threading
import time
import argparse
from collections import deque

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--buffer-mb', type=int, default=1024,
                    help='Max buffer size in MB before dropping oldest data')
    ap.add_argument('--chunk-kb', type=int, default=64,
                    help='Read chunk size in KB')
    args = ap.parse_args()

    max_bytes = args.buffer_mb * 1024 * 1024
    chunk = args.chunk_kb * 1024

    buf = deque()
    total_bytes = [0]
    lock = threading.Lock()
    eof = threading.Event()
    cond = threading.Condition(lock)

    def reader():
        try:
            fd = sys.stdin.buffer
            while True:
                data = fd.read(chunk)
                if not data:
                    break
                with cond:
                    buf.append(data)
                    total_bytes[0] += len(data)
                    # Drop oldest if over limit
                    while total_bytes[0] > max_bytes and len(buf) > 1:
                        old = buf.popleft()
                        total_bytes[0] -= len(old)
                    cond.notify()
        finally:
            eof.set()
            with cond:
                cond.notify_all()

    def writer():
        fd = sys.stdout.buffer
        try:
            while True:
                with cond:
                    while not buf and not eof.is_set():
                        cond.wait(timeout=0.2)
                    if not buf and eof.is_set():
                        return
                    chunk_out = buf.popleft()
                    total_bytes[0] -= len(chunk_out)
                try:
                    fd.write(chunk_out)
                    fd.flush()
                except (BrokenPipeError, OSError):
                    return
        except KeyboardInterrupt:
            pass

    t_r = threading.Thread(target=reader, daemon=True)
    t_w = threading.Thread(target=writer, daemon=False)
    t_r.start()
    t_w.start()

    # Stats to stderr every 5 sec
    while t_w.is_alive():
        time.sleep(5)
        with cond:
            tb = total_bytes[0]
        sys.stderr.write(f"[pipe_buffer] buffered={tb/1024/1024:.1f}MB eof={eof.is_set()}\n")
        sys.stderr.flush()
        if eof.is_set() and total_bytes[0] == 0:
            break

    t_w.join()

if __name__ == '__main__':
    main()
