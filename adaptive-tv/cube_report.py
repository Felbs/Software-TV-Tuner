"""cube_report.py — render the overnight Antenna x Channel x Time cube.

Reads cube_log.jsonl -> per-(rf, antenna) hourly MER medians, decode
counts, best hour, and the headline: which antenna owns which channel.

    python cube_report.py [cube_log.jsonl]
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).parent / "cube_log.jsonl")

rows = []
events = []
for line in path.read_text(encoding="utf-8").splitlines():
    try:
        o = json.loads(line)
    except json.JSONDecodeError:
        continue
    (events if "event" in o else rows).append(o)

if not rows:
    sys.exit("no samples yet")

# (rf, ant) -> list of (hour, mer, hdr, verdict)
cell = defaultdict(list)
for r in rows:
    if r.get("rf") is None:
        continue
    hh = int(r["t"][:2])
    cell[(r["rf"], r["ant"])].append(
        (hh, r.get("mer_med"), r.get("hdr", 0), r.get("verdict", "")))

ants = sorted({k[1] for k in cell})
rfs = sorted({k[0] for k in cell})

print(f"cube report - {len(rows)} samples, "
      f"{rows[0]['t']} .. {rows[-1]['t']}\n")

print(f"{'RF':>4} {'antenna':>8} {'n':>3} {'medMER':>7} {'bestMER':>8} "
      f"{'best@':>6} {'decodes':>8} {'verdict-run'}")
owner = {}
for rf in rfs:
    for ant in ants:
        pts = cell.get((rf, ant))
        if not pts:
            continue
        mers = sorted(p[1] for p in pts if p[1] is not None)
        med = mers[len(mers) // 2] if mers else None
        best = max((p for p in pts if p[1] is not None),
                   key=lambda p: p[1], default=None)
        dec = sum(1 for p in pts if p[3] == "DECODE")
        run = "".join({"DECODE": "D", "AT-CLIFF": "C", "CLOSE": "c",
                       "FLOOR": ".", "PILOT-ONLY": "p",
                       "NO-FS": "_"}.get(p[3], "?") for p in pts)
        print(f"{rf:>4} {ant:>8} {len(pts):>3} "
              f"{med if med is not None else '--':>7} "
              f"{best[1] if best else '--':>8} "
              f"{(str(best[0]) + 'h') if best else '--':>6} "
              f"{dec:>8} {run}")
        score = (dec, med or -99)
        if rf not in owner or score > owner[rf][0]:
            owner[rf] = (score, ant)
print("\nchannel ownership (auto-antenna selection map):")
for rf in rfs:
    (dec, med), ant = owner[rf]
    print(f"  RF{rf:<3} -> {ant:8s} ({dec} decodes, median MER {med})")

# machine-readable map: the E2 auto-selection consumer reads this.
# time is an official tuning knob (ratified 2026-07-06): per (rf, ant)
# hourly MER medians let a tuner pick antenna AND hour.
hourly = defaultdict(lambda: defaultdict(list))
for (rf, ant), pts in cell.items():
    for hh, mer, hdr, v in pts:
        if mer is not None:
            hourly[f"rf{rf}|{ant}"][hh].append((mer, v == "DECODE"))
mapobj = {"generated": rows[-1]["t"], "channels": {}}
for rf in rfs:
    (dec, med), ant = owner[rf]
    hours = {}
    for hh, lst in sorted(hourly.get(f"rf{rf}|{ant}", {}).items()):
        ms = sorted(m for m, _ in lst)
        hours[str(hh)] = {"mer": round(ms[len(ms) // 2], 2),
                          "decodes": sum(1 for _, d in lst if d)}
    best_hr = max(hours, key=lambda h: hours[h]["mer"]) if hours else None
    mapobj["channels"][str(rf)] = {
        "antenna": ant, "median_mer": med, "decodes": dec,
        "best_hour": best_hr, "hours": hours}
mp = path.parent / "cube_map.json"
mp.write_text(json.dumps(mapobj, indent=1), encoding="utf-8")
print(f"\nmap exported: {mp}")

catches = [e for e in events if e.get("event") == "TROPO-CATCH"]
trims = [e for e in events if e.get("event") == "trim-adopt"]
if catches:
    print("\nTROPO CATCHES:")
    for c in catches:
        print(f"  {c['t']} RF{c['rf']} on {c['ant']} MER {c['mer']}")
if trims:
    print("\ngain trims adopted:")
    for tr in trims:
        print(f"  {tr['t']} {tr['key']} -> {tr['gains']} (MER {tr['mer']})")
