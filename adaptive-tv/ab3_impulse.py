"""ab3_impulse.py — three-way live trial for hypothesis H1 during real
impulse weather: OFF vs FREEZE (binary batch guard) vs HUBER (per-sample
confidence-weighted LMS).

Six 8-minute phases, two rounds, order rotated between rounds so the
time-of-day trend (which confounded the morning A/B) cancels:
  round 1: OFF -> FREEZE -> HUBER
  round 2: HUBER -> FREEZE -> OFF
Fresh chain per phase on RF34. Per phase: median MER, gaps/min over the
final 4 min, headers/s, and each mechanism's own activity counters
(frz= / clmp= from the eq telemetry) — activity is the denominator that
says whether the weather was dirty enough for the result to count.
"""
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from tv_lab import ts_metrics   # noqa: E402

PY = sys.executable
TOOLS = Path(r"Z:\src\magic-tv-decoder\tools")
LOGDIR = HERE / "lab"
OUT = LOGDIR / ("ab3_impulse_%s.jsonl" % time.strftime("%Y%m%d_%H%M"))
RF = 34
PHASE_SEC = 8 * 60
RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
RE_FRZ = re.compile(r"frz=(\d+)")
RE_CLMP = re.compile(r"clmp=(\d+)")

MODES = {
    "OFF":    {},
    "FREEZE": {"STVT_EQ_IMPULSE_GUARD": "1"},
    "HUBER":  {"STVT_EQ_ROBUST": "1"},
}


def log(rec):
    rec["iso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)


def kill_chain():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'tv_live|tv_watch' } | "
                    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue }"], capture_output=True)


def phase_env(mode):
    env = os.environ.copy()
    env["PATH"] = (r"C:\Program Files\SDRplay\API\x64;C:\ffmpeg\bin;"
                   + env.get("PATH", ""))
    env.update({"STVT_ANTENNA": "Antenna A", "STVT_IFGR": "32",
                "STVT_RFGAIN_SEL": "2",
                "STVT_SDR_AGC": "1", "STVT_AGC_SETPOINT": "-20",
                "STVT_EQ": "long", "STVT_VITERBI": "soft",
                "STVT_RFNOTCH": "1", "STVT_DABNOTCH": "1",
                "STVT_RS": "stock", "STVT_SPS": "1.1",
                "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1",
                "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
                "STVT_EQ_TELEM": "1"})
    env.update(MODES[mode])
    return env


def run_phase(idx, mode):
    kill_chain()
    time.sleep(3)
    chain_log = LOGDIR / f"ab3_p{idx}_{mode}.log"
    lf = open(chain_log, "w")
    subprocess.Popen([PY, "-u", str(TOOLS / "tv_live.py"), "--rf", str(RF)],
                     env=phase_env(mode), stdout=lf,
                     stderr=subprocess.STDOUT)
    log({"phase": idx, "mode": mode, "event": "chain_up"})
    time.sleep(45)
    mers = []
    frz = clmp = 0
    t0 = time.time()
    while time.time() - t0 < PHASE_SEC:
        time.sleep(30)
        try:
            txt = chain_log.read_text(errors="ignore")[-30000:]
            errs = [float(m.group(1)) for m in RE_FS.finditer(txt)][-16:]
            vals = sorted(20 * math.log10(5.0 / e) for e in errs if e > 0)
            if vals:
                mers.append(vals[len(vals) // 2])
            f = RE_FRZ.findall(txt)
            c = RE_CLMP.findall(txt)
            if f: frz = int(f[-1])
            if c: clmp = int(c[-1])
        except OSError:
            pass
    m = ts_metrics(240) or {}
    kill_chain()
    rec = {"phase": idx, "mode": mode,
           "mer_median": round(sorted(mers)[len(mers) // 2], 2) if mers else None,
           "gaps_min": round(m.get("gaps_min", -1), 1),
           "hdrs_s": round(m.get("hdrs_s", -1), 1),
           "real_pct": round(m.get("real_pct", -1)),
           "freezes": frz, "clamped": clmp}
    log(rec)
    return rec


order = ["OFF", "FREEZE", "HUBER", "HUBER", "FREEZE", "OFF"]
log({"event": "ab3_start", "rf": RF, "order": order})
results = []
for i, mode in enumerate(order, 1):
    results.append(run_phase(i, mode))


def avg(mode, k):
    vs = [r[k] for r in results if r["mode"] == mode
          and isinstance(r.get(k), (int, float)) and r[k] >= 0]
    return round(sum(vs) / len(vs), 2) if vs else None


log({"event": "ab3_verdict",
     **{f"gaps_{m.lower()}": avg(m, "gaps_min") for m in MODES},
     **{f"mer_{m.lower()}": avg(m, "mer_median") for m in MODES},
     "freeze_activity": sum(r["freezes"] for r in results),
     "huber_clamped": sum(r["clamped"] for r in results)})
print("=== 3-WAY COMPLETE ===", flush=True)
