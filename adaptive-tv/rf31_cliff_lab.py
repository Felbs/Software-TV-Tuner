"""rf31_cliff_lab.py — what does the math want at EXACTLY the cliff?
RF31 on the directional @ 4:40 sits at -0.1..+0.1 dB with multipath
flutter. Sweep the untested cliff-regime levers, 60 s each, scored by
decoded headers/s + stream integrity (liveness-guarded)."""
import os, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, r"Z:\src\adaptive-tv")
from tv_lab import ts_metrics, kill_chain, LIVE, PY

TV_LIVE = r"Z:\src\magic-tv-decoder\tools\tv_live.py"

BASE = {"STVT_ANTENNA": "Antenna A", "STVT_IFGR": "40", "STVT_RFGAIN_SEL": "4",
        "STVT_EQ": "long", "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
        "STVT_DABNOTCH": "1", "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8",
        "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
        "STVT_EQ_TELEM": "1"}

ER20 = {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
        "STVT_EQ_QUALITY_BAD_RMS": "8"}

CONFIGS = [
    ("control er20+qr8      ", dict(ER20)),
    ("erasure 14            ", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "14",
                                "STVT_EQ_QUALITY_BAD_RMS": "8"}),
    ("erasure 7             ", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "7",
                                "STVT_EQ_QUALITY_BAD_RMS": "8"}),
    ("stock RS              ", {"STVT_RS": "stock",
                                "STVT_EQ_QUALITY_BAD_RMS": "8"}),
    ("fast-LMS beta 1e-4    ", dict(ER20, STVT_EQ_BETA="1e-4")),
    ("fast-LMS beta 2e-4    ", dict(ER20, STVT_EQ_BETA="2e-4")),
    ("qr6 (eager reset)     ", dict(ER20, STVT_EQ_QUALITY_BAD_RMS="6")),
    ("fpll alpha 2e-3       ", dict(ER20, STVT_FPLL_ALPHA="0.002")),
    ("SPS 1.5 + stock filter", {**ER20, "STVT_SPS": "1.5"}),
]

def run(name, extra, secs=60):
    kill_chain(); time.sleep(1)
    env = os.environ.copy()
    env["PATH"] = r"C:\Program Files\SDRplay\API\x64;" + env.get("PATH", "")
    env.update(BASE); env.update(extra)
    if "STVT_SPS" in extra and extra["STVT_SPS"] == "1.5":
        env.pop("STVT_RRC_SYMS", None)     # stock filter with stock SPS
    if LIVE.exists():
        try: LIVE.unlink()
        except OSError: pass
    ch = subprocess.Popen([PY, "-u", TV_LIVE, "--rf", "31"], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(secs)
    ch.terminate()
    try: ch.wait(timeout=6)
    except Exception: ch.kill()
    m = ts_metrics(40)
    if m:
        print(f"{name} hdrs/s={m['hdrs_s']:.2f} real={m['real_pct']:.0f}% "
              f"gaps/min={m['gaps_min']:.0f}", flush=True)
        return m["hdrs_s"], m["real_pct"]
    print(f"{name} NO OUTPUT", flush=True)
    return 0.0, 0.0

print("RF31 CLIFF LAB (round 1: 9 configs x 60s)", flush=True)
scores = {}
for name, extra in CONFIGS:
    scores[name] = [run(name, extra)]

top3 = sorted(scores, key=lambda k: -scores[k][0][0])[:3]
print(f"\nround 2: confirming top 3 (flutter breathes — reps matter)", flush=True)
for name in top3:
    extra = dict(next(e for n, e in CONFIGS if n == name))
    scores[name].append(run(name, extra))

print("\nFINAL (avg of reps):", flush=True)
for name, runs in sorted(scores.items(),
                         key=lambda kv: -sum(r[0] for r in kv[1])/len(kv[1])):
    h = sum(r[0] for r in runs)/len(runs)
    r = sum(x[1] for x in runs)/len(runs)
    print(f"  {name} hdrs/s={h:.2f} real={r:.0f}% (n={len(runs)})", flush=True)
kill_chain()
print("CLIFF LAB DONE", flush=True)
