"""tv_stress.py — channel-hopping stress test driven through the panel API.

Exercises the full production path (panel -> tune -> AGC -> cliff-detect ->
extractor -> player) exactly as a user's clicks would, then soaks on the
best channel. Results land in lab/stress_<date>.jsonl and the flight
recorder captures the fine-grained telemetry in parallel.

Usage: python tv_stress.py [settle_secs] [soak_mins]
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8642"
OUT = r"Z:\src\adaptive-tv\lab\stress_%s.jsonl" % time.strftime("%Y%m%d_%H%M")

# one representative station per locked mux (panel grid order)
HOPS = [
    {"rf": 36, "prog": 3, "virt": "5.1",  "name": "WTTG Fox"},
    {"rf": 34, "prog": 3, "virt": "4.1",  "name": "WRC NBC"},
    {"rf": 15, "prog": 1, "virt": "14.1", "name": "WFDC Univision"},
    {"rf": 35, "prog": 3, "virt": "66.1", "name": "ION"},
]

SETTLE = int(sys.argv[1]) if len(sys.argv) > 1 else 120
SOAK_MIN = int(sys.argv[2]) if len(sys.argv) > 2 else 15


def api(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def log(rec):
    rec["iso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)


def wait_tuned(timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = api("/api/status")
        if st.get("tuned"):
            return st
        stage = st.get("stage") or ""
        if stage.startswith("RADIO FAILED") or stage.startswith("PLAYER"):
            return st
        time.sleep(4)
    return api("/api/status")


results = []
for hop in HOPS:
    print(f"\n=== HOP {hop['virt']} {hop['name']} (RF{hop['rf']}) ===",
          flush=True)
    api("/api/tune", hop)
    st = wait_tuned()
    if not st.get("tuned"):
        log({"hop": hop["virt"], "result": "TUNE FAILED",
             "stage": st.get("stage")})
        continue
    time.sleep(SETTLE)
    st = api("/api/status")
    rec = {"hop": hop["virt"], "name": hop["name"], "rf": hop["rf"],
           "result": "ok",
           "mer_db": st.get("mer_db"), "mer_last": st.get("mer_last"),
           "hdrs_s": st.get("hdrs_s"), "gaps_min": st.get("gaps_min"),
           "real_pct": st.get("real_pct"), "in_rms": st.get("in_rms"),
           "max_x": st.get("max_x")}
    log(rec)
    results.append(rec)

# soak on the cleanest hop (fewest gaps, then most headers)
good = [r for r in results if (r.get("hdrs_s") or 0) > 3]
if good:
    best = sorted(good, key=lambda r: ((r.get("gaps_min") or 99),
                                       -(r.get("hdrs_s") or 0)))[0]
    hop = next(h for h in HOPS if h["virt"] == best["hop"])
    print(f"\n=== SOAK {hop['virt']} {hop['name']} for {SOAK_MIN} min ===",
          flush=True)
    api("/api/tune", hop)
    st = wait_tuned()
    t0 = time.time()
    while time.time() - t0 < SOAK_MIN * 60:
        time.sleep(60)
        st = api("/api/status")
        log({"soak": hop["virt"], "min": round((time.time() - t0) / 60),
             "mer_db": st.get("mer_db"), "hdrs_s": st.get("hdrs_s"),
             "gaps_min": st.get("gaps_min"),
             "real_pct": st.get("real_pct")})
    print("\n=== STRESS TEST COMPLETE ===", flush=True)
else:
    print("no hop produced video — soak skipped", flush=True)
print(f"results: {OUT}", flush=True)
