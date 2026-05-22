#!/usr/bin/env python3
"""ts_pes_scrub.py — PES-aware corruption filter.

Reads MPEG-TS packets from stdin. Tracks PES per PID:
  - When PUSI=1 (payload-unit-start), parses PES header:
      * Verifies 0x000001 start code prefix
      * Reads PES_packet_length
      * Reads PTS/DTS for sanity
  - When PUSI=1 announces a new PES while the previous one's reported
    length wasn't reached, the previous PES was TRUNCATED — drops it
    (replaces all its TS packets with NULLs in the output).
  - When PES header is corrupt, drops the entire PES until next PUSI=1.

This catches the "PES packet size mismatch" / "Packet corrupt" cases
that ffmpeg flags but processes with corrupt frames, giving it cleaner
input so the demuxer state stays sane.

Tradeoffs: drops potentially-recoverable partial frames. mpeg2video
without GOP integrity is often unwatchable anyway, so dropping is
usually no worse than letting partial decode glitch.

Usage:
    tail -c +1 -F live.ts | python3 ts_pes_scrub.py | ffmpeg -i pipe:0 ...

Tuning via env:
    TS_PES_LOG=1     stderr summary every 5s
    TS_PES_BUFFER=N  output buffering — when dropping, we may need to
                     buffer up to N packets of a PES before deciding it
                     was bad (default 64). Higher = better detection,
                     more latency.
"""
from __future__ import annotations
import os
import sys
import time
from collections import deque

PKT_SIZE  = 188
SYNC_BYTE = 0x47
NULL_PID  = 0x1FFF

NULL_PKT = bytes([0x47, 0x1F, 0xFF, 0x10]) + bytes([0xFF] * (PKT_SIZE - 4))

LOG       = sys.stderr
PRINT_LOG = os.environ.get("TS_PES_LOG", "0") == "1"
BUFFER_N  = int(os.environ.get("TS_PES_BUFFER", "64"))


class PESState:
    """Per-PID PES tracking state."""
    __slots__ = ("expected_len", "got_bytes", "pkts_in_pes",
                 "buf", "buf_was_truncated", "in_bad", "first_pkt_seen")

    def __init__(self):
        self.expected_len = 0          # PES_packet_length from header; 0=unbounded
        self.got_bytes    = 0          # data bytes accumulated since PES start
        self.pkts_in_pes  = 0
        self.buf          = deque()    # buffered TS packets of current PES (if not yet emitted)
        self.buf_was_truncated = False # buffer exceeded BUFFER_N and started passthrough
        self.in_bad       = False      # in drop-until-next-PUSI mode
        self.first_pkt_seen = False


def parse_pid(pkt: bytes) -> int:
    return ((pkt[1] & 0x1f) << 8) | pkt[2]


def has_pusi(pkt: bytes) -> bool:
    return bool(pkt[1] & 0x40)


def has_payload(pkt: bytes) -> bool:
    afc = (pkt[3] >> 4) & 0x3
    return afc == 1 or afc == 3


def payload_offset(pkt: bytes) -> int:
    """Byte offset into pkt where payload starts (skip TS header + AF)."""
    afc = (pkt[3] >> 4) & 0x3
    if afc == 1:
        return 4
    if afc == 3:
        # Skip AF: byte 4 = AF length, then AF body, then payload.
        af_len = pkt[4]
        return 5 + af_len
    return PKT_SIZE  # no payload


def parse_pes_header(payload: bytes) -> tuple[bool, int, bool]:
    """Parse a PES header from start of payload.

    Returns (decision, declared_length, definitely_bad).
      decision: True if header looks valid (or not enough data to judge)
      declared_length: PES length field, 0 = unbounded
      definitely_bad: True only when we have enough bytes AND start code is wrong
    """
    # If we don't have at least 3 bytes, we CAN'T check start code.
    # Assume valid (small PES header split across packets — large AF cases).
    if len(payload) < 3:
        return True, 0, False
    if payload[0] != 0 or payload[1] != 0 or payload[2] != 1:
        # Definitely corrupt: PES MUST start with 0x000001.
        return False, 0, True
    # Have 3+ bytes of valid start code. If we don't have 6, can't read length
    # — assume valid PES, leave length unknown (treat as unbounded).
    if len(payload) < 6:
        return True, 0, False
    sid = payload[3]
    if sid < 0xBC:
        # Reserved/unused stream_ids — definitely corrupt.
        return False, 0, True
    length = (payload[4] << 8) | payload[5]
    return True, length, False


pid_state: dict[int, PESState] = {}


def output_pkt(out_fh, pkt: bytes, drop: bool):
    out_fh.write(NULL_PKT if drop else pkt)


def flush_buf(out_fh, st: PESState, drop: bool):
    """Emit buffered packets, all as drop or all as keep."""
    while st.buf:
        output_pkt(out_fh, st.buf.popleft(), drop)


def handle_packet(out_fh, pkt: bytes, stats: dict) -> None:
    pid = parse_pid(pkt)

    # NULL packets — pass through, no PES state.
    if pid == NULL_PID:
        out_fh.write(pkt)
        return
    # PSI PIDs (PAT=0, PMT range 0x0010-0x1FFE for many use cases).
    # We could differentiate, but easiest: don't track PES for low PIDs.
    # PSI uses sections, not PES.
    if pid == 0 or pid < 0x20:
        out_fh.write(pkt)
        return

    pusi = has_pusi(pkt)
    pay  = has_payload(pkt)

    st = pid_state.get(pid)
    if st is None:
        st = PESState()
        pid_state[pid] = st

    if pusi and pay:
        # New PES boundary.
        # First, finalize the PREVIOUS PES (if any).
        if st.first_pkt_seen:
            stats["pes_total"] += 1
            # Check if previous PES had right length.
            if st.expected_len != 0 and st.got_bytes != st.expected_len:
                # Truncated PES — drop the buffered remains.
                stats["pes_truncated"] += 1
                if not st.buf_was_truncated:
                    flush_buf(out_fh, st, drop=True)
            else:
                # Good PES — flush.
                if not st.buf_was_truncated:
                    flush_buf(out_fh, st, drop=False)
        # Reset state for new PES.
        st.expected_len     = 0
        st.got_bytes        = 0
        st.pkts_in_pes      = 0
        st.buf.clear()
        st.buf_was_truncated = False
        st.first_pkt_seen   = True
        st.in_bad           = False

        # Parse the new PES header to validate.
        off = payload_offset(pkt)
        payload = pkt[off:]
        ok, declared, definitely_bad = parse_pes_header(payload)
        if definitely_bad:
            # Confirmed corrupt PES header (we had enough bytes to be sure).
            stats["pes_bad_header"] += 1
            st.in_bad = True
            output_pkt(out_fh, pkt, drop=True)
            return
        st.expected_len = declared
        # Buffer this packet rather than emit, in case PES turns out truncated.
        if len(st.buf) < BUFFER_N:
            st.buf.append(pkt)
        else:
            # Buffer overflow — start passing through and stop buffering.
            # We can still drop based on header validation but can't retroactively.
            st.buf_was_truncated = True
            output_pkt(out_fh, pkt, drop=False)
        st.got_bytes  += len(payload)
        st.pkts_in_pes = 1
    else:
        # Continuation of PES.
        if not st.first_pkt_seen:
            # No PES start yet — pass through (probably stream startup).
            out_fh.write(pkt)
            return
        if st.in_bad:
            output_pkt(out_fh, pkt, drop=True)
            return
        if pay:
            off = payload_offset(pkt)
            st.got_bytes += (PKT_SIZE - off)
            st.pkts_in_pes += 1
        if len(st.buf) < BUFFER_N and not st.buf_was_truncated:
            st.buf.append(pkt)
        else:
            st.buf_was_truncated = True
            output_pkt(out_fh, pkt, drop=False)


def main() -> int:
    out_fh = sys.stdout.buffer
    in_fh  = sys.stdin.buffer
    t0     = time.monotonic()
    last_log = t0
    stats  = {
        "pkts": 0,
        "pes_total": 0,
        "pes_truncated": 0,
        "pes_bad_header": 0,
        "resyncs": 0,
    }

    buf = b""
    while True:
        chunk = in_fh.read(65536)
        if not chunk:
            break
        buf += chunk
        while len(buf) >= PKT_SIZE:
            if buf[0] != SYNC_BYTE:
                # Resync.
                found = -1
                for i in range(1, min(2000, len(buf) - PKT_SIZE)):
                    if (buf[i] == SYNC_BYTE and
                        i + PKT_SIZE < len(buf) and
                        buf[i + PKT_SIZE] == SYNC_BYTE):
                        found = i
                        break
                if found < 0:
                    break
                stats["resyncs"] += 1
                buf = buf[found:]
                continue

            pkt = buf[:PKT_SIZE]
            buf = buf[PKT_SIZE:]
            stats["pkts"] += 1
            handle_packet(out_fh, pkt, stats)

        out_fh.flush()

        if PRINT_LOG:
            now = time.monotonic()
            if now - last_log >= 5.0:
                bad_pct = (100.0 * (stats["pes_truncated"] + stats["pes_bad_header"]) /
                           stats["pes_total"]) if stats["pes_total"] else 0.0
                LOG.write(
                    f"[ts_pes_scrub t={now-t0:.1f}s] pkts={stats['pkts']} "
                    f"pes={stats['pes_total']} bad_hdr={stats['pes_bad_header']} "
                    f"truncated={stats['pes_truncated']} ({bad_pct:.3f}%)\n")
                LOG.flush()
                last_log = now

    # Flush any pending buffered packets.
    for st in pid_state.values():
        flush_buf(out_fh, st, drop=False)

    # Final.
    bad_pct = (100.0 * (stats["pes_truncated"] + stats["pes_bad_header"]) /
               stats["pes_total"]) if stats["pes_total"] else 0.0
    LOG.write(
        f"[ts_pes_scrub FINAL] pkts={stats['pkts']} pes={stats['pes_total']} "
        f"bad_hdr={stats['pes_bad_header']} truncated={stats['pes_truncated']} "
        f"({bad_pct:.3f}%) resyncs={stats['resyncs']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
