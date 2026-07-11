"""Overnight campaign: normal sweep-training PLUS an hourly attempt to
crack TV out of the discone (RF7, the only channel it has ever nearly
decoded). Dawn window (04:30-07:00) probes every 30 min — tropo time.
Stands down instantly if the user tunes anything. Ends 07:00.

Replaces time_trainer.py for tonight (does its sweeps too).
"""
import io
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")
PANEL = "http://127.0.0.1:8642"
HERE = Path(__file__).parent
LOGF = HERE / "lab" / "discone_night.log"


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def api(path, body=None):
    req = urllib.request.Request(
        PANEL + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def user_active(s):
    # the user is asleep; anything WE start is ours. We mark our own
    # tunes with the name prefix below, so an unmarked tune = the user.
    return (s.get("tuned") or s.get("tuning")) and \
        not (s.get("name") or "").startswith("NIGHT")


def wait_scan_done(cap_s=1500):
    t0 = time.time()
    while time.time() - t0 < cap_s:
        time.sleep(15)
        if not api("/api/status").get("scan", {}).get("running"):
            return True
    return False


def sweep(antenna):
    api("/api/antenna", {"antenna": antenna})
    time.sleep(1)
    api("/api/scan", {})
    ok = wait_scan_done()
    log(f"sweep on {antenna}: {'done' if ok else 'TIMEOUT'}")


def rf7_probe(hold_s=150):
    """One real attempt to decode RF7 on the discone."""
    api("/api/tune", {"rf": 7, "prog": 1, "virt": "7.1",
                      "name": "NIGHT discone RF7", "antenna": "Antenna C"})
    t0 = time.time()
    while time.time() - t0 < 120:
        time.sleep(5)
        s = api("/api/status")
        if s.get("tuned"):
            break
        if (s.get("stage") or "").startswith("RADIO FAILED"):
            log("RF7 probe: radio failed stage")
            break
    time.sleep(hold_s)
    s = api("/api/status")
    mer, loss = s.get("mer_db"), s.get("loss_pct")
    hdrs = s.get("hdrs_s") or 0
    log(f"RF7@discone: MER={mer} loss={loss}% hdrs/s={hdrs}")
    if hdrs and hdrs > 0.5:
        log("*** CRACKED — VIDEO IS FLOWING FROM THE DISCONE *** "
            "holding 10 min to soak the proof")
        time.sleep(600)
        s2 = api("/api/status")
        log(f"soak result: MER={s2.get('mer_db')} loss={s2.get('loss_pct')}% "
            f"hdrs/s={s2.get('hdrs_s')}")
        return True
    api("/api/stop", {})
    return False


def main():
    log("bedtime campaign starts — sweeps + hourly discone RF7 probes, "
        "dawn boost 04:30, ends 07:00")
    sweep_ant = ["Antenna B", "Antenna C"]
    sweep_i = 0
    cracked = 0
    last_sweep_hr = -1
    last_probe = 0.0
    while True:
        now = datetime.now()
        if now.hour == 7:
            log(f"07:00 — campaign over. cracks: {cracked}")
            api("/api/stop", {})
            return
        try:
            s = api("/api/status")
            if user_active(s):
                log("user is watching — standing down 10 min")
                time.sleep(600)
                continue
            in_dawn = (now.hour == 4 and now.minute >= 30) or now.hour in (5, 6)
            probe_gap = 1800 if in_dawn else 3600
            if time.time() - last_probe >= probe_gap:
                last_probe = time.time()
                if rf7_probe():
                    cracked += 1
            elif now.hour != last_sweep_hr and not s.get("scan", {}).get("running"):
                last_sweep_hr = now.hour
                sweep(sweep_ant[sweep_i % 2])
                sweep_i += 1
        except Exception as e:
            log(f"panel unreachable ({e}) — retrying in 5 min")
            time.sleep(300)
            continue
        time.sleep(60)


if __name__ == "__main__":
    main()
