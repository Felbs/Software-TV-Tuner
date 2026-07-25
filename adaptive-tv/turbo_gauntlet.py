#!/usr/bin/env python3
"""turbo_gauntlet.py - measure the SHIPPED Turbo 2b on the frozen-capture rail.

Blueprint law: stage 3 (BCJR) is built only if stage 2 shows iteration gain that
saturates on reliability quality. That measurement never ran. This gauntlet:
  * A/B per capture: erasure+SOVA with STVT_TURBO=0 vs 1 (everything else fixed)
  * captures span the cliff: deep-fail (12.8), under (14.7), at (15.2-15.6),
    clean control (16.1), a VHF breather, and the 7/10 drizzle specimen
  * one SELFTEST run (no-pin re-decode agreement) = the stage-3 gate metric
Scoring = chain_lab's honest rail (seq-headers - 0.05*ffmpeg-null-sink errors).
Adopt-law: wins somewhere, regresses nowhere.
"""
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import chain_lab

CAPS = Path(r"Z:\src\adaptive-tv\lab\captures")
PICKS = [
    "night_philips_rf21_mer12.8_2237.cs16",   # deep fail - stampede-gate territory
    "night_philips_rf21_mer14.7_2315.cs16",   # under the cliff - turbo's home turf
    "night_philips_rf21_mer15.2_2253.cs16",   # right at the cliff
    "night_philips_rf21_mer15.6_2228.cs16",   # just above
    "night_philips_rf21_mer16.1_2222.cs16",   # clean control - regression check
    "night_discone_rf7_mer14.7_2104.cs16",    # VHF breather under cliff
    "fox_drizzle_20260710.cs16",              # the 7/10 drizzle disease specimen
]
ARMS = {
    "off": {"STVT_RS": "erasure", "STVT_SOVA": "1", "STVT_TURBO": "0"},
    "on":  {"STVT_RS": "erasure", "STVT_SOVA": "1", "STVT_TURBO": "1"},
}
TURBO_RE = re.compile(r"\[(?:turbo|rs_erasure)\][^\n]*", re.I)


def turbo_lines(tag):
    log = chain_lab.LAB / f"{tag}_{chain_lab.os.getpid()}.log"
    try:
        txt = log.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return TURBO_RE.findall(txt)[-8:]


def main():
    results = {}
    t0 = time.time()
    for cap in PICKS:
        iq = CAPS / cap
        if not iq.exists():
            print(f"[gauntlet] MISSING {cap}", flush=True)
            continue
        row = {}
        for arm, ov in ARMS.items():
            tag = f"tg_{cap.split('_mer')[0][-8:]}_{arm}".replace(".", "")
            try:
                m = chain_lab.replay(iq, ov, tag=tag)
            except Exception as e:
                print(f"[gauntlet] {cap} {arm}: REPLAY ERR {e}", flush=True)
                m = {"headers": -1, "err_lines": -1, "score": -1, "mer_db": 0}
            row[arm] = m
            tl = turbo_lines(tag)
            print(f"[gauntlet] {cap:44s} {arm:3s} headers={m.get('headers')} "
                  f"err={m.get('err_lines')} score={m.get('score')} "
                  f"mer={m.get('mer_db')}", flush=True)
            for ln in tl:
                print(f"           {ln}", flush=True)
        results[cap] = row
    # the stage-3 gate: selftest (no-pin re-decode agreement) on the home-turf cap
    st_cap = CAPS / PICKS[1]
    if st_cap.exists():
        print("[gauntlet] SELFTEST (stage-3 gate) on", PICKS[1], flush=True)
        try:
            m = chain_lab.replay(st_cap, {**ARMS["on"], "STVT_TURBO_SELFTEST": "1"},
                                 tag="tg_selftest")
            for ln in turbo_lines("tg_selftest"):
                print(f"           {ln}", flush=True)
        except Exception as e:
            print(f"[gauntlet] selftest ERR {e}", flush=True)
    # verdict table
    print("\n==== TURBO 2B GAUNTLET ====", flush=True)
    print(f"{'capture':46s} {'off:hdr/err':>14s} {'on:hdr/err':>14s} {'d_score':>8s}")
    wins = losses = 0
    for cap, row in results.items():
        o, n = row.get("off", {}), row.get("on", {})
        ds = (n.get("score", 0) or 0) - (o.get("score", 0) or 0)
        wins += ds > 0.5
        losses += ds < -0.5
        print(f"{cap:46s} {o.get('headers')}/{o.get('err_lines'):>6} "
              f"{n.get('headers')}/{n.get('err_lines'):>6} {ds:8.1f}", flush=True)
    print(f"VERDICT: wins={wins} losses={losses} "
          f"(adopt-law: wins somewhere, regresses nowhere) "
          f"elapsed {(time.time()-t0)/60:.0f} min", flush=True)
    Path(HERE / "lab" / "turbo_gauntlet.json").write_text(
        json.dumps(results, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
