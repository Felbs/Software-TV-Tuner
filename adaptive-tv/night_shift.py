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


# ── phase 1: cube ──────────────────────────────────────────────────
print(f"night shift: cube until {CUBE_END}, ambush until {AMBUSH_END}",
      flush=True)
r = subprocess.run([PY, "-u", str(HERE / "overnight_cube.py"), CUBE_END])
log_event({"event": "night-cube-done", "rc": r.returncode})

# ── phase 2: RF9 ambush ────────────────────────────────────────────
os.environ["STVT_EQ_TAP_CACHE"] = str(CACHE)
# DFE v1.1: gauntlet-proven marginal-channel weapon (RF7 sub-cliff:
# +58 hdr/sample, loss 80%->21%). RF9 at dawn is exactly its regime.
os.environ["STVT_EQ_DFE"] = "1"
best = 0
while now_hm() < AMBUSH_END:
    s = oc.sample(9, "Antenna B", 5, 32, secs=300)
    s["event"] = "rf9-ambush"
    log_event(s)
    hdr = s.get("hdr") or 0
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
log_event({"event": "ambush-done", "best_hdr": best})

# ── phase 3: morning ───────────────────────────────────────────────
env = os.environ.copy()
env["PATH"] = r"C:\Program Files\SDRplay\API\x64" + os.pathsep + env["PATH"]
subprocess.Popen([PY, "-u", str(HERE / "tv_tuna_panel.py")], env=env,
                 cwd=str(HERE))
print("night shift complete — panel relaunched for the morning", flush=True)
