#!/usr/bin/env python3
"""ts_validate.py — drop corrupt TS packets before they reach ffmpeg.

Reads 188-byte MPEG-TS packets from stdin, applies sanity checks, and
replaces ANY packet that fails with a NULL packet (PID 0x1FFF). Writes
the result to stdout.

Why this exists: even after Reed-Solomon decode + teiscrub, the chain
produces some packets with subtle corruption (continuity-counter jumps,
malformed PES headers, bad adaptation-field lengths). ffmpeg's
discardcorrupt sometimes catches them; when it doesn't, mpegts demuxer
logs "Packet corrupt" + the demuxer state goes wobbly, causing freezes.

Replacing corrupt packets with NULLs is GENTLER than dropping them
because the TS clock stays continuous. ffmpeg sees NULL → trivial skip,
no demuxer state perturbation.

Usage:
    tail -c +1 -F live.ts | python3 ts_validate.py | ffmpeg -i pipe:0 ...

Tuning via env:
    TS_VALIDATE_LOG=1     stderr stats every 5s
    TS_VALIDATE_STRICT=1  also drop packets with CC discontinuity (default off)
"""
from __future__ import annotations
import os
import sys
import time

PKT_SIZE  = 188
SYNC_BYTE = 0x47
NULL_PID  = 0x1FFF

# Pre-built NULL packet (sync + null PID + payload of 0xFF).
NULL_PKT = bytes([0x47, 0x1F, 0xFF, 0x10]) + bytes([0xFF] * (PKT_SIZE - 4))

LOG          = sys.stderr
PRINT_LOG    = os.environ.get("TS_VALIDATE_LOG",    "0") == "1"
STRICT       = os.environ.get("TS_VALIDATE_STRICT", "0") == "1"

# Per-PID continuity counter expectations.
last_cc: dict[int, int] = {}


def parse_pid(pkt: bytes) -> int:
    return ((pkt[1] & 0x1f) << 8) | pkt[2]


def validate(pkt: bytes) -> bool:
    """Return True if packet looks legit. False = replace with NULL."""
    # Length & sync.
    if len(pkt) != PKT_SIZE or pkt[0] != SYNC_BYTE:
        return False
    # TEI already set? Already-marked corrupt — drop.
    if pkt[1] & 0x80:
        return False
    pid = parse_pid(pkt)
    # Reserved PIDs.
    if pid == NULL_PID:
        return True  # NULL packet — keep
    # Adaptation field length sanity.
    afc = (pkt[3] >> 4) & 0x3
    if afc == 0:
        # Not allowed by spec (reserved value).
        return False
    if afc == 2 or afc == 3:
        # Has adaptation field; first byte = AF length.
        af_len = pkt[4]
        if af_len > 183:
            return False
        if afc == 3 and af_len == 183:
            # AF len 183 + AFC=3 (AF + payload) leaves 0 bytes for payload.
            # Not corrupt per se but unusual; let it through.
            pass
    # CC continuity check (only if STRICT).
    if STRICT:
        cc = pkt[3] & 0x0F
        prev = last_cc.get(pid)
        last_cc[pid] = cc
        if prev is not None:
            # CC increments mod 16 on packets WITH payload (PUSI or AFC&1).
            # Has-payload check:
            has_payload = (afc & 1) != 0
            if has_payload:
                expected = (prev + 1) & 0x0F
                if cc != expected and cc != prev:
                    # Discontinuity (cc != prev+1 and cc != prev for duplicate).
                    return False
    return True


def main() -> int:
    out_fh   = sys.stdout.buffer
    in_fh    = sys.stdin.buffer
    t0       = time.monotonic()
    last_log = t0
    n_in     = 0
    n_drop   = 0
    n_resync = 0

    # Buffer for handling stream alignment.
    buf = b""
    while True:
        chunk = in_fh.read(65536)
        if not chunk:
            break
        buf += chunk

        # Process whole packets from buf. If we're out of alignment (first
        # byte not 0x47), search forward for sync.
        while len(buf) >= PKT_SIZE:
            if buf[0] != SYNC_BYTE:
                # Resync: find next sync byte with PKT_SIZE confirmation.
                found = -1
                for i in range(1, min(2000, len(buf) - PKT_SIZE)):
                    if (buf[i] == SYNC_BYTE and
                        i + PKT_SIZE < len(buf) and
                        buf[i + PKT_SIZE] == SYNC_BYTE):
                        found = i
                        break
                if found < 0:
                    # Need more data.
                    break
                # Drop everything before the resync point.
                n_resync += 1
                buf = buf[found:]
                continue

            pkt = buf[:PKT_SIZE]
            buf = buf[PKT_SIZE:]
            n_in += 1
            if validate(pkt):
                out_fh.write(pkt)
            else:
                out_fh.write(NULL_PKT)
                n_drop += 1

        out_fh.flush()

        if PRINT_LOG:
            now = time.monotonic()
            if now - last_log >= 5.0:
                rate = (100.0 * n_drop / n_in) if n_in else 0.0
                LOG.write(f"[ts_validate t={now-t0:.1f}s] pkts={n_in} "
                          f"dropped={n_drop} ({rate:.3f}%) resyncs={n_resync}\n")
                LOG.flush()
                last_log = now

    # Final summary (always).
    rate = (100.0 * n_drop / n_in) if n_in else 0.0
    LOG.write(f"[ts_validate FINAL] pkts={n_in} dropped={n_drop} "
              f"({rate:.3f}%) resyncs={n_resync}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
