"""race_wiener.py — cold-start vs Wiener-seeded convergence race.

Leg 1 (cold): chain on the target with CIR dump, no tap cache.
        -> measures the channel + records the LMS convergence trajectory.
Solve:  wiener_seed converts the measured CIR into analytic taps.
Leg 2 (seeded): same chain, warm-started from the Wiener taps.
Verdict: fs_err trajectories + time-to-cliff + headers, side by side.

    python race_wiener.py --rf 7 --antenna "Antenna A" --rfg 5 --ifgr 32
"""
import argparse
import math
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
LAB = HERE / "wiener_lab"
CHAINLOG = HERE / "cube_chain.log"
PY = sys.executable
RE_EQ = re.compile(r"\[eq-long t=\s*([\d.]+)s\].*?fs_err_rms=([\d.]+)")

import overnight_cube as oc


def trajectory():
    txt = CHAINLOG.read_text(errors="ignore")
    pts = [(float(t), 20 * math.log10(5.0 / float(e)))
           for t, e in RE_EQ.findall(txt) if float(e) > 0]
    return pts


def timeto(pts, mer):
    for t, m in pts:
        if m >= mer:
            return t
    return None


def leg(rf, antenna, rfg, ifgr, secs, cache_dir, cir_dump=None):
    env_save = dict(os.environ)
    if cir_dump:
        os.environ["STVT_EQ_CIR"] = "1"
        os.environ["STVT_EQ_CIR_DUMP"] = str(cir_dump)
    if cache_dir:
        os.environ["STVT_EQ_TAP_CACHE"] = str(cache_dir)
    else:
        os.environ.pop("STVT_EQ_TAP_CACHE", None)
    try:
        s = oc.sample(rf, antenna, rfg, ifgr, secs=secs)
    finally:
        os.environ.clear()
        os.environ.update(env_save)
    return s, trajectory()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, required=True)
    ap.add_argument("--antenna", default="Antenna B")
    ap.add_argument("--rfg", type=int, default=3)
    ap.add_argument("--ifgr", type=int, default=40)
    ap.add_argument("--secs", type=int, default=45)
    ap.add_argument("--snr-db", type=float, default=16.0)
    args = ap.parse_args()

    LAB.mkdir(exist_ok=True)
    ant_key = args.antenna.replace(" ", "")
    cir = LAB / f"cir_race_rf{args.rf}.bin"
    seed_dir = LAB / "race_seed"
    seed_dir.mkdir(exist_ok=True)
    seed = seed_dir / f"taps_{ant_key}_rf{args.rf}.bin"
    if seed.exists():
        seed.unlink()

    print(f"== LEG 1: cold start, RF{args.rf} {args.antenna} ==", flush=True)
    s1, tr1 = leg(args.rf, args.antenna, args.rfg, args.ifgr,
                  args.secs, cache_dir=None, cir_dump=cir)
    print(f"   cold: MER {s1.get('mer_med')} hdr {s1.get('hdr')}")

    print("== SOLVE ==", flush=True)
    r = subprocess.run([PY, str(HERE / "wiener_seed.py"),
                        "--cir", str(cir), "--taps-out", str(seed),
                        "--snr-db", str(args.snr_db), "--shift", "55"],
                       capture_output=True, text=True)
    print(r.stdout.strip())
    if not seed.exists():
        sys.exit("no seed produced — aborting leg 2")

    print(f"== LEG 2: Wiener-seeded ==", flush=True)
    s2, tr2 = leg(args.rf, args.antenna, args.rfg, args.ifgr,
                  args.secs, cache_dir=seed_dir, cir_dump=None)
    print(f"   seeded: MER {s2.get('mer_med')} hdr {s2.get('hdr')}")

    print("\n== VERDICT ==")
    for name, pts, s in (("cold  ", tr1, s1), ("seeded", tr2, s2)):
        t15 = timeto(pts, 15.0)
        first = pts[0] if pts else None
        print(f"  {name}: first telem {first}, "
              f"t->15dB {t15 if t15 is not None else 'never'}, "
              f"headers {s.get('hdr')}, med {s.get('mer_med')}")
    e1 = [m for t, m in tr1 if t < 10]
    e2 = [m for t, m in tr2 if t < 10]
    if e1 and e2:
        print(f"  first-10s mean MER: cold {np.mean(e1):.2f} "
              f"vs seeded {np.mean(e2):.2f}  "
              f"({np.mean(e2)-np.mean(e1):+.2f} dB)")


if __name__ == "__main__":
    main()
