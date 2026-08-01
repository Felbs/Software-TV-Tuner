"""tv_overnight.py — unattended overnight campaign (runs until 07:30).

Waits for the radio to go quiet (no metering/tuning/scanning for 6 min),
then each hour:
  - circuit: tune the first program of every locked mux + an RF7 VHF
    probe, 120 s settle, quality snapshot (captures fireworks recovery,
    overnight drift, and the dawn propagation window)
  - 25-min soak on the best hop with per-minute samples
Failure recovery: on RADIO FAILED, restarts the SDRplay API service and
retries once. Log: lab/overnight_<stamp>.jsonl. Ends by stopping TV.
"""
import json
import subprocess
import time
import urllib.request

BASE = "http://127.0.0.1:8642"
OUT = r"Z:\src\adaptive-tv\lab\overnight_%s.jsonl" % time.strftime("%Y%m%d_%H%M")
END_HHMM = (7, 30)


def api(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def log(rec):
    rec["iso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)


def snap(st, extra=None):
    rec = {k: st.get(k) for k in ("mer_db", "mer_last", "hdrs_s", "gaps_min",
                                  "real_pct", "in_rms", "max_x")}
    if extra:
        rec.update(extra)
    return rec


def past_end():
    t = time.localtime()
    return (t.tm_hour, t.tm_min) >= END_HHMM and t.tm_hour < 12


def radio_quiet():
    try:
        st = api("/api/status")
        m = api("/api/meter")
        return (st.get("rf") is None and not st.get("tuning")
                and not st["scan"]["running"] and m.get("rf") is None)
    except Exception:
        return False


def restart_sdr_service():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Restart-Service SDRplayAPIService -Force"],
                   capture_output=True, timeout=90)  # pipe-ok: control cmd - nothing is read from the pipe
    time.sleep(5)


def tune(hop, timeout=200):
    for attempt in (1, 2):
        t0 = time.time()
        try:
            api("/api/tune", hop)
        except Exception:
            time.sleep(10)
            continue
        while time.time() - t0 < timeout:
            st = api("/api/status")
            if st.get("tuned"):
                st["_tune_secs"] = round(time.time() - t0, 1)
                return st
            stage = st.get("stage") or ""
            if stage.startswith("RADIO FAILED"):
                log({"event": "radio_failed", "hop": hop.get("virt"),
                     "attempt": attempt})
                if attempt == 1:
                    restart_sdr_service()
                break
            if stage.startswith("PLAYER"):
                st["_tune_secs"] = round(time.time() - t0, 1)
                st["_no_player"] = True
                return st
            time.sleep(4)
    return api("/api/status")


def hops_from_grid():
    hops = []
    try:
        rows = api("/api/grid")["rows"]
        seen = set()
        for r in rows:
            if r["rf"] not in seen:
                seen.add(r["rf"])
                hops.append({"rf": r["rf"], "prog": r["prog"],
                             "virt": r["virtual"], "name": r["callsign"]})
    except Exception:
        pass
    if not any(h["rf"] == 7 for h in hops):
        hops.append({"rf": 7, "prog": 1, "virt": "7.1", "name": "WJLA VHF"})
    return hops


# ── phase 0: wait for the humans to go to bed ──────────────────────
log({"event": "overnight_armed", "note": "waiting for radio quiet 6 min"})
quiet_since = None
while True:
    if radio_quiet():
        if quiet_since is None:
            quiet_since = time.time()
        elif time.time() - quiet_since > 6 * 60:
            break
    else:
        quiet_since = None
    time.sleep(30)
log({"event": "overnight_start"})

cycle = 0
while not past_end():
    cycle += 1
    log({"event": "cycle_start", "cycle": cycle})
    results = []
    for hop in hops_from_grid():
        if past_end():
            break
        st = tune(hop)
        if not st.get("tuned"):
            log({"cycle": cycle, "hop": hop["virt"], "result": "TUNE FAILED",
                 "stage": (st.get("stage") or "")[:90]})
            continue
        time.sleep(120)
        st = api("/api/status")
        rec = snap(st, {"cycle": cycle, "hop": hop["virt"],
                        "name": hop["name"], "rf": hop["rf"],
                        "no_player": bool(st.get("stage", "").startswith("PLAYER"))})
        log(rec)
        results.append(rec)
    good = [r for r in results if (r.get("hdrs_s") or 0) > 3]
    if good and not past_end():
        best = sorted(good, key=lambda r: ((r.get("gaps_min") or 99),
                                           -(r.get("hdrs_s") or 0)))[0]
        hop = {"rf": best["rf"], "prog": None, "virt": best["hop"],
               "name": best["name"]}
        rows = api("/api/grid")["rows"]
        hop["prog"] = next((r["prog"] for r in rows
                            if r["virtual"] == best["hop"]), 3)
        log({"event": "soak_start", "cycle": cycle, "channel": best["hop"]})
        tune(hop)
        t0 = time.time()
        while time.time() - t0 < 25 * 60 and not past_end():
            time.sleep(60)
            log(snap(api("/api/status"),
                     {"cycle": cycle, "soak": best["hop"],
                      "min": round((time.time() - t0) / 60)}))
    else:
        log({"event": "no_decodable_hop", "cycle": cycle,
             "note": "idling 20 min (waterfall keeps sweeping)"})
        try:
            api("/api/stop", {})
        except Exception:
            pass
        t0 = time.time()
        while time.time() - t0 < 20 * 60 and not past_end():
            time.sleep(60)

try:
    api("/api/stop", {})
except Exception:
    pass
log({"event": "overnight_complete", "cycles": cycle})
print("\n=== OVERNIGHT COMPLETE ===", flush=True)
