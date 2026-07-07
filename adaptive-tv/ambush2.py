"""ambush2.py — guard-armed RF9 dawn ambush (hot-swap upgrade, 03:40).

Same contract as night_shift phase 2/3, plus tonight's validated
MOD-12 GUARD in every dwell (slips self-heal in ~1 ms instead of
costing a chain kill + dwell restart — slips hit 2 of the first 2
dwells). Sheriff rides WITHOUT slip-kill authority (the guard already
cures those; killing a healed chain would be friendly fire).
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
HARD_STOP = "07:10"

os.environ.update({
    "STVT_EQ_TAP_CACHE": str(CACHE),
    "STVT_EQ_DFE": "1",
    "STVT_EQ_RESEED": "1",
    "STVT_EQ_QUALITY_BAD_RMS": "8",
    "STVT_EQ_MOD12_GUARD": "1",              # tonight's validated cure
    "STVT_EQ_CMD_FILE": str(HERE / "eq_cmd.txt"),
    "STVT_VIT_CMD_FILE": str(HERE / "vit_cmd.txt"),
})

sheriff = subprocess.Popen(
    [PY, "-u", str(HERE / "fec_sheriff.py"),
     "--log", str(HERE / "cube_chain.log"),
     "--cmd", str(HERE / "eq_cmd.txt"),
     "--vitcmd", str(HERE / "vit_cmd.txt"),
     "--mer", "14.0", "--badfrac", "0.6", "--cooldown", "20"])


def log_event(obj):
    obj["t"] = datetime.now().strftime("%H:%M:%S")
    with open(HERE / "cube_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")
    print(obj, flush=True)


def now_hm():
    return datetime.now().strftime("%H:%M")


log_event({"event": "ambush2-start", "note": "guard-armed hot-swap"})
best = 0
dwell_n = 0
recent_zero = 0
# 03:53 hot-tune: discone blanked twice while rabbit mints 800+/dwell —
# every discone dwell costs 5 golden minutes. Rabbit-only for the rest.
AMBUSH_ANTS = [("Antenna B", "rabbit")]
while True:
    if now_hm() >= HARD_STOP:
        break
    if now_hm() >= AMBUSH_END and recent_zero >= 2:
        break
    antenna, ant = AMBUSH_ANTS[dwell_n % len(AMBUSH_ANTS)]
    dwell_n += 1
    s = oc.sample(9, antenna, 5, 32, secs=300)
    s["event"] = "rf9-ambush"
    s["ant"] = ant
    txt = ""
    try:
        txt = (HERE / "cube_chain.log").read_text(errors="ignore")
    except OSError:
        pass
    s["guard_fires"] = txt.count("MOD12 GUARD")
    log_event(s)
    hdr = s.get("hdr") or 0
    recent_zero = recent_zero + 1 if hdr == 0 else 0
    if hdr > 0:
        oc.announce(f"Channel 9: {hdr} video headers, "
                    f"M E R {s.get('mer_med')}")
    if hdr > best:
        best = hdr
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

env = os.environ.copy()
env["PATH"] = r"C:\Program Files\SDRplay\API\x64" + os.pathsep + env["PATH"]
subprocess.Popen([PY, "-u", str(HERE / "tv_tuna_panel.py")], env=env,
                 cwd=str(HERE))
print("ambush2 complete — panel relaunched", flush=True)
