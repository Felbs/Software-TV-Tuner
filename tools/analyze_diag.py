#!/usr/bin/env python3
"""Analyze byte-stream taps captured by tv_live_diag.py.

Inputs (all in /tmp/diag_taps):
  dei_out.bin     — RS input bytes (207 bytes per RS code block)
  rs_out.bin      — RS output bytes (188 bytes per TS packet)
  rs_meta.bin     — RS plinfo stream (4 bytes per packet)
  derand_out.bin  — derandomized bytes (188 bytes per TS packet)
  depad_out.bin   — pre-TEIScrub byte stream (188-byte TS packets)

Reports:
  • RS correction rate (bytes RS changed per block)
  • RS failure rate (TEI=1 packets in pre-scrub depad output)
  • Distinct PIDs in depad output (= post-RS bit-error corruption)
  • Cross-stage consistency checks
"""

from collections import Counter
from pathlib import Path

DIAG_DIR = Path("/tmp/diag_taps")
MAX_BYTES = 50 * 1024 * 1024   # cap at 50MB per file to keep memory sane


def load(name):
    p = DIAG_DIR / name
    if not p.exists():
        return None
    return p.read_bytes()[:MAX_BYTES]


def hist_show(counter, label, top_n=8, total=None):
    if total is None:
        total = sum(counter.values())
    if total == 0:
        print(f"  {label}: (empty)")
        return
    print(f"  {label}: {total} total")
    for k, v in counter.most_common(top_n):
        print(f"    {k}: {v} ({100*v/total:.2f}%)")


def analyze_depad(buf):
    print("=" * 60)
    print("DEPAD OUTPUT — pre-TEIScrub TS packet stream")
    print("=" * 60)
    if not buf:
        print("  no data")
        return
    # Locate first aligned 0x47
    start = -1
    for i in range(188):
        if (buf[i] == 0x47 and buf[i+188] == 0x47 and
                len(buf) > i + 188*3 and buf[i+376] == 0x47):
            start = i
            break
    if start < 0:
        print("  could not find aligned sync — depad output is not TS-framed?")
        return
    print(f"  alignment found at offset {start}")
    n_pkts = (len(buf) - start) // 188
    tei = 0
    pusi = 0
    pids = Counter()
    null_pkts = 0
    for k in range(n_pkts):
        pkt = buf[start + k*188 : start + (k+1)*188]
        if pkt[0] != 0x47:
            continue
        tei += (pkt[1] >> 7) & 1
        pusi += (pkt[1] >> 6) & 1
        pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
        pids[pid] += 1
        if pid == 0x1FFF:
            null_pkts += 1
    print(f"  packets analyzed     : {n_pkts}")
    print(f"  TEI=1 packets        : {tei}  ({100*tei/n_pkts:.2f}%)  ← real RS-failure rate")
    print(f"  PUSI=1 packets       : {pusi}  ({100*pusi/n_pkts:.2f}%)")
    print(f"  NULL packets (PID=0x1FFF): {null_pkts}")
    print(f"  distinct PIDs        : {len(pids)}  (real ATSC: 5-15)")
    print(f"  top 8 PIDs:")
    for pid, ct in pids.most_common(8):
        flag = ""
        if pid == 0: flag = "(PAT)"
        elif pid == 0x1FFB: flag = "(PSIP)"
        elif pid == 0x1FFF: flag = "(NULL)"
        print(f"    PID 0x{pid:04x} ({pid:5d}): {ct} pkts {flag}")
    # Estimate "ghost" PID rate
    real_threshold = max(1, n_pkts // 1000)   # ≥0.1% of packets = real
    real = sum(1 for p, c in pids.items() if c >= real_threshold)
    ghosts = len(pids) - real
    ghost_pkts = sum(c for p, c in pids.items() if c < real_threshold)
    print(f"  est. real PIDs (≥{real_threshold} pkts each): {real}")
    print(f"  est. ghost PIDs       : {ghosts}  carrying {ghost_pkts} pkts ({100*ghost_pkts/n_pkts:.2f}%)")


def analyze_rs_correction(dei_buf, rs_buf):
    print()
    print("=" * 60)
    print("RS CORRECTION — byte diff between dei_out (input) and rs_out (output)")
    print("=" * 60)
    if not dei_buf or not rs_buf:
        print("  missing buffer")
        return
    # ATSC RS(207, 187): 207-byte input block → 187 data bytes out.
    # gr-dtv atsc_rs_decoder prepends sync byte 0x47, so output is 188 bytes.
    # Input layout (per ATSC standard): 187 data + 20 parity = 207
    # Output: 0x47 + 187 corrected data = 188
    # So rs_out[k*188+1 .. k*188+188] should be close to dei_in[k*207 .. k*207+187].
    n_blocks = min(len(dei_buf) // 207, len(rs_buf) // 188)
    print(f"  RS blocks analyzed   : {n_blocks}")
    diff_hist = Counter()
    total_byte_diffs = 0
    max_diff_in_block = 0
    rs_giveup = 0   # if RS gave up, output may equal input (or be marked TEI)
    for k in range(n_blocks):
        din  = dei_buf[k*207     : k*207 + 187]   # 187 data bytes (pre-parity)
        dout = rs_buf [k*188 + 1 : k*188 + 188]   # 187 bytes after sync byte
        diffs = sum(1 for a, b in zip(din, dout) if a != b)
        diff_hist[diffs] += 1
        total_byte_diffs += diffs
        if diffs > max_diff_in_block:
            max_diff_in_block = diffs
    avg_diff = total_byte_diffs / max(1, n_blocks)
    print(f"  avg bytes RS changed/block: {avg_diff:.2f}")
    print(f"  max bytes RS changed/block: {max_diff_in_block}")
    print(f"  blocks needing 0 corrections      : {diff_hist.get(0, 0)} ({100*diff_hist.get(0,0)/n_blocks:.1f}%)")
    print(f"  blocks needing 1-10 corrections   : {sum(v for k, v in diff_hist.items() if 1 <= k <= 10)} ({100*sum(v for k, v in diff_hist.items() if 1 <= k <= 10)/n_blocks:.1f}%)")
    print(f"  blocks needing 11-50 corrections  : {sum(v for k, v in diff_hist.items() if 11 <= k <= 50)} ({100*sum(v for k, v in diff_hist.items() if 11 <= k <= 50)/n_blocks:.1f}%)")
    print(f"  blocks with >50 byte diffs        : {sum(v for k, v in diff_hist.items() if k > 50)} ← RS giveup, output = uncorrected")
    print(f"  ATSC RS limit                     : 10 byte errors/block")
    print(f"  → if avg > 5, equalizer/viterbi is producing too many errors")


def analyze_rs_meta(meta_buf):
    print()
    print("=" * 60)
    print("RS METADATA (plinfo) — 4 bytes per packet")
    print("=" * 60)
    if not meta_buf:
        print("  missing")
        return
    n_pkts = len(meta_buf) // 4
    print(f"  packets covered      : {n_pkts}")
    # Show first 16 raw entries to understand format
    print(f"  first 16 entries (hex bytes):")
    for k in range(min(16, n_pkts)):
        b = meta_buf[k*4:(k+1)*4]
        print(f"    [{k:3d}]  {b.hex(' ')}")
    # Byte position histograms — find which byte tracks errors
    by_pos = [Counter() for _ in range(4)]
    for k in range(n_pkts):
        for i in range(4):
            by_pos[i][meta_buf[k*4 + i]] += 1
    print(f"  byte position 0 distinct values: {len(by_pos[0])}")
    print(f"  byte position 1 distinct values: {len(by_pos[1])}")
    print(f"  byte position 2 distinct values: {len(by_pos[2])}")
    print(f"  byte position 3 distinct values: {len(by_pos[3])}")
    for i in range(4):
        most = by_pos[i].most_common(3)
        print(f"  pos {i} top vals: {[(hex(v), c) for v, c in most]}")


def main():
    print(f"Loading taps from {DIAG_DIR}")
    files = {}
    for name in ("dei_out.bin", "rs_out.bin", "rs_meta.bin", "derand_out.bin", "depad_out.bin"):
        p = DIAG_DIR / name
        if p.exists():
            sz = p.stat().st_size
            files[name] = sz
            print(f"  {name:20s} {sz:>12,d} bytes")
        else:
            print(f"  {name:20s} MISSING")
    print()

    depad = load("depad_out.bin")
    dei   = load("dei_out.bin")
    rs    = load("rs_out.bin")
    meta  = load("rs_meta.bin")

    analyze_depad(depad)
    analyze_rs_correction(dei, rs)
    analyze_rs_meta(meta)


if __name__ == "__main__":
    main()
