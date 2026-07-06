"""cliff_curve.py — the true shape of the 8-VSB cliff, measured.

E4's FEC narration gives every cube sample a packet-survival rate
(rs5 last-5s window). Binned against median MER, that draws the real
transition curve — the "cliff" 15.2 dB is actually an S-curve whose
shape (width, center, tails) this tool measures from live data.

    python cliff_curve.py [cube_log.jsonl]
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).parent / "cube_log.jsonl")

bins = defaultdict(lambda: [0, 0, 0])   # mer_bin -> [samples, pkts, bad]
for line in path.read_text(encoding="utf-8").splitlines():
    try:
        o = json.loads(line)
    except json.JSONDecodeError:
        continue
    if "event" in o or o.get("mer_med") is None or "rs5_pkts" not in o:
        continue
    if o["rs5_pkts"] <= 0:
        continue
    b = round(o["mer_med"] * 2) / 2          # 0.5 dB bins
    bins[b][0] += 1
    bins[b][1] += o["rs5_pkts"]
    bins[b][2] += o["rs5_bad"]

if not bins:
    sys.exit("no FEC-annotated samples yet")

print(f"{'MER bin':>8} {'n':>4} {'pkt survival':>13}  curve")
for b in sorted(bins):
    n, pk, bad = bins[b]
    surv = 100.0 * (1 - bad / pk)
    bar = "#" * int(surv / 2.5)
    print(f"{b:>7.1f}  {n:>4} {surv:>12.2f}%  {bar}")
