#!/usr/bin/env python3
"""wsl_marginal_sweep.py — the marginal-signal config sweep (all-day).

The clean-FOX sweep proved long+erasure+SPS1.3 wins but couldn't discriminate
the error-correction/recovery knobs — a clean signal has nothing for them to
fix. So push a real FOX capture to the CLIFF EDGE with controlled AWGN
(STVT_ADD_NOISE, calibrated: ~340 = 45% TEI-bad) and sweep the levers that only
matter on marginal signal: erasure depth, SOVA soft-erasures, EQ recovery
tricks, EQ variants. Each config is run at several noise levels × several noise
seeds (the cliff is stochastic — average out the realization). Metric = mean
TEI-bad (RS-fail %); LOWER = more robust = the config that best rescues weak
reception. Deterministic per (config,level,seed) → clean A/B.
"""
import os, re, subprocess, time
from pathlib import Path

HOME = Path.home()
REPO = HOME / "Software-TV-Tuner"
IQ = REPO / "iq_captures/cap_fox_rf36_live.cf32"
REPLAY = REPO / "tools/tv_replay.py"
OUT = HOME / "wsl_sweep_marginal"; OUT.mkdir(exist_ok=True)
REPORT = OUT / "MARGINAL_REPORT.md"; CSV = OUT / "marginal.csv"
PER = 160
NOISE = ["300", "330", "360", "390"]      # cliff-edge levels
SEEDS = ["42", "7", "99"]                  # noise realizations

BASE = {"STVT_EQ": "long", "STVT_SPS": "1.3", "STVT_RRC_SYMS": "8",
        "STVT_VITERBI": "hard", "STVT_TEISCRUB": "0",
        "ATSCPLUS_FS_RELOCK_SEGS": "0", "ATSC_SYNC_SOFT_LOCK": "6.0"}

CONFIGS = [
    ("stock-RS", {"STVT_RS": "stock"}),
    ("erasure7", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "7"}),
    ("erasure20", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20"}),
    ("erasure30", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "30"}),
    ("erasure40", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "40"}),
    ("erasure50", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "50"}),
    ("SOVA+er20", {"STVT_SOVA": "1", "STVT_RS": "erasure",
                   "STVT_RS_ERASURES": "20", "STVT_VITERBI": "soft"}),
    ("SOVA+er40", {"STVT_SOVA": "1", "STVT_RS": "erasure",
                   "STVT_RS_ERASURES": "40", "STVT_VITERBI": "soft"}),
    ("er20+gearLMS", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
                      "STVT_EQ_GEAR_LMS": "1"}),
    ("er20+RLS", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
                  "STVT_EQ_RLS": "1"}),
    ("er20+FSavg4", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
                     "STVT_EQ_FS_AVG_DEPTH": "4"}),
    ("er20+robust", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
                     "STVT_EQ_ROBUST": "1"}),
    ("er20+LKG", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
                  "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0"}),
    ("er20+impulse", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
                      "STVT_EQ_IMPULSE_GUARD": "1"}),
    ("er20+nb", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20", "STVT_NB": "1"}),
    ("er20+fft", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
                  "STVT_EQ_FFT": "1"}),
    ("er20+multifs_dd", {"STVT_EQ": "multifs_dd", "STVT_RS": "erasure",
                         "STVT_RS_ERASURES": "20"}),
    ("er20+gearLMS+robust", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
                             "STVT_EQ_GEAR_LMS": "1", "STVT_EQ_ROBUST": "1"}),
    ("er30+FSavg4+robust", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "30",
                            "STVT_EQ_FS_AVG_DEPTH": "4", "STVT_EQ_ROBUST": "1"}),
    ("SOVA+er40+gearLMS", {"STVT_SOVA": "1", "STVT_RS": "erasure",
                           "STVT_RS_ERASURES": "40", "STVT_VITERBI": "soft",
                           "STVT_EQ_GEAR_LMS": "1"}),
]


def tei_bad(ts):
    try:
        d = open(ts, "rb").read()
    except OSError:
        return 100.0
    tei = 0; i = d.find(b"\x47"); t = 0
    while i >= 0 and i + 188 <= len(d):
        if d[i] != 0x47:
            i += 1; continue
        if d[i + 1] & 0x80:
            tei += 1
        t += 1; i += 188
    return round(100 * tei / max(t, 1), 1)


def run(env, noise, seed):
    e = {**os.environ, **BASE, **env,
         "STVT_ADD_NOISE": noise, "STVT_NOISE_SEED": seed}
    ts = OUT / "cur.ts"
    try:
        subprocess.run(["python3", "-u", str(REPLAY), "--iq", str(IQ),
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


def main():
    open(REPORT, "w").write(
        f"# Marginal-signal sweep — {time.strftime('%Y-%m-%d %H:%M')}\n"
        f"FOX at the cliff (AWGN {NOISE}) × seeds {SEEDS}. Metric = mean "
        f"TEI-bad%% (RS-fail); LOWER = more robust. {len(CONFIGS)} configs, "
        f"{len(CONFIGS)*len(NOISE)*len(SEEDS)} runs.\n\n")
    CSV.write_text("config,noise,seed,tei_bad\n")
    agg = {}
    total = len(CONFIGS) * len(NOISE) * len(SEEDS); done = 0
    for name, env in CONFIGS:
        vals = []
        per_level = {}
        for nz in NOISE:
            lv = []
            for sd in SEEDS:
                v = run(env, nz, sd); done += 1
                vals.append(v); lv.append(v)
                open(CSV, "a").write(f"{name},{nz},{sd},{v}\n")
            per_level[nz] = round(sum(lv) / len(lv), 1)
        mean = round(sum(vals) / len(vals), 1)
        agg[name] = mean
        best = min(agg, key=agg.get)
        open(REPORT, "a").write(
            f"- [{done}/{total}] `{name}`: mean TEI-bad **{mean}%** "
            f"(by level: {per_level})  | leader `{best}` ({agg[best]}%)\n")
    ranked = sorted(agg.items(), key=lambda x: x[1])
    with open(REPORT, "a") as f:
        f.write("\n## RANKING (lowest mean TEI-bad = most robust on marginal)\n")
        for n, v in ranked:
            f.write(f"1. `{n}` — {v}% mean RS-fail\n")
        base = agg.get("erasure20", "?")
        f.write(f"\n**MOST ROBUST: `{ranked[0][0]}` ({ranked[0][1]}%)** "
                f"vs erasure20 baseline ({base}%) vs stock-RS ({agg.get('stock-RS','?')}%).\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        open(REPORT, "a").write(f"\ncrashed: {e}\n")
