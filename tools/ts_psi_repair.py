#!/usr/bin/env python3
"""ts_psi_repair.py — repair MPEG-TS streams that lose PSI during glitches.

Reads TS packets (188 bytes) from stdin. Maintains a cache of the last
valid PAT (PID 0x0000) and PMT(s). When the live stream goes >100 ms
without a fresh PAT or PMT, injects the cached copy. Writes the (now
PSI-continuous) stream to stdout.

Use case: the ATSC demod produces TS where PAT/PMT packets get corrupted
during equalizer drift, even though video bytes keep flowing. Players
then drop their decoder state and freeze. This filter keeps PSI alive
so the player never loses the program structure.

Usage:
    tail -F live.ts | python3 ts_psi_repair.py | mpv -
    tail -F live.ts | python3 ts_psi_repair.py | python3 tv_player.py -
"""
from __future__ import annotations
import os
import sys
import time

PKT_SIZE      = 188
SYNC_BYTE     = 0x47
PAT_PID       = 0x0000

# How often to consider PSI "missing" — in packet counts. At ATSC nominal
# 19.39 Mbps that's ~12,940 packets/sec, so 1500 packets ≈ 100 ms.
MISS_THRESHOLD_PKTS = int(os.environ.get("TS_PSI_MISS_PKTS", "1500"))

# How often we even check for missing PSI. Lower = more responsive,
# higher = less overhead. 200 packets ≈ 15 ms.
CHECK_INTERVAL_PKTS = int(os.environ.get("TS_PSI_CHECK_PKTS", "200"))

# Mode 'miss'    = only inject when no real PAT/PMT seen for MISS_THRESHOLD
# Mode 'always'  = inject at constant rate every CHECK_INTERVAL packets,
#                  regardless of whether real PSI is flowing. Guarantees
#                  the player always sees fresh PSI but adds overhead.
INJECT_MODE = os.environ.get("TS_PSI_MODE", "miss")   # 'miss' | 'always'

# Optional: drop everything until we've cached a valid PAT+PMT. Avoids
# feeding the player a stream of garbage during chain startup.
WARMUP_REQUIRE_PSI = os.environ.get("TS_PSI_WARMUP", "0") == "1"

# Optional: when injecting, do it RIGHT AFTER the cached PMT (so the
# player sees PAT immediately followed by PMT). Default = inject each
# in its own check tick.
GROUPED_INJECT = os.environ.get("TS_PSI_GROUPED", "1") == "1"

LOG = sys.stderr


def parse_pid(pkt: bytes) -> int:
    return ((pkt[1] & 0x1f) << 8) | pkt[2]


def has_tei(pkt: bytes) -> bool:
    return bool(pkt[1] & 0x80)


def has_pusi(pkt: bytes) -> bool:
    return bool(pkt[1] & 0x40)


def set_continuity_counter(pkt: bytes, cc: int) -> bytes:
    """Return a copy of pkt with the continuity counter set to cc."""
    out = bytearray(pkt)
    out[3] = (out[3] & 0xf0) | (cc & 0x0f)
    return bytes(out)


def get_payload_offset(pkt: bytes) -> int | None:
    """Where the payload starts in this TS packet, or None if no payload."""
    adaptation_control = (pkt[3] >> 4) & 0x3
    if adaptation_control in (0, 2):
        return None
    if adaptation_control == 3:
        af_len = pkt[4]
        return 5 + af_len
    return 4  # adaptation_control == 1


def parse_pat_pmt_pids(pkt: bytes) -> list[tuple[int, int]]:
    """Given a PAT packet (PUSI set, table_id 0x00), pull out the
    (program_number, pmt_pid) pairs."""
    off = get_payload_offset(pkt)
    if off is None or off >= len(pkt):
        return []
    pointer = pkt[off]
    p = off + 1 + pointer
    if p >= len(pkt) or pkt[p] != 0x00:
        return []
    section_len = ((pkt[p + 1] & 0x0f) << 8) | pkt[p + 2]
    prog_start = p + 3 + 5            # skip section header
    prog_end   = p + 3 + section_len - 4   # last 4 bytes are CRC
    pmt_pids = []
    q = prog_start
    while q + 4 <= prog_end and q + 4 <= len(pkt):
        prog_num = (pkt[q] << 8) | pkt[q + 1]
        pid      = ((pkt[q + 2] & 0x1f) << 8) | pkt[q + 3]
        if prog_num != 0:    # 0 = network_PID
            pmt_pids.append((prog_num, pid))
        q += 4
    return pmt_pids


def is_valid_pat(pkt: bytes) -> bool:
    """Heuristic check: looks like a real PAT (not garbage)."""
    if has_tei(pkt) or not has_pusi(pkt):
        return False
    off = get_payload_offset(pkt)
    if off is None or off + 4 >= len(pkt):
        return False
    pointer = pkt[off]
    p = off + 1 + pointer
    if p >= len(pkt) - 3:
        return False
    return pkt[p] == 0x00  # table_id for PAT


def is_valid_pmt(pkt: bytes) -> bool:
    if has_tei(pkt) or not has_pusi(pkt):
        return False
    off = get_payload_offset(pkt)
    if off is None or off + 4 >= len(pkt):
        return False
    pointer = pkt[off]
    p = off + 1 + pointer
    if p >= len(pkt) - 3:
        return False
    return pkt[p] == 0x02  # table_id for PMT


def main() -> int:
    stdin  = sys.stdin.buffer
    stdout = sys.stdout.buffer

    cached_pat: bytes | None = None
    cached_pmt: dict[int, bytes] = {}      # pmt_pid -> packet
    pmt_pids: set[int] = set()
    last_pat_seen: int = 0
    last_pmt_seen: dict[int, int] = {}     # pmt_pid -> pkt_counter
    pat_inject_cc: int = 0
    pmt_inject_cc: dict[int, int] = {}

    pkt_counter = 0
    bytes_in    = 0
    bytes_out   = 0
    pat_inj     = 0
    pmt_inj     = 0
    resync_count = 0
    t0 = time.monotonic()
    last_log = t0

    # Read into a buffer, slice 188-byte packets, resync on sync byte.
    buf = bytearray()
    READ_CHUNK = 65536
    try:
        while True:
            chunk = stdin.read(READ_CHUNK)
            if not chunk:
                break
            buf.extend(chunk)
            bytes_in += len(chunk)

            while len(buf) >= PKT_SIZE:
                if buf[0] != SYNC_BYTE:
                    # Resync — find next plausible sync.
                    found = -1
                    scan_end = min(len(buf) - PKT_SIZE, 1024)
                    for i in range(1, scan_end + 1):
                        if buf[i] == SYNC_BYTE:
                            # Look ahead: is buf[i + 188] also sync?
                            if i + 2 * PKT_SIZE <= len(buf):
                                if buf[i + PKT_SIZE] == SYNC_BYTE:
                                    found = i
                                    break
                            else:
                                found = i  # too short to confirm, take it
                                break
                    if found < 0:
                        # Drop everything up to last byte; will retry on more data
                        del buf[:max(1, len(buf) - PKT_SIZE)]
                        resync_count += 1
                        break
                    del buf[:found]
                    resync_count += 1
                    continue

                pkt = bytes(buf[:PKT_SIZE])
                del buf[:PKT_SIZE]
                pkt_counter += 1

                pid = parse_pid(pkt)

                # Cache valid PAT
                if pid == PAT_PID and is_valid_pat(pkt):
                    cached_pat = pkt
                    last_pat_seen = pkt_counter
                    # Refresh the set of PMT PIDs.
                    new_pids = {p for _, p in parse_pat_pmt_pids(pkt)}
                    if new_pids:
                        pmt_pids = new_pids

                # Cache valid PMT(s)
                elif pid in pmt_pids and is_valid_pmt(pkt):
                    cached_pmt[pid] = pkt
                    last_pmt_seen[pid] = pkt_counter

                # Warmup gate: hold output until we've cached PAT+PMT once.
                warm = (cached_pat is not None and len(cached_pmt) > 0)
                if not WARMUP_REQUIRE_PSI or warm:
                    stdout.write(pkt)
                    bytes_out += PKT_SIZE

                # Periodic check: inject PSI.
                if (pkt_counter % CHECK_INTERVAL_PKTS == 0
                        and cached_pat is not None):
                    pat_due = (INJECT_MODE == "always"
                               or pkt_counter - last_pat_seen > MISS_THRESHOLD_PKTS)
                    if pat_due:
                        pat_inject_cc = (pat_inject_cc + 1) & 0x0f
                        out_pat = set_continuity_counter(cached_pat,
                                                         pat_inject_cc)
                        stdout.write(out_pat)
                        bytes_out += PKT_SIZE
                        pat_inj += 1
                        last_pat_seen = pkt_counter
                    for pmt_pid, pmt_pkt in cached_pmt.items():
                        pmt_due = (INJECT_MODE == "always"
                                   or (pkt_counter - last_pmt_seen.get(pmt_pid, 0)
                                       > MISS_THRESHOLD_PKTS))
                        if GROUPED_INJECT and pat_due:
                            pmt_due = True   # always emit PMT right after PAT
                        if pmt_due:
                            cc = (pmt_inject_cc.get(pmt_pid, 0) + 1) & 0x0f
                            pmt_inject_cc[pmt_pid] = cc
                            out_pmt = set_continuity_counter(pmt_pkt, cc)
                            stdout.write(out_pmt)
                            bytes_out += PKT_SIZE
                            pmt_inj += 1
                            last_pmt_seen[pmt_pid] = pkt_counter

                # Flush periodically so player sees data promptly.
                if pkt_counter % 256 == 0:
                    try: stdout.flush()
                    except BrokenPipeError: return 0

                # Stats line every 5 s.
                now = time.monotonic()
                if now - last_log >= 5.0:
                    elapsed = now - t0
                    rate = bytes_in / max(1.0, elapsed) / 1e6
                    LOG.write(
                        f"[psi_repair] t={elapsed:6.1f}s  in={bytes_in/1e6:.1f}MB "
                        f"({rate:.2f}MB/s)  pkts={pkt_counter}  "
                        f"pat_inj={pat_inj}  pmt_inj={pmt_inj}  "
                        f"pmt_pids={sorted(pmt_pids)}  resync={resync_count}\n")
                    LOG.flush()
                    last_log = now
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        pass
    finally:
        try: stdout.flush()
        except Exception: pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
