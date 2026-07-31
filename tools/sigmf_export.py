#!/usr/bin/env python3
"""sigmf_export.py - SigMF metadata sidecars for the rig's cs16 captures.

The PYSDR-harvest ledger item: our interleaved int16 IQ is already exactly
SigMF's ci16_le datatype - all that's missing is metadata. This writes a
NON-DESTRUCTIVE <name>.sigmf-meta next to each capture (SigMF v1 with
core:dataset naming the original .cs16 as a Non-Conforming Dataset), so every
existing tool path keeps working and the captures become portable/self-describing.

  python sigmf_export.py --dir Z:\\src\\adaptive-tv\\lab\\captures --rate 8e6 --kind atsc
  python sigmf_export.py --dir Z:\\src\\hamTuna\\lab\\cw_harvest  --rate 250e3 --kind cw
"""
import argparse
import json
import os
import re
import time
from pathlib import Path

# US ATSC RF channel -> center frequency (Hz): 6 MHz channels
def atsc_center(rf):
    if 2 <= rf <= 4:
        return (57 + (rf - 2) * 6) * 1e6
    if 5 <= rf <= 6:
        return (79 + (rf - 5) * 6) * 1e6
    if 7 <= rf <= 13:
        return (177 + (rf - 7) * 6) * 1e6
    if 14 <= rf <= 36:
        return (473 + (rf - 14) * 6) * 1e6
    return None


RF_RE = re.compile(r"rf(\d+)", re.I)
KHZ_RE = re.compile(r"cw_(\d+)_")     # harvester: cw_<khz>_<utc>.cs16


def meta_for(p: Path, rate: float, kind: str):
    m = {"global": {"core:datatype": "ci16_le", "core:sample_rate": rate,
                    "core:version": "1.0.0", "core:dataset": p.name,
                    "core:recorder": "STVT rig (RSPdx via SoapySDR)",
                    "core:description": ""},
         "captures": [{"core:sample_start": 0}], "annotations": []}
    freq = None
    if kind == "atsc":
        r = RF_RE.search(p.name)
        if r:
            freq = atsc_center(int(r.group(1)))
        m["global"]["core:description"] = f"ATSC 1.0 8-VSB capture ({p.name})"
    elif kind == "cw":
        r = KHZ_RE.search(p.name)
        if r:
            freq = float(r.group(1)) * 1e3
        m["global"]["core:description"] = f"HF CW band capture ({p.name})"
        side = p.with_suffix(".json")     # harvester sidecar -> annotations
        if side.exists():
            try:
                d = json.loads(side.read_text(encoding="utf-8"))
                ann = {"core:sample_start": 0,
                       "core:label": (d.get("text", "") or "")[:80]}
                if d.get("eye") is not None:
                    ann["stvt:eye_q"] = d["eye"]
                if d.get("verified_calls"):
                    ann["stvt:verified_calls"] = [v["call"] for v in d["verified_calls"]]
                m["annotations"].append(ann)
            except Exception:
                pass
    if freq:
        m["captures"][0]["core:frequency"] = freq
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p.stat().st_mtime))
    m["captures"][0]["core:datetime"] = ts
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--rate", type=float, required=True)
    ap.add_argument("--kind", choices=("atsc", "cw"), required=True)
    ap.add_argument("--min-age", type=float, default=120.0,
                    help="skip files newer than this (seconds) - never race a writer")
    a = ap.parse_args()
    root = Path(a.dir)
    done = skip = 0
    for p in sorted(root.glob("*.cs16")):
        mp = p.with_suffix(".sigmf-meta")
        if mp.exists() or (time.time() - p.stat().st_mtime) < a.min_age:
            skip += 1; continue
        mp.write_text(json.dumps(meta_for(p, a.rate, a.kind), indent=1),
                      encoding="utf-8")
        done += 1
    print(f"[sigmf] {root}: {done} metas written, {skip} skipped")


if __name__ == "__main__":
    main()
