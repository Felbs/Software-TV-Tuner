"""day_program_729.py - all-day autonomous experiment queue (7/29).
Runs detached; each cycle takes the warden politely, does one experiment,
releases, sleeps. Everything logs to lab/day_program_729.log.

Cycle plan (repeats ~hourly):
  1. TV strength ladder: RF36 (FOX, strong) + RF34 + RF31 through BOTH
     equalizers with AGC-matched levels -> frames scored -> the daylight
     strength-spectrum data the WL promotion still wants.
  2. RF27/RF36 front-end forensics: log in_rms with AGC on vs manual gain
     (the wandering-front-end hypothesis for the never-decoding channels).
  3. GPS/Galileo: nothing (file-based work runs separately).
Storm watch, observatory and panel keep their own schedules; this queue
never fights them (warden priority 60, skip-don't-stack).
"""
import os
import signal
import subprocess
import sys
import time
import datetime

sys.path.insert(0, r"Z:\src\gr-radiotuna\tools")
import radio_lock

# Interpreter from the environment, never hardcoded (dox gate, 8/01): a
# personal path is an identity leak in a public repo AND breaks for every
# other user. STVT_PY overrides; sys.executable is the honest default.
PY = os.environ.get("STVT_PY") or sys.executable
REPO = r"Z:\src\magic-tv-decoder"
OUT = os.path.join(REPO, "lab", "day729")
os.makedirs(OUT, exist_ok=True)
LOG = os.path.join(REPO, "lab", "day_program_729.log")

BASE = dict(os.environ, STVT_VITERBI="soft", STVT_RS="erasure", STVT_SOVA="1",
            STVT_FPLL_FOLD="1", STVT_ANTENNA="Antenna B", STVT_BIAST="1",
            STVT_SDR_AGC="1", STVT_AGC_SETPOINT="-20")

END_HOUR = 18          # stand down when the user gets home
SECS = 90


def log(msg):
    line = f"{datetime.datetime.now():%H:%M:%S} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def frames(ts):
    if not os.path.exists(ts) or os.path.getsize(ts) < 2e6:
        return 0
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "warning",
                        "-stats", "-err_detect", "ignore_err", "-i", ts,
                        "-map", "0:v", "-f", "null", "-"],
                       capture_output=True, text=True)
    import re
    m = re.findall(r"frame=\s*(\d+)", r.stderr)
    return int(m[-1]) if m else 0


def sdr_hygiene():
    """The 7/29 three-layer contention lesson: (1) sweep strays that squat
    the control port and contend for the radio, (2) bounce the SDRplay
    service (activateStream fails sdrplay_api_Fail on a 2nd consecutive
    run otherwise), (3) settle."""
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process -Filter "
                        "\"Name='python.exe'\" | Where-Object "
                        "{$_.CommandLine -match 'tv_live'} | ForEach-Object "
                        # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
                        "{ Stop-Process -Id $_.ProcessId -Force "
                        "-Confirm:$false }"],
                       capture_output=True, timeout=60)  # pipe-ok: control cmd - nothing is read from the pipe
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Restart-Service -Name SDRplayAPIService -Force "
                        "-Confirm:$false"], capture_output=True, timeout=90)  # pipe-ok: control cmd - nothing is read from the pipe
    except Exception as exc:
        log(f"  hygiene warning: {exc!r}")
    time.sleep(12)


def run_one(rf, eq, tag):
    sdr_hygiene()
    ts = os.path.join(OUT, f"{tag}_rf{rf}_{eq}.ts")
    lg = os.path.join(OUT, f"{tag}_rf{rf}_{eq}.log")
    with open(lg, "w") as lf:
        p = subprocess.Popen([PY, os.path.join(REPO, "tools", "tv_live.py"),
                              "--rf", str(rf), "--out", ts],
                             cwd=REPO, env=dict(BASE, STVT_EQ=eq),
                             stdout=lf, stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        deadline = time.time() + SECS
        while time.time() < deadline and p.poll() is None:
            time.sleep(5)
            try:
                radio_lock.heartbeat()
            except Exception:
                pass
        p.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.terminate()
    n = frames(ts)
    # pull the last in_rms for the front-end forensics
    rms = "?"
    try:
        with open(lg, errors="replace") as f:
            for line in f:
                if "in_rms=" in line:
                    rms = line.split("in_rms=")[1].split()[0]
    except Exception:
        pass
    log(f"  RF{rf} {eq}: {n} frames, in_rms {rms}, "
        f"{os.path.getsize(ts)/1e6:.0f} MB" if os.path.exists(ts) else
        f"  RF{rf} {eq}: no file")
    return n


cycle = 0
while datetime.datetime.now().hour < END_HOUR:
    cycle += 1
    tag = f"c{cycle:02d}"
    log(f"=== cycle {cycle} ===")
    try:
        with radio_lock.Holder("day729", "daylight TV ladder", 60,
                               wait_s=120):
            for rf in (36, 34, 31, 27):
                for eq in ("wl", "long"):
                    run_one(rf, eq, tag)
                    time.sleep(3)
    except Exception as exc:
        log(f"  cycle skipped ({exc!r})")
    log(f"=== cycle {cycle} done; sleeping ===")
    time.sleep(1800)
log("day program complete")
