"""belief_map.py — the map as a living belief state (Bayesian upgrade).

The lookup-table map broke the universal-tuner promise: history keyed
on antenna NAMES goes stale the moment hardware moves (2026-07-07: the
rabbit changed spot AND port twice in one day). Fix:

  * every (channel, antenna) cell = a POSTERIOR over expected MER:
    Normal-with-unknown-mean, observations weighted by exp(-age/tau)
  * HARDWARE EPOCHS: a hardware-change event multiplies prior variance
    for the affected antennas instead of erasing their history
  * selection = THOMPSON SAMPLING (panel side): draw from each belief,
    tune the winner — exploration emerges from uncertainty itself.

Reads cube_log.jsonl (+ hardware-epoch events), writes belief_map.json:
  {rf: {ant: {mean, sd, n_eff, last_seen}}}

Log a hardware change:
    python belief_map.py --epoch "rabbit moved to ANT-A, philips to B"
Rebuild beliefs:
    python belief_map.py
"""
import argparse
import json
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
LOG = HERE / "cube_log.jsonl"
OUT = HERE / "belief_map.json"

TAU_H = 18.0            # observation half-life ~12.5 h (tau 18 h)
EPOCH_INFLATE = 4.0     # sd multiplier applied across a hardware epoch
PRIOR_SD = 3.0          # a cell we know nothing about


def parse_when(tstr, seq_hint):
    """cube_log carries only HH:MM:SS — reconstruct rough absolute age
    using file order: entries are appended chronologically; we walk the
    file once and count day rollovers."""
    return seq_hint  # resolved by caller's rollover walk


def load_observations():
    rows = []
    epochs = []
    day = 0
    prev = None
    for line in LOG.read_text(encoding="utf-8").splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        t = o.get("t", "")
        if len(t) >= 8:
            if prev is not None and t < prev:
                day += 1                     # midnight rollover
            prev = t
        o["_day"] = day
        if o.get("event") == "hardware-epoch":
            epochs.append(o)
        elif o.get("rf") is not None and o.get("ant") \
                and o.get("mer_med") is not None and "event" not in o:
            rows.append(o)
        elif o.get("event") in ("rf9-ambush", "rf15-probe") \
                and o.get("mer_med") is not None and o.get("ant"):
            o["rf"] = o.get("rf", 9 if "rf9" in o["event"] else 15)
            rows.append(o)
    return rows, epochs, day


def build():
    rows, epochs, today = load_observations()
    if not rows:
        print("no observations")
        return
    # absolute-ish timestamp: day index + HH:MM:SS
    def abst(o):
        h, m, s = (int(x) for x in o["t"].split(":"))
        return o["_day"] * 86400 + h * 3600 + m * 60 + s
    now = max(abst(o) for o in rows)
    ep_times = [abst(e) for e in epochs]

    cells = defaultdict(lambda: {"w": 0.0, "wx": 0.0, "wx2": 0.0,
                                 "last": None, "epochs_crossed": 0})
    for o in rows:
        t = abst(o)
        age_h = (now - t) / 3600.0
        w = math.exp(-age_h / TAU_H)
        crossed = sum(1 for et in ep_times if et > t)
        w *= 0.25 ** crossed          # authority collapses across epochs
        c = cells[(o["rf"], o["ant"])]
        c["w"] += w
        c["wx"] += w * o["mer_med"]
        c["wx2"] += w * o["mer_med"] ** 2
        c["last"] = o["t"]
        c["epochs_crossed"] = max(c["epochs_crossed"], crossed)

    out = defaultdict(dict)
    for (rf, ant), c in cells.items():
        if c["w"] < 0.05:
            continue
        mean = c["wx"] / c["w"]
        var = max(0.25, c["wx2"] / c["w"] - mean ** 2)
        n_eff = c["w"]
        sd = math.sqrt(var / max(0.5, n_eff))
        sd = min(PRIOR_SD, sd * (EPOCH_INFLATE if c["epochs_crossed"]
                                 else 1.0))
        out[str(rf)][ant] = {"mean": round(mean, 2), "sd": round(sd, 2),
                             "n_eff": round(n_eff, 1), "last": c["last"]}
    OUT.write_text(json.dumps(out, indent=1))
    print(f"belief map: {sum(len(v) for v in out.values())} cells "
          f"({len(epochs)} hardware epochs on record) -> {OUT.name}")
    for rf in sorted(out, key=int):
        parts = []
        for ant, b in sorted(out[rf].items()):
            parts.append(f"{ant} {b['mean']}±{b['sd']}")
        print(f"  RF{rf:>3}: " + " | ".join(parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epoch", help="log a hardware change (text)")
    args = ap.parse_args()
    if args.epoch:
        ev = {"event": "hardware-epoch", "note": args.epoch,
              "t": datetime.now().strftime("%H:%M:%S")}
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")
        print("epoch logged:", args.epoch)
    build()


if __name__ == "__main__":
    main()
