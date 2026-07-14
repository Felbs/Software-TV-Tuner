#!/usr/bin/env python3
"""native_sps_marginal.py — validate SPS 1.3 vs 1.5 on the NATIVE Ubuntu box,
on REAL RF36 signal pushed to the decode cliff with calibrated AWGN.

Lesson banked on WSL: a clean signal can't discriminate config (both 0% TEI),
and synthetic-only sweeps can mislead (RLS overfit AWGN). So: (1) self-calibrate
the noise amplitude on THIS capture until TEI-bad crosses ~30%, then (2) A/B
SPS 1.3 vs 1.5 at the 3 levels bracketing that cliff, several seeds each.
Metric = mean TEI-bad% (RS-fail); LOWER = more robust.
"""
import os, subprocess, collections
from pathlib import Path

HOME = Path.home()
REPO = HOME / "Software-TV-Tuner"
IQ = REPO / "iq_captures/cap_rf36_native.cf32"
REPLAY = REPO / "tools/tv_replay.py"
OUT = HOME / "native_sweep"; OUT.mkdir(exist_ok=True)
REPORT = OUT / "SPS_MARGINAL.md"
PER = 90

BASE = {"STVT_EQ": "long", "STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
        "STVT_RRC_SYMS": "8", "STVT_VITERBI": "hard", "STVT_TEISCRUB": "0",
        "STVT_FPLL_FOLD": "1", "STVT_FPLL_BLOCK_NCO": "1"}


def tei_bad(ts):
    try:
        d = open(ts, "rb").read()
    except OSError:
        return 100.0
    i = d.find(b"\x47"); n = tei = 0
    while i >= 0 and i + 188 <= len(d):
        if d[i] != 0x47:
            i += 1; continue
        if d[i + 1] & 0x80:
            tei += 1
        n += 1; i += 188
    return round(100 * tei / max(n, 1), 2)


def run(sps, noise, seed):
    e = {**os.environ, **BASE, "STVT_SPS": str(sps),
         "STVT_ADD_NOISE": str(noise), "STVT_NOISE_SEED": str(seed)}
    ts = OUT / "cur.ts"
    try:
        subprocess.run(["python3", str(REPLAY), "--iq", str(IQ),
                        "--out", str(ts), "--log", str(OUT / "cur.log")],
                       env=e, timeout=PER,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass
    v = tei_bad(ts)
    try:
        ts.unlink()
    except OSError:
        pass
    return v


def log(msg):
    with open(REPORT, "a") as f:
        f.write(msg + "\n")
    print(msg, flush=True)


def main():
    open(REPORT, "w").write("# Native SPS 1.3 vs 1.5 — marginal (RF36 @ cliff)\n\n")
    # Phase 1: self-calibrate the cliff at SPS=1.5.
    log("## Phase 1 — calibrate noise to the cliff (SPS=1.5, seed 42)")
    cliff = None
    amp = 1000.0
    for _ in range(10):
        v = run(1.5, amp, 42)
        log(f"- noise={amp:.0f} -> TEI-bad {v}%")
        if v >= 25.0:
            cliff = amp; break
        amp *= 1.6
    if cliff is None:
        cliff = amp
        log(f"(cliff not clearly hit; using {cliff:.0f})")
    # 3 levels bracketing the cliff.
    levels = [round(cliff * 0.7), round(cliff), round(cliff * 1.3)]
    log(f"\n## Phase 2 — SPS A/B at cliff levels {levels}, seeds 42/7/99\n")
    seeds = [42, 7, 99]
    agg = {1.3: [], 1.5: []}
    for nz in levels:
        for sps in (1.3, 1.5):
            vals = [run(sps, nz, sd) for sd in seeds]
            m = round(sum(vals) / len(vals), 2)
            agg[sps].append(m)
            log(f"- SPS {sps} @ noise {nz}: mean TEI-bad **{m}%**  {vals}")
    m13 = round(sum(agg[1.3]) / len(agg[1.3]), 2)
    m15 = round(sum(agg[1.5]) / len(agg[1.5]), 2)
    log(f"\n## VERDICT\n- SPS 1.3 overall mean TEI-bad: **{m13}%**")
    log(f"- SPS 1.5 overall mean TEI-bad: **{m15}%**")
    winner = "1.3" if m13 < m15 else "1.5"
    log(f"- **More robust on real marginal signal: SPS {winner}** "
        f"(lower RS-fail = better).")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        open(REPORT, "a").write(f"\ncrashed: {e}\n")
        raise
