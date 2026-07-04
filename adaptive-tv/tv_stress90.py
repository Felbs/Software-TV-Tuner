"""tv_stress90.py — 90-minute scientific stress campaign via the panel API.

Five phases, each targeting an open question about running perfectly on
any antenna:
  A  baseline circuit (reproducibility)
  B  30-min endurance soak (runtime drift: leaks, buffer creep)
  C  RF15 collapse lab (classify the repeat offender's failure shape)
  D  same-mux program hops (extractor coverage + hop-latency baseline)
  E  repeat circuit + final soak (evening drift, per-tower)

Every tune logs its latency (tune->watching). On RADIO FAILED the script
restarts the SDRplay API service and retries once. Results in
lab/stress90_<stamp>.jsonl; flight recorder runs in parallel.
"""
import json
import subprocess
import time
import urllib.request

BASE = "http://127.0.0.1:8642"
OUT = r"Z:\src\adaptive-tv\lab\stress90_%s.jsonl" % time.strftime("%Y%m%d_%H%M")

CIRCUIT = [
    {"rf": 36, "prog": 3, "virt": "5.1",  "name": "WTTG Fox"},
    {"rf": 34, "prog": 3, "virt": "4.1",  "name": "WRC NBC"},
    {"rf": 15, "prog": 1, "virt": "14.1", "name": "WFDC Univision"},
    {"rf": 35, "prog": 3, "virt": "66.1", "name": "ION"},
]


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


def restart_sdr_service():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Restart-Service SDRplayAPIService -Force"],
                   capture_output=True, timeout=60)
    time.sleep(4)


def tune(hop, timeout=180):
    """Tune with latency measurement and one wedge-recovery retry."""
    for attempt in (1, 2):
        t0 = time.time()
        api("/api/tune", hop)
        while time.time() - t0 < timeout:
            st = api("/api/status")
            if st.get("tuned"):
                st["_tune_secs"] = round(time.time() - t0, 1)
                st["_attempt"] = attempt
                return st
            stage = st.get("stage") or ""
            if stage.startswith("RADIO FAILED"):
                log({"event": "radio_failed", "hop": hop["virt"],
                     "attempt": attempt})
                if attempt == 1:
                    restart_sdr_service()
                break
            if stage.startswith("PLAYER"):
                st["_tune_secs"] = round(time.time() - t0, 1)
                st["_attempt"] = attempt
                st["_no_player"] = True
                return st
            time.sleep(4)
    return api("/api/status")


def circuit(tag, settle=120):
    out = []
    for hop in CIRCUIT:
        st = tune(hop)
        if not st.get("tuned"):
            log({"phase": tag, "hop": hop["virt"], "result": "TUNE FAILED",
                 "stage": st.get("stage")})
            continue
        lat = st.get("_tune_secs")
        time.sleep(settle)
        st = api("/api/status")
        rec = snap(st, {"phase": tag, "hop": hop["virt"],
                        "name": hop["name"], "tune_secs": lat,
                        "result": "ok"})
        log(rec)
        out.append(rec)
    return out


T_START = time.time()
log({"event": "campaign_start", "plan": "A circuit / B 30min soak / "
     "C RF15 lab / D RF36 program hops / E circuit + soak"})

# ── Phase A: baseline circuit ──────────────────────────────────────
log({"event": "phase", "phase": "A_baseline"})
res_a = circuit("A")

# ── Phase B: 30-min endurance soak on the cleanest hop ─────────────
good = [r for r in res_a if (r.get("hdrs_s") or 0) > 3]
best_virt = sorted(good, key=lambda r: ((r.get("gaps_min") or 99),
                                        -(r.get("hdrs_s") or 0)))[0]["hop"] \
            if good else "5.1"
best_hop = next(h for h in CIRCUIT if h["virt"] == best_virt)
log({"event": "phase", "phase": "B_endurance", "channel": best_virt})
st = tune(best_hop)
t_b = time.time()
while time.time() - t_b < 30 * 60:
    time.sleep(60)
    st = api("/api/status")
    log(snap(st, {"phase": "B", "min": round((time.time() - t_b) / 60)}))

# ── Phase C: RF15 collapse lab ─────────────────────────────────────
log({"event": "phase", "phase": "C_rf15_lab"})
rf15 = next(h for h in CIRCUIT if h["rf"] == 15)
st = tune(rf15)
t_c = time.time()
retuned = False
while time.time() - t_c < 12 * 60:
    time.sleep(20)
    st = api("/api/status")
    log(snap(st, {"phase": "C", "sec": round(time.time() - t_c),
                  "retuned": retuned}))
    if not retuned and time.time() - t_c > 6 * 60:
        log({"event": "C_midpoint_retune"})
        st = tune(rf15)
        log(snap(api("/api/status"),
                 {"phase": "C", "event": "post_retune",
                  "tune_secs": st.get("_tune_secs")}))
        retuned = True

# ── Phase D: every program on RF36 (extractor coverage + latency) ──
log({"event": "phase", "phase": "D_rf36_programs"})
try:
    rows = api("/api/grid")["rows"]
    rf36_rows = [r for r in rows if r["rf"] == 36][:5]
except Exception:
    rf36_rows = []
for r in rf36_rows:
    hop = {"rf": 36, "prog": r["prog"], "virt": r["virtual"],
           "name": r["callsign"]}
    st = tune(hop)
    if not st.get("tuned"):
        log({"phase": "D", "hop": hop["virt"], "result": "TUNE FAILED",
             "stage": st.get("stage")})
        continue
    lat = st.get("_tune_secs")
    time.sleep(90)
    log(snap(api("/api/status"),
             {"phase": "D", "hop": hop["virt"], "name": hop["name"],
              "tune_secs": lat,
              "no_player": bool(st.get("_no_player"))}))

# ── Phase E: evening-drift circuit + final soak ────────────────────
log({"event": "phase", "phase": "E_drift_circuit"})
res_e = circuit("E")
good = [r for r in res_e if (r.get("hdrs_s") or 0) > 3]
final_virt = sorted(good, key=lambda r: ((r.get("gaps_min") or 99),
                                         -(r.get("hdrs_s") or 0)))[0]["hop"] \
             if good else best_virt
final_hop = next(h for h in CIRCUIT if h["virt"] == final_virt)
log({"event": "phase", "phase": "E_final_soak", "channel": final_virt})
st = tune(final_hop)
while time.time() - T_START < 88 * 60:
    time.sleep(60)
    st = api("/api/status")
    log(snap(st, {"phase": "E_soak",
                  "min": round((time.time() - T_START) / 60)}))

log({"event": "campaign_complete",
     "total_min": round((time.time() - T_START) / 60)})
print(f"\n=== 90-MIN CAMPAIGN COMPLETE — results: {OUT} ===", flush=True)
