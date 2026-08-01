"""live_bench.py — the LIVE gate for lever 1 (warm start) and lever 3
(data recycling), off the real radio, one fresh tv_live process per trial.

This is the path a user actually takes: the panel's classic tune and the
scanner's per-candidate spawn both start a NEW process. Per trial we record

  fields_to_target   field syncs until fs_err_rms stays at/below the bar
                     (the same absolute ruler the offline bench uses)
  frames             ffmpeg null-sink -map 0:v on the captured TS
  oso                SDR source overflows — MUST be 0 to promote anything
                     live (drizzle_wave_interferer law)
  warm/cold          which start the equalizer got

Arms:
  cold      cache directory wiped before the trial
  warm      cache directory left populated by the preceding cold trial
  rec8      cold cache + STVT_EQ_RECYCLE=8 (window 40) — lever 3's live gate
  base      no cache at all, no recycling = today's production behaviour

Usage:
  python lab/speed_build/live_bench.py --rf 34 --secs 30 --trials 3
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PY = r"C:\Users\user\radioconda\python.exe"
WORK = REPO / "lab" / "speed_build" / "live"
LEDGER = REPO / "lab" / "speed_build" / "live.jsonl"
sys.path.insert(0, r"Z:\src\gr-radiotuna\tools")

BASE = {
    "STVT_VITERBI": "soft", "STVT_RS": "erasure", "STVT_SOVA": "1",
    "STVT_FPLL_FOLD": "1", "STVT_ANTENNA": "Antenna B", "STVT_BIAST": "1",
    "STVT_SDR_AGC": "1", "STVT_AGC_SETPOINT": "-20",
    "STVT_EQ": "long", "STVT_EQ_TELEM": "1", "STVT_EQ_TELEM_EVERY": "1",
    "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
}
RE_EQ = re.compile(r"\[eq-long t=\s*([\d.]+)s\] fs=(\d+) fs_err_rms=([\d.]+)")
RE_FRAME = re.compile(r"frame=\s*(\d+)")
RE_OSO = re.compile(r"\bOs?O\b|overflow", re.I)


def hygiene():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\""
                    " | Where-Object {$_.CommandLine -match 'tv_live'} |"
                    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
                    " ForEach-Object { Stop-Process -Id $_.ProcessId -Force"  # kill-ok (see above)
                    " -Confirm:$false }"], capture_output=True, timeout=60)  # pipe-ok: control cmd - nothing is read from the pipe
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Restart-Service -Name SDRplayAPIService -Force "
                    "-Confirm:$false"], capture_output=True, timeout=120)  # pipe-ok: control cmd - nothing is read from the pipe
    time.sleep(12)


def frames(ts: Path) -> int:
    if not ts.exists() or ts.stat().st_size < 2_000_000:
        return 0
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
         "-err_detect", "ignore_err", "-analyzeduration", "100M",
         "-probesize", "100M", "-i", str(ts), "-map", "0:v", "-f", "null", "-"],
        capture_output=True, text=True)
    m = RE_FRAME.findall(r.stderr)
    return int(m[-1]) if m else 0


def first_stable(rows, target, run_len=5):
    run = 0
    for _t, fs, err in rows:
        if err <= target:
            run += 1
            if run >= run_len:
                return fs
        else:
            run = 0
    return None


def trial(arm, rf, secs, cache_dir, radio_lock):
    WORK.mkdir(parents=True, exist_ok=True)
    ts = WORK / f"{arm}_rf{rf}.ts"
    log = WORK / f"{arm}_rf{rf}.log"
    env = dict(os.environ, **BASE)
    if arm in ("cold", "warm", "rec8"):
        env["STVT_EQ_TAP_CACHE"] = str(cache_dir)
    else:
        env.pop("STVT_EQ_TAP_CACHE", None)
        env.pop("STVT_EQ_TAP_CACHE_FILE", None)
    if arm == "rec8":
        env["STVT_EQ_RECYCLE"] = "8"
        env["STVT_EQ_RECYCLE_FIELDS"] = "40"
    hygiene()
    with open(log, "w", encoding="utf-8") as lf:
        p = subprocess.Popen([PY, str(REPO / "tools" / "tv_live.py"),
                              "--rf", str(rf), "--out", str(ts)],
                             cwd=str(REPO), env=env, stdout=lf,
                             stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        t_end = time.time() + secs
        while time.time() < t_end and p.poll() is None:
            time.sleep(2)
            if radio_lock:
                try:
                    radio_lock.heartbeat()
                except Exception:
                    pass
        try:
            p.send_signal(signal.CTRL_BREAK_EVENT)
            p.wait(30)
        except Exception:
            p.kill()
    txt = log.read_text(errors="replace")
    rows = [(float(a), int(b), float(c)) for a, b, c in RE_EQ.findall(txt)]
    return {
        "arm": arm, "rf": rf, "secs": secs, "fields": len(rows),
        "frames": frames(ts),
        "oso": len(RE_OSO.findall(txt)),
        "warm": txt.count("[eq-long] WARM START"),
        "cold": txt.count("[eq-long] COLD START"),
        "recycle": txt.count("DATA RECYCLING"),
        "rows": rows,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=34)
    ap.add_argument("--secs", type=float, default=30.0)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--target", type=float, default=0.5179)
    ap.add_argument("--arms", default="base,cold,warm,rec8",
                    help="comma-separated arm order (order matters for "
                         "airtime-drift controls)")
    ap.add_argument("--no-lock", action="store_true")
    a = ap.parse_args()

    cache_dir = WORK / "tapcache"
    radio_lock = holder = None
    if not a.no_lock:
        import radio_lock as _rl
        radio_lock = _rl
        holder = radio_lock.Holder("speed1-live", "warm/recycle live gate",
                                   80, wait_s=300)
        holder.__enter__()
        if not holder.ok:
            print("[live] could not take the warden lock — standing down")
            return 2
    out = []
    try:
        for k in range(1, a.trials + 1):
            shutil.rmtree(cache_dir, ignore_errors=True)
            cache_dir.mkdir(parents=True, exist_ok=True)
            for arm in [x.strip() for x in a.arms.split(",")]:
                if arm == "rec8":
                    # rec8 must start from an empty cache so the measurement
                    # is of RECYCLING, not of the warm start
                    shutil.rmtree(cache_dir, ignore_errors=True)
                    cache_dir.mkdir(parents=True, exist_ok=True)
                r = trial(arm, a.rf, a.secs, cache_dir, radio_lock)
                fs = first_stable(r["rows"], a.target)
                r["fields_to_target"] = fs
                mer = (20 * math.log10(5.0 / statistics.median(
                    [e for _t, _f, e in r["rows"][len(r["rows"])//2:]]))
                    if r["rows"] else None)
                r["mer_db"] = round(mer, 2) if mer else None
                rec = {kk: vv for kk, vv in r.items() if kk != "rows"}
                rec["trial"] = k
                out.append(rec)
                with open(LEDGER, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
                print(f"  [t{k} {arm:<5}] fields={r['fields']:>5} "
                      f"to_target={fs} frames={r['frames']:>5} "
                      f"MER={r['mer_db']} OsO={r['oso']} "
                      f"warm={r['warm']} cold={r['cold']} "
                      f"rec={r['recycle']}", flush=True)
    finally:
        if holder:
            try:
                holder.__exit__(None, None, None)
            except Exception:
                pass
    print("\n=== medians ===")
    for arm in [x.strip() for x in a.arms.split(",")]:
        rs = [r for r in out if r["arm"] == arm]
        if not rs:
            continue
        ft = [r["fields_to_target"] for r in rs if r["fields_to_target"]]
        print(f"  {arm:<5} n={len(rs)} fields_to_target="
              f"{statistics.median(ft) if ft else None} "
              f"frames={statistics.median([r['frames'] for r in rs])} "
              f"MER={statistics.median([r['mer_db'] for r in rs if r['mer_db']])} "
              f"OsO={[r['oso'] for r in rs]}")


if __name__ == "__main__":
    sys.exit(main() or 0)
