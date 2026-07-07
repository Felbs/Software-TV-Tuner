"""bo_harvest.py — Bayesian optimization of WATCHABILITY itself.

The play-path campaign gave us the objective we never had: harvested
true-GOPs per dwell (harvest_player's count of fully-surviving GOPs).
This tool runs Bayesian optimization — Gaussian-process surrogate +
expected-improvement acquisition, the same math that cracked the gain
problem — over the chain's knob space, maximizing that objective on a
breathing channel.

Knobs (6-D, mixed):
    BETA          FS-LMS step            log-uniform 1e-5 .. 5e-4
    FS_AVG_DEPTH  coherent FS averaging  {1, 2, 4, 8}
    DD_MU         decision tracking      {0, 1e-6, 5e-6, 2e-5}
    DFE           feedback section       {0, 1}
    IFGR          gain reduction         22 .. 44
    QUAL_RMS      quality-reset bar      {0, 6, 8, 10}

Each evaluation: 75 s dwell -> harvest count (noisy, expensive — the
textbook BO regime). ~25 evaluations ≈ 35 min.

    python bo_harvest.py --rf 21 --ant "Antenna B" --rfg 2 [--evals 25]
"""
import argparse
import json
import math
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import overnight_cube as oc
from harvest_player import harvest

LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")

DIMS = 6


def decode(u):
    """[0,1]^6 -> concrete knob dict."""
    beta = 10 ** (np.interp(u[0], [0, 1], [math.log10(1e-5),
                                           math.log10(5e-4)]))
    depth = [1, 2, 4, 8][min(3, int(u[1] * 4))]
    ddmu = [0.0, 1e-6, 5e-6, 2e-5][min(3, int(u[2] * 4))]
    dfe = 1 if u[3] >= 0.5 else 0
    ifgr = int(round(np.interp(u[4], [0, 1], [22, 44])))
    qual = [0, 6, 8, 10][min(3, int(u[5] * 4))]
    return {"STVT_EQ_BETA": f"{beta:.2e}",
            "STVT_EQ_FS_AVG_DEPTH": str(depth),
            "STVT_EQ_DD_MU": f"{ddmu:g}",
            "STVT_EQ_DFE": str(dfe),
            "_IFGR": ifgr,
            "STVT_EQ_QUALITY_BAD_RMS": str(qual)}


def evaluate(u, rf, antenna, rfg, secs):
    knobs = decode(u)
    ifgr = knobs.pop("_IFGR")
    old = oc.chain_env

    def patched(rf_, a, g, i, _k=knobs, _o=old):
        e = _o(rf_, a, g, i)
        e.update(_k)
        return e
    oc.chain_env = patched
    try:
        s = oc.sample(rf, antenna, rfg, ifgr, secs=secs)
    finally:
        oc.chain_env = old
    tmp = HERE / "lab" / "bo_eval.ts"
    try:
        shutil.copyfile(LIVE, tmp)
        got, dropped = harvest(tmp, HERE / "lab" / "bo_eval.harvested.ts",
                               verbose=False)
    except Exception:
        got, dropped = 0, 0
    return got, s, {**knobs, "IFGR": ifgr}


# ── minimal noise-aware GP + expected improvement ──────────────────
def gp_ei(X, y, cand, noise=1.0):
    X = np.array(X)
    y = np.array(y, float)
    mu0 = y.mean()
    ys = y - mu0
    ell = 0.35
    K = np.exp(-0.5 * ((X[:, None, :] - X[None, :, :]) ** 2).sum(-1)
               / ell ** 2)
    K += np.eye(len(X)) * (noise ** 2 / max(1e-9, y.var() + 1e-9) + 1e-6)
    Ks = np.exp(-0.5 * ((cand[:, None, :] - X[None, :, :]) ** 2).sum(-1)
                / ell ** 2)
    Kinv = np.linalg.inv(K)
    m = Ks @ Kinv @ ys + mu0
    v = np.clip(1.0 - np.einsum("ij,jk,ik->i", Ks, Kinv, Ks), 1e-9, None)
    sd = np.sqrt(v) * (y.std() + 1e-9)
    best = y.max()
    z = (m - best) / sd
    phi = np.exp(-0.5 * z ** 2) / math.sqrt(2 * math.pi)
    Phi = 0.5 * (1 + np.vectorize(math.erf)(z / math.sqrt(2)))
    return (m - best) * Phi + sd * phi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, required=True)
    ap.add_argument("--ant", default="Antenna B")
    ap.add_argument("--rfg", type=int, default=2)
    ap.add_argument("--evals", type=int, default=25)
    ap.add_argument("--secs", type=int, default=75)
    args = ap.parse_args()

    rng = np.random.default_rng(9)
    X, y = [], []
    log = HERE / "cube_log.jsonl"
    print(f"BO harvest tuner: RF{args.rf} {args.ant}, "
          f"{args.evals} evaluations", flush=True)
    for i in range(args.evals):
        if i < 6:
            u = rng.random(DIMS)                     # exploration seeds
        else:
            cand = rng.random((400, DIMS))
            ei = gp_ei(X, y, cand, noise=1.5)
            u = cand[int(np.argmax(ei))]
        got, s, knobs = evaluate(u, args.rf, args.ant, args.rfg, args.secs)
        X.append(u)
        y.append(got)
        ev = {"event": "bo-eval", "i": i, "gops": got,
              "mer": s.get("mer_med"), "hdr": s.get("hdr"), "knobs": knobs,
              "t": datetime.now().strftime("%H:%M:%S")}
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")
        print(f"  eval {i+1:2d}: GOPs {got:3d} | MER {s.get('mer_med')} "
              f"hdr {s.get('hdr')} | {knobs}", flush=True)
    best_i = int(np.argmax(y))
    print(f"\nBEST: {y[best_i]} true GOPs with {decode(X[best_i])}")
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "bo-best", "gops": int(y[best_i]),
                            "knobs": decode(X[best_i]),
                            "t": datetime.now().strftime("%H:%M:%S")}) + "\n")


if __name__ == "__main__":
    main()
