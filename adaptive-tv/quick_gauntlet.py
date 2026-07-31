"""quick_gauntlet.py — 10-minute paired A/B verdict for chain changes.

The cube's accuracy came from repetition over hours; the gauntlet gets
it from PAIRING: arms alternate ABAB on the same channel, so slow
propagation drift cancels inside each pair. Three regime channels:

    healthy   RF34 rabbit   (must-not-regress sentinel)
    cliff     RF21 rabbit   (where changes actually matter)
    patient   RF7 discone   (marginal VHF — the frontier)

Usage:
    python quick_gauntlet.py --a "STVT_RS_ERASURES=0" \
                             --b "STVT_RS_ERASURES=14" [--rounds 2]
Metrics per sample: median MER, seq headers, rs5 bad-packet fraction.
Verdict per channel: paired deltas; overall: sign consistency.
"""
import argparse
import os
import time

import overnight_cube as oc

# Recast 2026-07-07 evening (three-antenna era): patient was RF7 on
# ANT-A — now the moved rabbit's dead floor (8.5, uninformative).
# Discone-C RF7 is the honest marginal cell (14-15.3, breathing).
CELLS = [
    ("healthy", 34, "Antenna B", 2, 32),
    ("cliff",   21, "Antenna B", 2, 26),
    ("patient",  7, "Antenna C", 5, 32),
]
SECS = 40


def run_arm(envs, rf, antenna, rfg, ifgr):
    save = dict(os.environ)
    os.environ.update(envs)
    try:
        return oc.sample(rf, antenna, rfg, ifgr, secs=SECS)
    finally:
        os.environ.clear()
        os.environ.update(save)


def parse_env(s):
    out = {}
    for kv in s.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def fmt(s):
    bad = s.get("rs5_bad")
    pk = s.get("rs5_pkts")
    frac = f"{bad/pk:.1%}" if bad is not None and pk else "--"
    return f"MER {s.get('mer_med')} hdr {s.get('hdr')} bad5 {frac}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="KEY=V,KEY=V for arm A")
    ap.add_argument("--b", required=True, help="KEY=V,KEY=V for arm B")
    ap.add_argument("--rounds", type=int, default=2)
    args = ap.parse_args()
    ea, eb = parse_env(args.a), parse_env(args.b)
    t0 = time.time()

    wins = {"A": 0, "B": 0, "tie": 0}
    for name, rf, ant, rfg, ifgr in CELLS:
        print(f"== {name}: RF{rf} {ant} ==", flush=True)
        d_hdr = []
        d_mer = []
        for r in range(args.rounds):
            sa = run_arm(ea, rf, ant, rfg, ifgr)
            sb = run_arm(eb, rf, ant, rfg, ifgr)
            print(f"  r{r+1} A: {fmt(sa)}")
            print(f"  r{r+1} B: {fmt(sb)}", flush=True)
            if sa.get("hdr") is not None and sb.get("hdr") is not None:
                d_hdr.append(sb["hdr"] - sa["hdr"])
            if sa.get("mer_med") and sb.get("mer_med"):
                d_mer.append(sb["mer_med"] - sa["mer_med"])
        mh = sum(d_hdr) / len(d_hdr) if d_hdr else 0
        mm = sum(d_mer) / len(d_mer) if d_mer else 0
        v = "B" if mh > 5 else ("A" if mh < -5 else "tie")
        wins[v] += 1
        print(f"  -> paired: hdr {mh:+.0f}/sample, MER {mm:+.2f} dB, "
              f"verdict {v}")

    print(f"\nGAUNTLET ({time.time()-t0:.0f}s): "
          f"B wins {wins['B']}, A wins {wins['A']}, ties {wins['tie']}")
    print("rule: adopt B only if it wins somewhere and regresses nowhere.")


if __name__ == "__main__":
    main()
