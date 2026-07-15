#!/usr/bin/env python3
"""wsl_sync_sweep.py — phase 2: attack the sync-relock ceiling.

Phase 1 found long+erasure20+sps1.3 best, but EVERY config sat at ~15,000
symbol-timing relocks — the sync block can't HOLD this marginal capture, and no
EQ/RS knob touches that. This phase sweeps the atsc_sync_soft timing-loop knobs
(loop gain alpha, lock/unlock thresholds, sticky fraction) on the phase-1
winning base, looking for the setting that stops the relocking and rescues more
of the signal. Metric = real-content packets recovered; also tracks relocks
(want DOWN) and MER.
"""
import os, re, subprocess, time
from collections import Counter
from pathlib import Path

HOME = Path.home()
REPO = HOME / "Software-TV-Tuner"
IQ = REPO / "iq_captures/cap_proven_default_RF34_30s.cf32"
REPLAY = REPO / "tools/tv_replay.py"
OUT = HOME / "wsl_sweep"; OUT.mkdir(exist_ok=True)
REPORT = OUT / "SYNC_SWEEP_REPORT.md"; CSV = OUT / "sync_sweep.csv"
PER = 200

# phase-1 winning base
BASE = {"STVT_EQ": "long", "STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
        "STVT_SPS": "1.3", "STVT_RRC_SYMS": "8", "STVT_VITERBI": "hard",
        "STVT_TEISCRUB": "1", "STVT_EQ_TELEM": "1",
        "ATSCPLUS_FS_RELOCK_SEGS": "0"}


def build_configs():
    cfgs = [("winner-baseline", {})]
    # 1D sweeps of each sync knob
    for a in ["0.05", "0.1", "0.15", "0.2", "0.3", "0.5", "0.6"]:
        cfgs.append((f"alpha={a}", {"ATSC_SYNC_SOFT_ALPHA": a}))
    for u in ["0.1", "0.25", "0.5", "0.75", "1.0", "1.5"]:
        cfgs.append((f"unlock={u}", {"ATSC_SYNC_SOFT_UNLOCK": u}))
    for s in ["0.90", "0.97", "0.98", "0.99", "0.995", "0.999"]:
        cfgs.append((f"sticky={s}", {"ATSC_SYNC_SOFT_STICKY": s}))
    for l in ["2.5", "3.0", "3.5", "5.0", "6.0"]:
        cfgs.append((f"lock={l}", {"ATSC_SYNC_SOFT_LOCK": l}))
    cfgs.append(("adaptive", {"ATSC_SYNC_SOFT_ADAPTIVE": "1"}))
    # interaction grid on the three most promising knobs (they couple)
    for a in ["0.1", "0.2", "0.3"]:
        for u in ["0.25", "0.5", "1.0"]:
            for s in ["0.97", "0.99", "0.995"]:
                cfgs.append((f"a{a}/u{u}/s{s}",
                             {"ATSC_SYNC_SOFT_ALPHA": a,
                              "ATSC_SYNC_SOFT_UNLOCK": u,
                              "ATSC_SYNC_SOFT_STICKY": s}))
    return cfgs


def measure(ts):
    try:
        d = open(ts, "rb").read()
    except OSError:
        return 0, 100.0
    p = Counter(); i = d.find(b"\x47"); t = 0
    while i >= 0 and i + 188 <= len(d):
        if d[i] != 0x47:
            i += 1; continue
        p[((d[i + 1] & 0x1f) << 8) | d[i + 2]] += 1; t += 1; i += 188
    return (sum(c for k, c in p.items() if k != 0x1fff),
            round(100 * p.get(0x1fff, 0) / max(t, 1), 1))


def logstat(pat, log):
    try:
        m = re.findall(pat, open(log).read())
        return m[-1] if m else None
    except OSError:
        return None


def run_one(name, env):
    e = {**os.environ, **BASE, **env}
    ts = OUT / ("s_" + re.sub(r"[^A-Za-z0-9]", "_", name) + ".ts")
    lg = OUT / "syncur.log"
    t0 = time.time()
    try:
        subprocess.run(["python3", "-u", str(REPLAY), "--iq", str(IQ),
                        "--out", str(ts), "--log", str(lg)], env=e, timeout=PER,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass
    real, nullp = measure(ts)
    rel = logstat(r"relocks=(\d+)", lg)
    fs = logstat(r"fs_err_rms=([\d.]+)", lg)
    mer = None
    if fs:
        import math
        mer = round(20 * math.log10(5 / float(fs)), 1) if float(fs) > 0 else None
    try:
        ts.unlink()
    except OSError:
        pass
    return {"name": name, "real": real, "null": nullp, "relocks": rel,
            "mer": mer, "sec": round(time.time() - t0)}


def main():
    cfgs = build_configs()
    open(REPORT, "w").write(
        f"# Sync-knob sweep (phase 2) — {time.strftime('%Y-%m-%d %H:%M')}\n"
        f"Base = phase-1 winner (long+erasure20+sps1.3). Attacking the ~15k "
        f"sync relocks. {len(cfgs)} configs. Want real UP, relocks DOWN.\n\n")
    CSV.write_text("name,real_pkts,null_pct,relocks,mer,sec\n")
    res = []
    for i, (name, env) in enumerate(cfgs):
        r = run_one(name, env)
        res.append(r)
        open(CSV, "a").write(f"{r['name']},{r['real']},{r['null']},"
                             f"{r['relocks']},{r['mer']},{r['sec']}\n")
        best = max(res, key=lambda x: x["real"])
        open(REPORT, "a").write(
            f"- [{i+1}/{len(cfgs)}] `{r['name']}`: real={r['real']} "
            f"relocks={r['relocks']} MER={r['mer']} null={r['null']}%  | "
            f"leader `{best['name']}` ({best['real']}, relocks {best['relocks']})\n")
    res.sort(key=lambda x: -x["real"])
    with open(REPORT, "a") as f:
        f.write("\n## RANKING\n")
        for r in res[:12]:
            f.write(f"1. `{r['name']}` — {r['real']} pkts, relocks {r['relocks']}, "
                    f"MER {r['mer']}, null {r['null']}%\n")
        w = res[0]
        base_real = next((r["real"] for r in res if r["name"] == "winner-baseline"), 0)
        f.write(f"\n**BEST: `{w['name']}` — {w['real']} real packets "
                f"(phase-1 winner was {base_real}; relocks {w['relocks']} vs ~15355).**\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        open(REPORT, "a").write(f"\ncrashed: {e}\n")
