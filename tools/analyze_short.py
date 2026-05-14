#!/usr/bin/env python3
"""Short analyzer — one-line summary suitable for piping into a comparison table.

Output (tab-separated):
  eq_name<TAB>n_pkts<TAB>tei_pct<TAB>n_real_pids<TAB>n_ghost_pids<TAB>ghost_pkt_pct
"""
import sys
from collections import Counter
from pathlib import Path

DIAG_DIR = Path("/tmp/diag_taps")
MAX = 50 * 1024 * 1024
SKIP_BYTES = 8 * 1024 * 1024   # skip first 8 MB (~3-4s) to drop pre-convergence

eq_name = sys.argv[1] if len(sys.argv) > 1 else "?"

p = DIAG_DIR / "depad_out.bin"
if not p.exists():
    print(f"{eq_name}\t-\t-\t-\t-\t-\tNO_DATA")
    sys.exit(0)

buf = p.read_bytes()[SKIP_BYTES : SKIP_BYTES + MAX]
if len(buf) < 188 * 100:
    print(f"{eq_name}\t-\t-\t-\t-\t-\tEMPTY")
    sys.exit(0)

# Find aligned 0x47
start = -1
for i in range(188):
    if (buf[i] == 0x47 and buf[i+188] == 0x47 and
            len(buf) > i + 376 and buf[i+376] == 0x47):
        start = i; break

if start < 0:
    print(f"{eq_name}\t-\t-\t-\t-\t-\tUNALIGNED")
    sys.exit(0)

n = (len(buf) - start) // 188
tei = 0
pids = Counter()
for k in range(n):
    pkt = buf[start + k*188 : start + (k+1)*188]
    if pkt[0] != 0x47:
        continue
    tei += (pkt[1] >> 7) & 1
    pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
    pids[pid] += 1

real_thresh = max(1, n // 1000)
real = sum(1 for p, c in pids.items() if c >= real_thresh)
ghosts = len(pids) - real
ghost_pkts = sum(c for p, c in pids.items() if c < real_thresh)

print(f"{eq_name}\t{n}\t{100*tei/n:.2f}\t{real}\t{ghosts}\t{100*ghost_pkts/n:.2f}\tOK")
