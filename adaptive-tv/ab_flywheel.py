"""ab_flywheel.py — A/B for Strike 1 (sync flywheel) on the live signal.

Waits for the radio to go quiet (no TV/meter/scan for 5 min), installs
nothing (assumes the flywheel DLL is already installed), then runs four
mirrored 8-minute phases on RF34:
    OFF -> COAST -> COAST -> OFF     (ATSCPLUS_FS_COAST=3)
Metrics per phase: gaps/min (the disease), headers/s, real %, MER, and
the flywheel's own coast counter from [fs_telem] lines (activity proof).
Success = COAST phases post materially fewer gaps than their OFF
neighbors while real% stays honest (no null-stream mirages).
"""
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from tv_lab import ts_metrics   # noqa: E402

PY = sys.executable
TOOLS = Path(r"Z:\src\magic-tv-decoder\tools")
LOGDIR = HERE / "lab"
OUT = LOGDIR / ("ab_flywheel_%s.jsonl" % time.strftime("%Y%m%d_%H%M"))
BASE = "http://127.0.0.1:8642"
RF = 34
PHASE_SEC = 8 * 60
RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
RE_COAST = re.compile(r"COAST #\d+ \(synthetic FS, field=\d+, lifetime=(\d+)\)")


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


def log(rec):
    rec["iso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)


def radio_quiet():
    global _api_fails
    try:
        st = api("/api/status")
        m = api("/api/meter")
        _api_fails = 0
        return (st.get("rf") is None and not st.get("tuning")
                and not st["scan"]["running"]
                and (m.get("rf") is None or m.get("watching")))
    except Exception:
        _api_fails = globals().get("_api_fails", 0) + 1
        return _api_fails >= 6   # panel dead 2+ min = nobody owns the radio


def kill_chain():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'tv_live|tv_watch' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue }"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "mpv.exe"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe"], capture_output=True)


def kill_panel():
    # The panel's idle waterfall sweeper grabs the radio and starves
    # direct chain launches (cost phase 1 of run 2). The trial owns the
    # radio; the panel sits out and gets relaunched at the end.
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'tv_tuna_panel' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue }"], capture_output=True)


def start_panel():
    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         "$env:PATH = 'C:\\Program Files\\SDRplay\\API\\x64;' + $env:PATH; "
         "Start-Process -FilePath $env:USERPROFILE\\radioconda\\python.exe "
         "-ArgumentList 'Z:\\src\\adaptive-tv\\tv_tuna_panel.py' "
         "-WindowStyle Hidden"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def restart_sdr_service():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Restart-Service SDRplayAPIService -Force"],
                   capture_output=True, timeout=90)
    time.sleep(5)


def phase_env(coast):
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
                "ATSCPLUS_FS_TELEM": "1",
                "ATSCPLUS_FS_COAST": "3" if coast else "0"})
    return env


def run_phase(idx, coast):
    tag = "COAST" if coast else "OFF"
    kill_chain()
    time.sleep(3)
    chain_log = LOGDIR / f"ab_fly_p{idx}_{tag}.log"
    lf = open(chain_log, "w")
    subprocess.Popen([PY, "-u", str(TOOLS / "tv_live.py"), "--rf", str(RF)],
                     env=phase_env(coast), stdout=lf,
                     stderr=subprocess.STDOUT)
    log({"phase": idx, "mode": tag, "event": "chain_up"})
    time.sleep(45)
    # dead-chain check: no FPLL telemetry after settle = radio never
    # opened (wedge / contention) — restart the service and retry once
    try:
        txt0 = chain_log.read_text(errors="ignore")
    except OSError:
        txt0 = ""
    if "fpll" not in txt0:
        log({"phase": idx, "mode": tag, "event": "dead_chain_recovery"})
        kill_chain()
        restart_sdr_service()
        lf = open(chain_log, "w")
        subprocess.Popen([PY, "-u", str(TOOLS / "tv_live.py"),
                          "--rf", str(RF)],
                         env=phase_env(coast), stdout=lf,
                         stderr=subprocess.STDOUT)
        time.sleep(45)
    mers = []
    coasts = 0
    t0 = time.time()
    while time.time() - t0 < PHASE_SEC:
        time.sleep(30)
        try:
            txt = chain_log.read_text(errors="ignore")[-40000:]
            errs = [float(m.group(1)) for m in RE_FS.finditer(txt)][-16:]
            vals = sorted(20 * math.log10(5.0 / e) for e in errs if e > 0)
            if vals:
                mers.append(vals[len(vals) // 2])
            cs = RE_COAST.findall(txt)
            if cs:
                coasts = int(cs[-1])
        except OSError:
            pass
    m = ts_metrics(240) or {}
    kill_chain()
    rec = {"phase": idx, "mode": tag,
           "mer_median": round(sorted(mers)[len(mers) // 2], 2) if mers else None,
           "gaps_min": round(m.get("gaps_min", -1), 1),
           "hdrs_s": round(m.get("hdrs_s", -1), 1),
           "real_pct": round(m.get("real_pct", -1)),
           "coasts": coasts}
    log(rec)
    return rec


log({"event": "flywheel_ab_armed", "note": "waiting for 5 min radio quiet"})
quiet_since = None
while True:
    if radio_quiet():
        if quiet_since is None:
            quiet_since = time.time()
        elif time.time() - quiet_since > 60:
            break
    else:
        quiet_since = None
    time.sleep(20)
log({"event": "flywheel_ab_start"})
# DLL pre-installed manually (2026-07-05 15:20 — the in-harness cmd
# quoting broke; install verified by timestamp before this run)
kill_panel()
time.sleep(3)

results = []
for i, coast in enumerate([False, True, True, False], 1):
    results.append(run_phase(i, coast))


def avg(mode, k):
    vs = [r[k] for r in results if r["mode"] == mode
          and isinstance(r.get(k), (int, float)) and r[k] >= 0]
    return round(sum(vs) / len(vs), 2) if vs else None


log({"event": "flywheel_verdict",
     "gaps_off": avg("OFF", "gaps_min"), "gaps_coast": avg("COAST", "gaps_min"),
     "hdrs_off": avg("OFF", "hdrs_s"), "hdrs_coast": avg("COAST", "hdrs_s"),
     "real_off": avg("OFF", "real_pct"), "real_coast": avg("COAST", "real_pct"),
     "mer_off": avg("OFF", "mer_median"), "mer_coast": avg("COAST", "mer_median"),
     "total_coasts": sum(r["coasts"] for r in results)})
start_panel()
print("=== FLYWHEEL A/B COMPLETE ===", flush=True)
