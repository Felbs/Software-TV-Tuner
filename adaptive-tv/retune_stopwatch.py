"""retune_stopwatch.py — stopwatch a panel tune transition end-to-end.

Measures, from the /api/tune POST:
  t_reset   live.ts unlinked (classic) or truncated (persistent rotate)
  t_video   first MPEG seq header (00 00 01 B3) in FRESH bytes = video flowing
  t_hdrs    /api/status hdrs_s > 0 (the panel's own metric; gated on the
            20 MB ts_metrics threshold, so it trails t_video by ~8 s of mux)
  t_done    stage_pct == 100 and not tuning
then settles `--settle` s and reads mer_db / loss_pct / hdrs_s / gaps_min,
and verifies the extracted program: live_solo.ts video PID vs pid_cache.

Usage:
  python retune_stopwatch.py RF PROG VIRT NAME ANTENNA [--label X] [--settle 25]

Rows append to lab/retune_stopwatch.jsonl.
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

PANEL = "http://127.0.0.1:8642"
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
SOLO = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live_solo.ts")
HERE = Path(__file__).resolve().parent
OUT = HERE / "retune_stopwatch.jsonl"
PID_CACHE = HERE / "pid_cache.json"
SEQ = b"\x00\x00\x01\xb3"


def api(path, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(PANEL + path, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait_radio_free():
    """time_trainer may have a scan mid-flight — wait it out."""
    while True:
        s = api("/api/status")
        if not s["scan"]["running"]:
            return s
        print(f"  [wait] scan running ({s['scan']['pct']}%) — waiting…",
              flush=True)
        time.sleep(10)


def verify_program(rf, prog):
    """Right mux + right program? Probe live.ts PSI: the requested
    program_id must exist, its service_name comes back for the report,
    and the pid_cache vpid (if known) must be among its PIDs. (The solo
    remux renumbers PIDs, so live.ts PSI is the honest identity check;
    solo-side mapping is tv_watch's own verified PID logic.)"""
    try:
        cache = json.loads(PID_CACHE.read_text(encoding="utf-8"))
        vpid = cache.get(f"{rf}:{prog}", {}).get("vpid")
    except (OSError, ValueError):
        vpid = None
    try:
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-probesize", "6000000", "-analyzeduration", "2000000",
             "-show_programs", str(LIVE)],
            capture_output=True, text=True, timeout=30)
        progs = json.loads(pr.stdout or "{}").get("programs", [])
        mine = next((p for p in progs if p.get("program_id") == prog), None)
        if not mine:
            return {"ok": False, "why": f"program {prog} not in live.ts PMT"}
        svc = (mine.get("tags") or {}).get("service_name", "?")
        pids = []
        for s in mine.get("streams", []):
            sid = s.get("id")
            if sid is not None:
                pids.append(int(str(sid), 16) if str(sid).startswith("0x")
                            else int(sid))
        ok = (vpid in pids) if (vpid is not None and pids) else True
        return {"ok": ok, "service": svc, "vpid_match": vpid in pids
                if vpid is not None else None}
    except Exception as e:
        return {"ok": None, "why": str(e)[:80]}


def measure(rf, prog, virt, name, ant, label, settle, timeout):
    wait_radio_free()
    try:
        prev_size = LIVE.stat().st_size
    except OSError:
        prev_size = None
    print(f"[{time.strftime('%H:%M:%S')}] TUNE {label}: RF{rf} p{prog} "
          f"{virt} {name} ant={ant}", flush=True)
    t0 = time.time()
    api("/api/tune", {"rf": rf, "prog": prog, "virt": virt,
                      "name": name, "antenna": ant})
    t_reset = t_video = t_hdrs = t_done = None
    ofs = 0
    carry = b""
    stages = []
    last_stage = None
    while time.time() - t0 < timeout:
        now = time.time() - t0
        # ── live.ts reset + fresh-bytes video detector ──
        try:
            sz = LIVE.stat().st_size
        except OSError:
            sz = None
            if t_reset is None and prev_size is not None:
                t_reset = now          # unlinked (classic path)
                ofs, carry = 0, b""
        if sz is not None:
            if t_reset is None and (prev_size is None or sz < prev_size):
                t_reset = now          # truncated (persistent rotate)
                ofs, carry = 0, b""
            prev_size = max(sz, prev_size or 0) if t_reset is None else sz
            if t_reset is not None and t_video is None and sz > ofs:
                try:
                    with open(LIVE, "rb") as f:
                        f.seek(ofs)
                        chunk = f.read(4_000_000)
                    if SEQ in carry + chunk:
                        t_video = now
                    carry = chunk[-3:]
                    ofs += len(chunk)
                except OSError:
                    pass
        # ── panel status ──
        try:
            s = api("/api/status", timeout=5)
        except Exception:
            time.sleep(0.5)
            continue
        stg = (s.get("stage") or "")[:90]
        if stg and stg != last_stage:
            stages.append([round(now, 1), stg])
            last_stage = stg
        if t_hdrs is None and s.get("tuned") and (s.get("hdrs_s") or 0) > 0:
            t_hdrs = now
        if t_done is None and not s.get("tuning") and s.get("tuned") \
                and (s.get("stage_pct") or 0) >= 100:
            t_done = now
        if s.get("stage", "").startswith(("RADIO FAILED", "PLAYER")):
            stages.append([round(now, 1), "FAIL: " + s["stage"][:70]])
            break
        if t_hdrs is not None and t_done is not None:
            break
        time.sleep(0.5)
    print(f"  t_reset={t_reset and round(t_reset,1)}  "
          f"t_video={t_video and round(t_video,1)}  "
          f"t_hdrs={t_hdrs and round(t_hdrs,1)}  "
          f"t_done={t_done and round(t_done,1)}", flush=True)
    for t, stg in stages:
        print(f"    {t:6.1f}s  {stg}", flush=True)
    print(f"  settling {settle}s for health read…", flush=True)
    time.sleep(settle)
    try:
        s = api("/api/status")
        health = {k: s.get(k) for k in
                  ("mer_db", "loss_pct", "hdrs_s", "gaps_min", "real_pct")}
    except Exception:
        health = {}
    pid_ok = verify_program(rf, prog)
    row = {"iso": time.strftime("%Y-%m-%dT%H:%M:%S"), "label": label,
           "rf": rf, "prog": prog, "virt": virt, "ant": ant,
           "t_reset": t_reset, "t_video": t_video, "t_hdrs": t_hdrs,
           "t_done": t_done, "stages": stages, "health": health,
           "pid_ok": pid_ok,
           "persist_flag": os.environ.get("STVT_PERSIST_RETUNE", "")}
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    print(f"  health after settle: {health}  pid_ok={pid_ok}", flush=True)
    return row


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("rf", type=int)
    ap.add_argument("prog", type=int)
    ap.add_argument("virt")
    ap.add_argument("name")
    ap.add_argument("ant")
    ap.add_argument("--label", default="")
    ap.add_argument("--settle", type=int, default=25)
    ap.add_argument("--timeout", type=int, default=240)
    a = ap.parse_args()
    measure(a.rf, a.prog, a.virt, a.name, a.ant,
            a.label or f"RF{a.rf}p{a.prog}", a.settle, a.timeout)
