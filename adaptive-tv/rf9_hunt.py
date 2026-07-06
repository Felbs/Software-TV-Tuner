"""rf9_hunt.py — opportunistic long-dwell strike on channel 9 at the
tropo peak. Waits for the cube's cycle to finish (its last discone
sample), then uses the inter-cycle sleep gap for one 5-minute RF9 dwell
with the warm-start tap cache — long integration the 28 s cube samples
can't afford. Logs an rf9-hunt event; announces if headers appear.
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

import overnight_cube as oc

LOG = Path(__file__).parent / "cube_log.jsonl"
CACHE = Path(__file__).parent / "tap_cache"
CACHE.mkdir(exist_ok=True)
os.environ["STVT_EQ_TAP_CACHE"] = str(CACHE)

TARGET_CYC = int(os.environ.get("HUNT_CYC", "18"))
DWELL = 300


def cube_cycle_done(cyc):
    try:
        for line in LOG.read_text(encoding="utf-8").splitlines():
            o = json.loads(line)
            if (o.get("cyc") == cyc and o.get("rf") == 36
                    and o.get("ant") == "discone"):
                return True
    except (OSError, json.JSONDecodeError):
        pass
    return False


print(f"hunt: waiting for cube cycle {TARGET_CYC} to finish...", flush=True)
t0 = time.time()
while not cube_cycle_done(TARGET_CYC):
    if time.time() - t0 > 1800:
        raise SystemExit("hunt: gave up waiting for the cycle")
    time.sleep(10)

# safety margin after the cube's last chain exits
time.sleep(20)
print(f"hunt: gap open, {DWELL}s dwell on RF9 rabbit begins "
      f"{datetime.now():%H:%M:%S}", flush=True)
s = oc.sample(9, "Antenna B", 5, 32, secs=DWELL)
s.update({"event": "rf9-hunt", "dwell": DWELL})
oc.log(s)
print("hunt result:", s, flush=True)
if s.get("hdr", 0) > 0:
    oc.announce(f"Channel 9 first headers ever: {s['hdr']} headers, "
                f"M E R {s.get('mer_med')}")
