"""tv_evening_lab.py — ~55-minute evening experiment block (pool session).

Four experiments that want a still, humanless room:
  1  RF15 stakeout (20 min)   — is the resurrection strengthening as night
                                falls? 20-s samples extend the time-of-day
                                curve the 90-min campaign started.
  2  NBC recheck (10 min)     — RF34 gapped 24/min at 18:57; does it recover
                                like Fox did? (per-tower drift curve)
  3  Latency ladder (15 min)  — alternate Fox<->ION tunes, 4 cold tunes,
                                latency distribution for the warm-start-EQ
                                business case.
  4  VHF telemetry probe (8m) — meter-mode on RF7 and RF9 (Philips locked
                                them on 7/03): capture in_rms/MER shape for
                                the VHF-starvation mystery file.
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8642"
OUT = r"Z:\src\adaptive-tv\lab\evening_lab_%s.jsonl" % time.strftime("%Y%m%d_%H%M")


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


def tune(hop, timeout=200):
    t0 = time.time()
    api("/api/tune", hop)
    while time.time() - t0 < timeout:
        st = api("/api/status")
        if st.get("tuned"):
            st["_tune_secs"] = round(time.time() - t0, 1)
            return st
        stage = st.get("stage") or ""
        if stage.startswith(("RADIO FAILED", "PLAYER")):
            st["_tune_secs"] = round(time.time() - t0, 1)
            return st
        time.sleep(4)
    return api("/api/status")


log({"event": "evening_lab_start"})

# ── 1: RF15 stakeout, 20 min of 20-s samples ───────────────────────
log({"event": "exp", "exp": "1_rf15_stakeout"})
st = tune({"rf": 15, "prog": 1, "virt": "14.1", "name": "WFDC"})
log(snap(st, {"exp": 1, "event": "tuned", "tune_secs": st.get("_tune_secs")}))
t0 = time.time()
while time.time() - t0 < 20 * 60:
    time.sleep(20)
    log(snap(api("/api/status"), {"exp": 1, "sec": round(time.time() - t0)}))

# ── 2: NBC recheck, 10 min ─────────────────────────────────────────
log({"event": "exp", "exp": "2_nbc_recheck"})
st = tune({"rf": 34, "prog": 3, "virt": "4.1", "name": "WRC NBC"})
log(snap(st, {"exp": 2, "event": "tuned", "tune_secs": st.get("_tune_secs")}))
t0 = time.time()
while time.time() - t0 < 9 * 60:
    time.sleep(45)
    log(snap(api("/api/status"), {"exp": 2, "sec": round(time.time() - t0)}))

# ── 3: latency ladder, Fox<->ION alternating ───────────────────────
log({"event": "exp", "exp": "3_latency_ladder"})
lat = []
for i, hop in enumerate([
        {"rf": 36, "prog": 3, "virt": "5.1", "name": "Fox"},
        {"rf": 35, "prog": 3, "virt": "66.1", "name": "ION"},
        {"rf": 36, "prog": 3, "virt": "5.1", "name": "Fox"},
        {"rf": 35, "prog": 3, "virt": "66.1", "name": "ION"}]):
    st = tune(hop)
    lat.append(st.get("_tune_secs"))
    log({"exp": 3, "hop": i + 1, "virt": hop["virt"],
         "tune_secs": st.get("_tune_secs"), "tuned": st.get("tuned")})
    time.sleep(120)
    log(snap(api("/api/status"), {"exp": 3, "hop": i + 1, "settled": True}))
log({"exp": 3, "event": "latency_summary", "tune_secs_all": lat})

# ── 4: VHF telemetry probe via meter mode ──────────────────────────
for rf in (7, 9):
    log({"event": "exp", "exp": f"4_vhf_rf{rf}"})
    api("/api/meter", {"rf": rf})
    time.sleep(30)
    t0 = time.time()
    while time.time() - t0 < 3.5 * 60:
        time.sleep(20)
        m = api("/api/meter")
        log({"exp": 4, "rf": rf, "sec": round(time.time() - t0),
             "mer_last": m.get("mer_last"), "in_rms": m.get("in_rms"),
             "max_x": m.get("max_x")})
api("/api/meter/stop", {})

# leave the evening's healthiest channel playing
st = tune({"rf": 36, "prog": 3, "virt": "5.1", "name": "WTTG Fox 5"})
log({"event": "evening_lab_complete", "final_channel": "5.1"})
print(f"\n=== EVENING LAB COMPLETE — results: {OUT} ===", flush=True)
