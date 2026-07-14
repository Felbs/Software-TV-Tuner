#!/usr/bin/env python3
"""native_sps_11v13.py — the decision that matters for THIS branch: does SPS 1.3
beat the current stvt_run.sh default of SPS 1.1 on real marginal signal?

1.1 is leaner (less CPU) but under-samples more; 1.3 costs ~18% more front-end
work. Test both at the discriminating cliff band (not the saturated levels).
Metric = mean TEI-bad% (RS-fail); LOWER = more robust.
"""
import os, subprocess
from pathlib import Path

HOME = Path.home(); REPO = HOME / "Software-TV-Tuner"
IQ = REPO / "iq_captures/cap_rf36_native.cf32"
REPLAY = REPO / "tools/tv_replay.py"
OUT = HOME / "native_sweep"; OUT.mkdir(exist_ok=True)
REPORT = OUT / "SPS_11v13.md"
PER = 90
LEVELS = [850, 1000, 1120, 1250]
SEEDS = [42, 7, 99]
BASE = {"STVT_EQ": "long", "STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
        "STVT_RRC_SYMS": "8", "STVT_VITERBI": "hard", "STVT_TEISCRUB": "0",
        "STVT_FPLL_FOLD": "1", "STVT_FPLL_BLOCK_NCO": "1"}


def tei_bad(ts):
    try: d = open(ts, "rb").read()
    except OSError: return 100.0
    i = d.find(b"\x47"); n = tei = 0
    while i >= 0 and i + 188 <= len(d):
        if d[i] != 0x47: i += 1; continue
        if d[i + 1] & 0x80: tei += 1
        n += 1; i += 188
    return round(100 * tei / max(n, 1), 2)


def run(sps, noise, seed):
    e = {**os.environ, **BASE, "STVT_SPS": str(sps),
         "STVT_ADD_NOISE": str(noise), "STVT_NOISE_SEED": str(seed)}
    ts = OUT / "cur2.ts"
    try:
        subprocess.run(["python3", str(REPLAY), "--iq", str(IQ), "--out", str(ts),
                        "--log", str(OUT / "cur2.log")], env=e, timeout=PER,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass
    v = tei_bad(ts)
    try: ts.unlink()
    except OSError: pass
    return v


def log(m):
    open(REPORT, "a").write(m + "\n"); print(m, flush=True)


def main():
    open(REPORT, "w").write("# SPS 1.1 vs 1.3 — real marginal (RF36 @ cliff band)\n\n")
    agg = {1.1: [], 1.3: []}
    for nz in LEVELS:
        row = {}
        for sps in (1.1, 1.3):
            vals = [run(sps, nz, sd) for sd in SEEDS]
            m = round(sum(vals) / len(vals), 2); agg[sps].append(m); row[sps] = m
            log(f"- SPS {sps} @ noise {nz}: mean TEI-bad **{m}%**  {vals}")
        log(f"  -> level {nz}: {'1.3 better' if row[1.3] < row[1.1] else '1.1 better'}"
            f" (delta {round(row[1.1]-row[1.3],2)}pp)\n")
    m11 = round(sum(agg[1.1]) / len(agg[1.1]), 2)
    m13 = round(sum(agg[1.3]) / len(agg[1.3]), 2)
    log(f"## VERDICT\n- SPS 1.1 overall: **{m11}%**\n- SPS 1.3 overall: **{m13}%**")
    log(f"- **Winner: SPS {'1.3' if m13 < m11 else '1.1'}** "
        f"(delta {round(abs(m11-m13),2)}pp; lower RS-fail = better).")


if __name__ == "__main__":
    try: main()
    except Exception as e:
        open(REPORT, "a").write(f"\ncrashed: {e}\n"); raise
