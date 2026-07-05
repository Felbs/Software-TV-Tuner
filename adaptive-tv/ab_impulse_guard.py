"""ab_impulse_guard.py — A/B for research hypothesis H1 (impulse-gated
LMS freeze) on the live rabbit-ears signal.

4 alternating phases on RF34 (NBC, decodes reliably with daytime gaps):
  OFF -> ON -> OFF -> ON, 8 min each, fresh chain per phase.
Per-phase metrics: MER (median of samples), gaps/min (CC-discontinuity
bursts, PID 0x1FFF excluded), headers/s liveness, and the guard's own
freeze counter from the chain log. Interleaving controls propagation
drift; the deltas between adjacent phases are the honest signal.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from tv_lab import ts_metrics   # noqa: E402

PY = r"C:\Users\user\radioconda\python.exe"
TOOLS = Path(r"Z:\src\magic-tv-decoder\tools")
LOGDIR = HERE / "lab"
OUT = LOGDIR / ("ab_impulse_%s.jsonl" % time.strftime("%Y%m%d_%H%M"))
RF = 34
PHASE_SEC = 8 * 60
RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
RE_FRZ = re.compile(r"frz=(\d+)")


def log(rec):
    rec["iso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)


def kill_chain():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'tv_live|tv_watch' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue }"], capture_output=True)


def phase_env(guard_on):
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
                "STVT_EQ_TELEM": "1",
                "STVT_EQ_IMPULSE_GUARD": "1" if guard_on else "0"})
    return env


def run_phase(idx, guard_on):
    tag = "ON" if guard_on else "OFF"
    kill_chain()
    time.sleep(3)
    chain_log = LOGDIR / f"ab_imp_p{idx}_{tag}.log"
    lf = open(chain_log, "w")
    p = subprocess.Popen([PY, "-u", str(TOOLS / "tv_live.py"),
                          "--rf", str(RF)],
                         env=phase_env(guard_on), stdout=lf,
                         stderr=subprocess.STDOUT)
    log({"phase": idx, "guard": tag, "event": "chain_up", "pid": p.pid})
    time.sleep(45)          # lock + settle before measuring
    mers, frz_last = [], 0
    t0 = time.time()
    while time.time() - t0 < PHASE_SEC:
        time.sleep(30)
        try:
            txt = chain_log.read_text(errors="ignore")[-30000:]
            errs = [float(m.group(1)) for m in RE_FS.finditer(txt)][-16:]
            if errs:
                import math
                m_now = sorted(20 * math.log10(5.0 / e)
                               for e in errs if e > 0)
                if m_now:
                    mers.append(m_now[len(m_now) // 2])
            fz = RE_FRZ.findall(txt)
            if fz:
                frz_last = int(fz[-1])
        except OSError:
            pass
    m = ts_metrics(240) or {}
    kill_chain()
    rec = {"phase": idx, "guard": tag,
           "mer_median": round(sorted(mers)[len(mers) // 2], 2) if mers else None,
           "gaps_min": round(m.get("gaps_min", -1), 1),
           "hdrs_s": round(m.get("hdrs_s", -1), 1),
           "real_pct": round(m.get("real_pct", -1)),
           "freezes": frz_last, "result": "done"}
    log(rec)
    return rec


log({"event": "ab_start", "rf": RF, "phases": "OFF ON OFF OFF... [alternating]"})
results = []
for i, guard in enumerate([False, True, False, True], 1):
    results.append(run_phase(i, guard))

off = [r for r in results if r["guard"] == "OFF"]
on = [r for r in results if r["guard"] == "ON"]


def avg(rs, k):
    vs = [r[k] for r in rs if isinstance(r.get(k), (int, float)) and r[k] >= 0]
    return round(sum(vs) / len(vs), 2) if vs else None


log({"event": "ab_verdict",
     "gaps_off": avg(off, "gaps_min"), "gaps_on": avg(on, "gaps_min"),
     "mer_off": avg(off, "mer_median"), "mer_on": avg(on, "mer_median"),
     "hdrs_off": avg(off, "hdrs_s"), "hdrs_on": avg(on, "hdrs_s"),
     "total_freezes": sum(r["freezes"] for r in on)})
print("=== A/B COMPLETE ===", flush=True)
