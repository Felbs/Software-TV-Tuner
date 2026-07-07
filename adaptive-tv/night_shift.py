"""night_shift.py — the whole night, one script.

    Phase 1  CUBE      until 05:00 — Antenna x Channel x Time round 2
                        (fills the 17:00-24:00 map gap; DEAF trigger and
                        specimen ring armed in every sample)
    Phase 2  AMBUSH    05:00-06:45 — continuous RF9 long dwells with the
                        warm-start tap cache, sitting INSIDE the measured
                        decode window (05:30-06:30) instead of sampling
                        past it. Voice announce on headers; specimen ring
                        armed; every dwell logged as an rf9-ambush event.
    Phase 3  MORNING   TV panel relaunched for the pre-work hours.

Run at bedtime:  python night_shift.py
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import overnight_cube as oc

HERE = Path(__file__).parent
PY = sys.executable
CACHE = HERE / "tap_cache"
CACHE.mkdir(exist_ok=True)

AMBUSH_END = "06:45"
CUBE_END = "05:00"


def log_event(obj):
    obj["t"] = datetime.now().strftime("%H:%M:%S")
    with open(HERE / "cube_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")
    print(obj, flush=True)


def now_hm():
    return datetime.now().strftime("%H:%M")


# ── phase 0: flutter probes (Physics Ladder P1) ───────────────────
# 0.54 Hz periodic fading found on RF34/rabbit at 23:45 (~20 dB above
# floor, hw-AGC off). Same channel on two antennas disambiguates:
# follows the antenna = local/mechanical; on both = common path or DSP.
import re as _re

import numpy as _np


def flutter_fft(rf, antenna, ant, rfg, ifgr, secs=75):
    s = oc.sample(rf, antenna, rfg, ifgr, secs=secs)
    txt = (HERE / "cube_chain.log").read_text(errors="ignore")
    pts = [(float(a), float(b)) for a, b in _re.findall(
        r"\[eq-long t=\s*([\d.]+)s\].*?fs_err_rms=([\d.]+)", txt)]
    out = {"event": "flutter-probe", "rf": rf, "ant": ant,
           "mer_med": s.get("mer_med"), "n": len(pts)}
    if len(pts) > 120:
        t = _np.array([p[0] for p in pts])
        v = _np.array([p[1] for p in pts])
        tu = _np.arange(t[0], t[-1], 0.2)
        vu = _np.interp(tu, t, v)
        vu -= vu.mean()
        ps = _np.abs(_np.fft.rfft(vu * _np.hanning(len(vu)))) ** 2
        fr = _np.fft.rfftfreq(len(vu), 0.2)
        band = (fr > 0.2) & (fr < 2.4)
        fb, pb = fr[band], ps[band]
        floor = float(_np.median(pb)) + 1e-30
        i = int(_np.argmax(pb))
        out.update({"peak_hz": round(float(fb[i]), 2),
                    "peak_db": round(10 * _np.log10(pb[i] / floor), 1)})
    log_event(out)


for probe in [(34, "Antenna B", "rabbit", 2, 32),
              (34, "Antenna A", "discone", 2, 32),   # cross-antenna: does
              (7, "Antenna B", "rabbit", 5, 32)]:    # 0.54 Hz follow B?
    try:
        flutter_fft(*probe)
    except Exception as e:
        log_event({"event": "flutter-probe-error", "err": str(e)[:80]})

# ── phase 1: cube, with the PILOT TRIPWIRE (Physics Ladder P3) ─────
# Detection precedes decoding: enhancement onset shows in RF9's cube
# samples long before video is possible. From TRIPWIRE_FROM onward, a
# rising RF9 (mer_med >= TRIP_MER) retires the cube EARLY and starts
# the ambush at the enhancement's leading edge instead of at a fixed
# alarm-clock hour.
TRIPWIRE_FROM = "03:30"
TRIP_MER = 13.8          # RF9 baseline ~12.5; 13.8 = clear rising edge

import psutil


def rf9_recent_mer():
    try:
        lines = (HERE / "cube_log.jsonl").read_text(
            encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines[-120:]):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("rf") == 9 and o.get("ant") == "rabbit" \
                and "event" not in o:
            return o.get("mer_med")
    return None


def kill_tree(proc):
    try:
        p = psutil.Process(proc.pid)
        for c in p.children(recursive=True):
            try:
                c.kill()
            except psutil.NoSuchProcess:
                pass
        p.kill()
    except psutil.NoSuchProcess:
        pass


print(f"night shift: cube until {CUBE_END} (tripwire from "
      f"{TRIPWIRE_FROM}), ambush until {AMBUSH_END}", flush=True)
cube = subprocess.Popen([PY, "-u", str(HERE / "overnight_cube.py"),
                         CUBE_END])
tripped = False
while cube.poll() is None:
    time.sleep(30)
    if now_hm() >= TRIPWIRE_FROM:
        m = rf9_recent_mer()
        if m is not None and m >= TRIP_MER:
            log_event({"event": "TRIPWIRE", "rf9_mer": m,
                       "note": "rising edge — ambush starts early"})
            oc.announce("Channel 9 rising early")
            tripped = True
            kill_tree(cube)
            time.sleep(4)
            break
log_event({"event": "night-cube-done", "tripped": tripped})

# ── phase 2: RF9 ambush ────────────────────────────────────────────
os.environ["STVT_EQ_TAP_CACHE"] = str(CACHE)
# DFE v1.1: gauntlet-proven marginal-channel weapon (RF7 sub-cliff:
# +58 hdr/sample, loss 80%->21%). RF9 at dawn is exactly its regime.
os.environ["STVT_EQ_DFE"] = "1"
# Reseed-on-collapse (built tonight): quality resets re-read the tap
# cache FILE (refreshed by each dwell's LKG saves) — recovery jumps to
# current knowledge instead of crawling. First live exercise = tonight.
os.environ["STVT_EQ_RESEED"] = "1"
os.environ["STVT_EQ_QUALITY_BAD_RMS"] = "8"
# E5 v1: the FEC sheriff rides along — detects confidently-wrong
# equilibria (healthy MER + dead RS), tries tap surgery via the eq
# command port, escalates to chain kill (the dwell just ends early
# and the next one starts fresh).
os.environ["STVT_EQ_CMD_FILE"] = str(HERE / "eq_cmd.txt")
sheriff = subprocess.Popen(
    [PY, "-u", str(HERE / "fec_sheriff.py"),
     "--log", str(HERE / "cube_chain.log"),
     "--cmd", str(HERE / "eq_cmd.txt"),
     "--mer", "14.0", "--badfrac", "0.6", "--cooldown", "20"])
best = 0
dwell_n = 0
recent_zero = 0
HARD_STOP = "07:10"
AMBUSH_ANTS = [("Antenna B", "rabbit"), ("Antenna A", "discone")]
while True:
    # stay past AMBUSH_END while the fish are biting (hot extension);
    # hard stop protects the user's morning TV
    if now_hm() >= HARD_STOP:
        break
    if now_hm() >= AMBUSH_END and recent_zero >= 2:
        break
    antenna, ant = AMBUSH_ANTS[dwell_n % 2]   # two independent shots
    dwell_n += 1
    s = oc.sample(9, antenna, 5, 32, secs=300)
    s["event"] = "rf9-ambush"
    s["ant"] = ant
    log_event(s)
    hdr = s.get("hdr") or 0
    recent_zero = recent_zero + 1 if hdr == 0 else 0
    if hdr > 0:
        oc.announce(f"Channel 9: {hdr} video headers, "
                    f"M E R {s.get('mer_med')}")
    if hdr > best:
        best = hdr
        # a genuinely decoding RF9 deserves a specimen of its OWN success
        if hdr >= 20:
            trig = Path(r"Z:\src\magic-tv-decoder\tools\data"
                        r"\specimens\TRIGGER")
            try:
                trig.write_text(f"RF9-GOLDEN {hdr} headers")
            except OSError:
                pass
log_event({"event": "ambush-done", "best_hdr": best, "dwells": dwell_n})
try:
    sheriff.terminate()
except Exception:
    pass

# ── phase 3: morning ───────────────────────────────────────────────
env = os.environ.copy()
env["PATH"] = r"C:\Program Files\SDRplay\API\x64" + os.pathsep + env["PATH"]
subprocess.Popen([PY, "-u", str(HERE / "tv_tuna_panel.py")], env=env,
                 cwd=str(HERE))
print("night shift complete — panel relaunched for the morning", flush=True)
