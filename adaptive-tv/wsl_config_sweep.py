#!/usr/bin/env python3
"""wsl_config_sweep.py — overnight decode-config sweep on the Threadripper.

Replays a MARGINAL capture (cap_proven_default_RF34_30s.cf32 — the RF34 grab
that the chain locks only intermittently) through the full chain-config space
via tv_replay.py, and scores each config by how much real program data it
recovers. Deterministic (frozen IQ file), so it's a clean apples-to-apples A/B:
every config decodes the exact same samples. The Threadripper can run the
heaviest equalizers the Pi never could — the point is to find the config that
squeezes the most out of a hard signal, i.e. makes THIS version decode best.

Metric = real-content TS packets recovered (non-null, TEI-clean via TEISCRUB).
Also logs null%, median MER (fs_err), and sync relocks (lock stability).
"""
import os, re, subprocess, time
from collections import Counter
from pathlib import Path

HOME = Path.home()
REPO = HOME / "Software-TV-Tuner"
IQ = REPO / "iq_captures/cap_fox_rf36_live.cf32"
REPLAY = REPO / "tools/tv_replay.py"
OUT = HOME / "wsl_sweep"; OUT.mkdir(exist_ok=True)
REPORT = OUT / "SWEEP_REPORT.md"; CSV = OUT / "sweep.csv"
PER_CFG_TIMEOUT = 200

BASE = {"STVT_SPS": "1.1", "STVT_RRC_SYMS": "8", "STVT_VITERBI": "hard",
        "STVT_TEISCRUB": "1", "STVT_EQ_TELEM": "1",
        "ATSCPLUS_FS_RELOCK_SEGS": "0", "ATSC_SYNC_SOFT_LOCK": "6.0"}


def build_configs():
    cfgs = []
    # 1) equalizer shootout (stock RS)
    for eq in ["long", "multifs", "multifs_dd", "pilot", "pilot_dd_soft", "cma"]:
        cfgs.append((f"eq:{eq}", {"STVT_EQ": eq, "STVT_RS": "stock"}))
    # 2) RS mode on the strongest EQ (long)
    for rs, er in [("stock", None), ("erasure", "7"), ("erasure", "20"),
                   ("erasure", "30"), ("erasure", "40")]:
        e = {"STVT_EQ": "long", "STVT_RS": rs}
        if er:
            e["STVT_RS_ERASURES"] = er
        cfgs.append((f"rs:{rs}{'/' + er if er else ''}", e))
    # 3) recovery/tracking knobs (long + erasure20 base — the current leader)
    RB = {"STVT_EQ": "long", "STVT_RS": "erasure", "STVT_RS_ERASURES": "20"}
    for name, extra in [
        ("gearLMS", {"STVT_EQ_GEAR_LMS": "1"}),
        ("RLS", {"STVT_EQ_RLS": "1"}),
        ("FSavg4", {"STVT_EQ_FS_AVG_DEPTH": "4"}),
        ("FSavg8", {"STVT_EQ_FS_AVG_DEPTH": "8"}),
        ("qreset", {"STVT_EQ_QUALITY_BAD_RMS": "8"}),
        ("LKG", {"STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0"}),
        ("rfnotch", {"STVT_RFNOTCH": "1", "STVT_DABNOTCH": "1"}),
        ("robust", {"STVT_EQ_ROBUST": "1"}),
        ("impulse", {"STVT_EQ_IMPULSE_GUARD": "1"}),
        ("nb", {"STVT_NB": "1"}),
        ("fft", {"STVT_EQ_FFT": "1"}),
        ("reset30", {"STVT_EQ_RESET_INTERVAL_SEC": "30"}),
    ]:
        cfgs.append((f"long+er20+{name}", {**RB, **extra}))
    # 4) EQ adaptation rate BETA
    for beta in ["1e-5", "3e-5", "5e-5", "1e-4", "3e-4", "1e-3"]:
        cfgs.append((f"long+er20+beta{beta}", {**RB, "STVT_EQ_BETA": beta}))
    # 5) internal oversampling SPS
    for sps in ["1.1", "1.3", "1.5", "2.0"]:
        cfgs.append((f"long+er20+sps{sps}", {**RB, "STVT_SPS": sps}))
    # 6) matched-filter length
    for rrc in ["4", "6", "8", "12", "16"]:
        cfgs.append((f"long+er20+rrc{rrc}", {**RB, "STVT_RRC_SYMS": rrc}))
    # 7) decode-diversity start offsets (different EQ/FPLL adaptation trajectory)
    for skip in ["500000", "2000000", "5000000"]:
        cfgs.append((f"long+er20+skip{skip}", {**RB, "STVT_IQ_SKIP": skip}))
    return cfgs


def measure(ts_path):
    try:
        d = open(ts_path, "rb").read()
    except OSError:
        return None
    p = Counter(); i = d.find(b"\x47"); t = 0
    while i >= 0 and i + 188 <= len(d):
        if d[i] != 0x47:
            i += 1; continue
        p[((d[i + 1] & 0x1f) << 8) | d[i + 2]] += 1; t += 1; i += 188
    real = sum(c for k, c in p.items() if k != 0x1fff)
    nullp = 100 * p.get(0x1fff, 0) / max(t, 1)
    return real, round(nullp, 1), t


def med_mer(log_path):
    try:
        txt = open(log_path).read()
    except OSError:
        return None
    v = [float(x) for x in re.findall(r"fs_err_rms=([\d.]+)", txt)]
    if not v:
        return None
    v.sort(); e = v[len(v) // 2]
    import math
    return round(20 * math.log10(5 / e), 1) if e > 0 else None


def relocks(log_path):
    try:
        m = re.findall(r"relocks=(\d+)", open(log_path).read())
        return int(m[-1]) if m else None
    except OSError:
        return None


def run_one(name, env):
    e = {**os.environ, **BASE, **env}
    ts = OUT / f"o_{name.replace('/', '_').replace('+', '_').replace(':', '_')}.ts"
    lg = OUT / "cur.log"
    t0 = time.time()
    try:
        subprocess.run(["python3", "-u", str(REPLAY), "--iq", str(IQ),
                        "--out", str(ts), "--log", str(lg)],
                       env=e, timeout=PER_CFG_TIMEOUT,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass
    dt = round(time.time() - t0)
    m = measure(ts)
    try:
        ts.unlink()
    except OSError:
        pass
    if m is None:
        return {"name": name, "real": 0, "null": 100.0, "mer": None,
                "relocks": None, "sec": dt}
    return {"name": name, "real": m[0], "null": m[1],
            "mer": med_mer(lg), "relocks": relocks(lg), "sec": dt}


def main():
    cfgs = build_configs()
    with open(REPORT, "w") as f:
        f.write(f"# WSL config sweep — {time.strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"Capture: {IQ.name} (marginal RF34). Metric: real-content "
                f"packets recovered (higher=better). {len(cfgs)} configs.\n\n")
    CSV.write_text("name,real_pkts,null_pct,mer,relocks,sec\n")
    results = []
    for i, (name, env) in enumerate(cfgs):
        r = run_one(name, env)
        results.append(r)
        with open(CSV, "a") as f:
            f.write(f"{r['name']},{r['real']},{r['null']},{r['mer']},"
                    f"{r['relocks']},{r['sec']}\n")
        best = max(results, key=lambda x: x["real"])
        with open(REPORT, "a") as f:
            f.write(f"- [{i+1}/{len(cfgs)}] `{r['name']}`: real={r['real']} "
                    f"null={r['null']}% MER={r['mer']} relocks={r['relocks']} "
                    f"({r['sec']}s)  | leader: `{best['name']}` ({best['real']})\n")
    # final ranking
    results.sort(key=lambda x: -x["real"])
    with open(REPORT, "a") as f:
        f.write("\n## RANKING (most real content recovered)\n")
        for r in results[:15]:
            f.write(f"1. `{r['name']}` — {r['real']} pkts, null {r['null']}%, "
                    f"MER {r['mer']}, relocks {r['relocks']}\n")
        w = results[0]
        f.write(f"\n**WINNER: `{w['name']}` — {w['real']} real packets "
                f"(vs baseline long/stock).**\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        with open(REPORT, "a") as f:
            f.write(f"\nsweep crashed: {e}\n")
