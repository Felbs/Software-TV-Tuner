"""rf15_sentry.py — hunt RF15's high phase instead of waiting for it.

RF15 oscillates 12↔17 dB; its DEAF anomaly (healthy MER, dead FEC)
appears only in high phases (seen 16:22 yesterday). Probe every 30 min;
when the median crosses the bar, IMMEDIATELY run the full trial:
a 120 s dwell with guard (default) + DFE — the two weapons together.
Exits at 16:50 to hand the radio to the evening cube.

    python rf15_sentry.py
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

import overnight_cube as oc

HERE = Path(__file__).parent
BAR = 15.3
END = "16:50"


def log_event(obj):
    obj["t"] = datetime.now().strftime("%H:%M:%S")
    with open(HERE / "cube_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")
    print(obj, flush=True)


print(f"rf15 sentry: probing every 30 min until {END}; "
      f"full trial at median >= {BAR}", flush=True)
while datetime.now().strftime("%H:%M") < END:
    s = oc.sample(15, "Antenna B", 1, 26, secs=120)
    s["event"] = "rf15-probe"
    log_event(s)
    med = s.get("mer_med") or 0
    if med >= BAR:
        log_event({"event": "RF15-HIGH-PHASE", "mer": med})
        os.environ["STVT_EQ_DFE"] = "1"
        os.environ["STVT_EQ_TAP_CACHE"] = str(HERE / "tap_cache")
        t = oc.sample(15, "Antenna B", 1, 26, secs=180)
        t["event"] = "rf15-DEAF-trial"
        log_event(t)
        os.environ.pop("STVT_EQ_DFE", None)
        hdr = t.get("hdr") or 0
        if hdr > 0:
            oc.announce(f"W E T A decodes: {hdr} headers")
            log_event({"event": "RF15-FIRST-DECODE", "hdr": hdr})
            break                      # history made; sentry retires
    # sleep in 60 s slices so the end-time check stays responsive
    for _ in range(28):
        if datetime.now().strftime("%H:%M") >= END:
            break
        time.sleep(60)
print("rf15 sentry done", flush=True)
