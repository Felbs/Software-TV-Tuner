#!/usr/bin/env python3
"""wl_v3/sweep.py — drive tools/tv_dual.py over a capture x shrinkage matrix.

Every run is a SAMPLE-ALIGNED long-vs-wl A/B (one input stream, two
equalizers). Across runs the input is a frozen file with a fixed noise seed, so
the `long` leg is a per-capture INVARIANT — its md5 must be identical in every
arm of the same capture. That is the built-in control: if it ever moves, the
comparison is contaminated and the row is void.

    python lab/wl_v3/sweep.py --captures rf34cliff --arms v2,g05,g10
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time
from pathlib import Path

REPO = Path(r"Z:\src\magic-tv-decoder")
PY = os.path.join(os.environ.get("USERPROFILE", ""), "radioconda", "python.exe")
OUT = REPO / "lab" / "wl_v3" / "runs"

CAPTURES = {
    # name          iq file                                    noise amp, seed
    "rf34clean":   (REPO / "lab/marginal_iq/rf34_ctrl.cs16",   0,    42),
    "rf34cliff":   (REPO / "lab/marginal_iq/rf34_ctrl.cs16",   2147, 42),
    "rf34cliff77": (REPO / "lab/marginal_iq/rf34_ctrl.cs16",   2147, 77),
    "rf34deep":    (REPO / "lab/marginal_iq/rf34_ctrl.cs16",   2400, 42),
    "rf7":         (REPO / "lab/marginal_iq/rf7_marg.cs16",    0,    42),
    "rf9":         (REPO / "lab/marginal_iq/rf9_marg.cs16",    0,    42),
    "rf35":        (REPO / "lab/marginal_iq/rf35_marg.cs16",   0,    42),
    "rf27":        (REPO / "lab/wl_gate2/rf27_capture.cs16",   0,    42),
}

ARMS = {
    # name : extra env
    "v2":     {},                                                    # shrink off
    "g025":   {"STVT_WL_SHRINK": "1", "STVT_WL_SHRINK_GAIN": "0.25"},
    "g05":    {"STVT_WL_SHRINK": "1", "STVT_WL_SHRINK_GAIN": "0.5"},
    "g10":    {"STVT_WL_SHRINK": "1", "STVT_WL_SHRINK_GAIN": "1.0"},
    "g10b1":  {"STVT_WL_SHRINK": "1", "STVT_WL_SHRINK_GAIN": "1.0",
               "STVT_WL_SHRINK_B0": "0.001"},
    "g10b10": {"STVT_WL_SHRINK": "1", "STVT_WL_SHRINK_GAIN": "1.0",
               "STVT_WL_SHRINK_B0": "0.10"},
    # degenerate-to-linear: kappa forced to 1 every field => imag plane is
    # identically zero at every output segment => strictly-linear real FFE
    "degen":  {"STVT_WL_SHRINK": "1", "STVT_WL_SHRINK_GAIN": "1.0",
               "STVT_WL_SHRINK_FORCE": "1"},
}

BASE = dict(STVT_VITERBI="soft", STVT_RS="erasure", STVT_SOVA="1",
            STVT_FPLL_FOLD="1", STVT_EQ_TELEM="1", STVT_EQ_TELEM_EVERY="1",
            PYTHONPATH=f"{REPO};{REPO / 'tools'}")


def run(cap: str, arm: str, diag: bool = False) -> dict:
    iq, noise, seed = CAPTURES[cap]
    tag = f"{cap}_{arm}"
    outdir = OUT / cap
    outdir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(BASE)
    env.update(ARMS[arm])
    cmd = [PY, str(REPO / "tools" / "tv_dual.py"), "--iq", str(iq),
           "--outdir", str(outdir), "--tag", tag]
    if noise:
        cmd += ["--noise", str(noise), "--seed", str(seed)]
    if diag:
        d = outdir / f"diag_{arm}"
        cmd += ["--diag-dir", str(d)]
    t0 = time.time()
    subprocess.run(cmd, env=env, check=False)
    js = outdir / f"{tag}.json"
    res = json.loads(js.read_text()) if js.exists() else {}
    res["wall_s"] = round(time.time() - t0, 1)
    res["arm"] = arm
    res["capture"] = cap
    js.write_text(json.dumps(res, indent=2))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--captures", default="rf34clean,rf34cliff")
    ap.add_argument("--arms", default="v2,g05,g10")
    ap.add_argument("--diag", action="store_true")
    ap.add_argument("--repeats", type=int, default=1,
                    help="repeat every (capture, arm) N times. The control "
                         "verdict needs >=3 samples of the `long` leg per "
                         "capture (gate_lib.MIN_RUNS) — with 3+ arms the arms "
                         "themselves supply them; with fewer, raise this.")
    # MEASURED volk noise floor on the `long` control leg, 3 identical runs per
    # capture, 2026-07-29: rf34clean spread 0, rf7 1, rf34cliff 2, rf9 3
    # (and lab/wl_v3/WORKLOG §3 saw 5 over 5 runs at the knee). The control
    # tolerance must therefore be >= 4 or it manufactures false VOIDs on
    # marginal captures — the first run of this gate did exactly that on rf9.
    ap.add_argument("--ctl-frame-tol", type=int, default=4,
                    help="frame spread the `long` control leg is allowed across "
                         "identical runs (default 4 = the measured volk noise "
                         "floor on marginal captures)")
    a = ap.parse_args()

    sys.path.insert(0, str(REPO / "lab"))
    from gate_lib import RunRow, control_ok, hash_stats, frame_stats, MIN_RUNS

    rows = []
    for cap in a.captures.split(","):
        # ── THE CONTROL, done validly (2026-07-29) ────────────────────────
        # The `long` leg is the same computation in every arm (the WL knobs
        # cannot reach it), so the arms ARE repeated runs of one control. The
        # old test — `lm == the first arm's md5` — was the invalid single-run
        # hash comparison: NEITHER equalizer is bit-reproducible across
        # processes (volk kernel choice by pointer alignment; see
        # lab/gate_lib.py THE LAW). It passed by luck whenever both runs drew
        # the modal hash. Now every arm's long leg is COLLECTED and judged
        # together, on the modal hash and the frame median.
        base_long_md5 = None
        ctl_rows: list = []
        for arm in a.arms.split(","):
          for rep in range(a.repeats):
            r = run(cap, arm, a.diag)
            lm = r.get("long", {}).get("md5")
            if base_long_md5 is None:
                base_long_md5 = lm
            # kept for the ledger, but it is NOT the verdict any more
            r["long_md5_equals_first_arm"] = (lm == base_long_md5)
            ctl_rows.append(RunRow(tag=f"{cap}_{arm}", run=rep,
                                   md5=(lm or "").upper(),
                                   frames=r.get("long", {}).get("frames", 0)))
            r["control_ok"] = True   # provisional; the pooled verdict follows
            rows.append(r)
            t = r.get("telemetry", {})
            p = t.get("paired") or {}
            mw = t.get("mer_wl") or {}
            ml = t.get("mer_long") or {}
            print(f"{cap:<12}{arm:<8} long {r['long']['frames']:>6}f  "
                  f"wl {r['wl']['frames']:>6}f  adv {str(r.get('wl_frame_advantage')):>8}%  "
                  f"imagfrac {str((t.get('imag_frac') or {}).get('mean')):>7}  "
                  f"ben {str((t.get('imag_benefit') or {}).get('mean')):>8}  "
                  f"kap {str((t.get('kappa') or {}).get('mean')):>7}  "
                  f"MERwl_p10 {str(mw.get('p10')):>7} MERlong_p10 {str(ml.get('p10')):>7}  "
                  f"long_md5 {(lm or '')[:8]}", flush=True)

        # ── the pooled control verdict for this capture ────────────────────
        hs, fs = hash_stats(ctl_rows), frame_stats(ctl_rows)
        if len(ctl_rows) < MIN_RUNS:
            verdict = (f"CONTROL UNDECIDED — only {len(ctl_rows)} sample(s) of "
                       f"the `long` leg; a hash claim needs >= {MIN_RUNS} "
                       f"(use --repeats or more arms). {hs} | {fs}")
            ok = None
        else:
            ok, why = control_ok(ctl_rows, ctl_rows,
                                 frame_tol=a.ctl_frame_tol)
            ok = ok and fs.spread <= a.ctl_frame_tol
            verdict = (f"CONTROL {'OK' if ok else 'VOID'} "
                       f"(tol {a.ctl_frame_tol}) — {hs} | {fs}")
        print(f"  [{cap}] {verdict}", flush=True)
        for r in rows:
            if r.get("capture") == cap:
                r["control_ok"] = ok
                r["control_verdict"] = verdict
                r["control_hashes"] = hs.counts
    (OUT / "sweep_summary.json").write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
