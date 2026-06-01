#!/usr/bin/env python3
"""ts_pid_filter.py — drop TS packets whose PID isn't in a learned whitelist.

Reads MPEG-TS packets (188 bytes) from stdin. First learns the top N PIDs
in the first SAMPLE_BYTES bytes of input (where N covers >=PCT% of traffic).
Then drops any packet with a PID outside that set, replacing with a NULL
packet so the timing stays constant.

This filters out ~10% of "noise PIDs" that the equalizer can't fully
suppress, letting ffmpeg see only the real ATSC multiplex.

Env:
    TS_PID_SAMPLE_BYTES   bytes to learn from (default 5_000_000 = ~5MB)
    TS_PID_TOP_PCT        cover this fraction of traffic (default 95)
    TS_PID_MIN_PIDS       minimum whitelist size (default 8)
    TS_PID_FORCE_WHITELIST  comma-separated PIDs to always include
                            (default: 0,0x1fff = PAT, NULL)

Usage:
    tail -F live.ts | python3 ts_pid_filter.py | ffmpeg ...
"""
import os
import sys
from collections import Counter

PKT_SIZE = 188
SYNC_BYTE = 0x47
NULL_PID = 0x1fff

SAMPLE_BYTES = int(os.environ.get("TS_PID_SAMPLE_BYTES", str(5_000_000)))
TOP_PCT = int(os.environ.get("TS_PID_TOP_PCT", "95"))
MIN_PIDS = int(os.environ.get("TS_PID_MIN_PIDS", "8"))
FORCE_WHITELIST = set()
for s in os.environ.get("TS_PID_FORCE_WHITELIST", "0,0x1fff").split(","):
    s = s.strip()
    if not s:
        continue
    FORCE_WHITELIST.add(int(s, 0))

LOG = sys.stderr


def get_pid(pkt: bytes) -> int:
    return ((pkt[1] & 0x1F) << 8) | pkt[2]


def build_null_packet() -> bytes:
    """Build a valid 188-byte NULL packet (PID 0x1fff)."""
    # sync(0x47) + payload_unit_start=0 + transport_priority=0 + PID hi=0x1F (NULL PID hi)
    # + PID lo=0xFF + scrambling=0 + adaptation=0b01 (payload only) + cc=0
    # + 184 bytes of 0xFF
    return bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xff" * 184


def main() -> int:
    null_pkt = build_null_packet()
    pid_count = Counter()
    sample_bytes_read = 0
    whitelist = set()
    learning = True
    buf = bytearray()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    while True:
        chunk = stdin.read(65536)
        if not chunk:
            break
        buf.extend(chunk)
        # Process complete packets from buf
        while len(buf) >= PKT_SIZE:
            pkt = bytes(buf[:PKT_SIZE])
            del buf[:PKT_SIZE]
            if pkt[0] != SYNC_BYTE:
                # Misaligned — try to resync by searching for next sync byte
                # Put back what we took, find next sync
                idx = -1
                for i in range(1, PKT_SIZE):
                    if i < len(buf) - 1 and buf[i] == SYNC_BYTE:
                        idx = i
                        break
                if idx > 0:
                    del buf[:idx]
                continue
            pid = get_pid(pkt)
            if learning:
                pid_count[pid] += 1
                sample_bytes_read += PKT_SIZE
                if sample_bytes_read >= SAMPLE_BYTES:
                    # Build whitelist
                    total = sum(pid_count.values())
                    threshold = total * TOP_PCT // 100
                    accumulated = 0
                    for p, c in pid_count.most_common():
                        whitelist.add(p)
                        accumulated += c
                        if accumulated >= threshold and len(whitelist) >= MIN_PIDS:
                            break
                    whitelist |= FORCE_WHITELIST
                    learning = False
                    LOG.write(
                        f"[ts_pid_filter] learned whitelist of {len(whitelist)} PIDs "
                        f"covering {accumulated * 100 // total}% of traffic: "
                        f"{sorted(hex(p) for p in whitelist)}\n"
                    )
                    LOG.flush()
                # During learning, pass through unchanged
                stdout.write(pkt)
            else:
                if pid in whitelist:
                    stdout.write(pkt)
                else:
                    # Replace noise packet with NULL — preserves cadence for ffmpeg
                    stdout.write(null_pkt)
        stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(0)
