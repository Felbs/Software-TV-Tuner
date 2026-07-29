"""retune_bench.py — stopwatch the PERSISTENT-RETUNE channel change, warm
cache vs cache-less, off the real radio.

One tv_live process is started with STVT_PERSIST_RETUNE=1 and then walked
around a channel ladder by writing retune.cmd. For every transition we time,
from the instant the command file lands:

  t_ack     the chain acknowledged the retune (retune.ack)
  t_video   the FIRST MPEG sequence header (00 00 01 B3) in the freshly
            truncated live.ts = video is flowing again. This is the
            user-visible channel-change time and the headline number.

and we record, per transition, the equalizer's own story from the chain log
(WARM START / COLD START banner + the fs_err_rms curve that follows) plus the
SDR overflow count (OsO) for the whole run, because no live promotion counts
without OsO == 0 (drizzle_wave_interferer law).

  --arm cacheless   STVT_PERSIST_RETUNE_CACHE=0 = today's behaviour, the
                    retuned chain runs with no warm start at all
  --arm warm        the shipped speed-1 path: save / rebind / warm-load

Usage (holds the warden itself):
  python lab/speed_build/retune_bench.py --arm warm --ladder 36,34,31 \
      --laps 2 --dwell 20 --label after
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PY = r"C:\Users\user\radioconda\python.exe"
WORK = REPO / "lab" / "speed_build" / "retune"
LEDGER = REPO / "lab" / "speed_build" / "retune.jsonl"
SEQ = b"\x00\x00\x01\xb3"

sys.path.insert(0, r"Z:\src\gr-radiotuna\tools")

# Same chain env the day-program ladder uses, so the comparison is against a
# configuration already known to decode on this antenna.
BASE = {
    "STVT_VITERBI": "soft", "STVT_RS": "erasure", "STVT_SOVA": "1",
    "STVT_FPLL_FOLD": "1", "STVT_ANTENNA": "Antenna B", "STVT_BIAST": "1",
    "STVT_SDR_AGC": "1", "STVT_AGC_SETPOINT": "-20",
    "STVT_PERSIST_RETUNE": "1",
    "STVT_EQ_TELEM": "1", "STVT_EQ_TELEM_EVERY": "1",
    "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
}

RE_EQ = re.compile(r"fs_err_rms=([\d.]+)")


def hygiene():
    """The 7/29 three-layer contention lesson: sweep strays that squat the
    radio, bounce the SDRplay service, settle."""
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\""
                    " | Where-Object {$_.CommandLine -match 'tv_live'} |"
                    " ForEach-Object { Stop-Process -Id $_.ProcessId -Force"
                    " -Confirm:$false }"], capture_output=True, timeout=60)
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Restart-Service -Name SDRplayAPIService -Force "
                    "-Confirm:$false"], capture_output=True, timeout=120)
    time.sleep(12)


def first_seq_header(ts: Path, deadline: float, t0: float, size0: int):
    """Poll live.ts for the first MPEG sequence header in FRESH bytes.

    retune() reopens the file_sink on the same path, which truncates to
    offset 0 at the next work() call. Scanning before that would find the
    PREVIOUS channel's headers and report a bogus sub-100 ms channel change,
    so first wait for the size to collapse below where it was. Returns
    (t_truncate, t_video) in seconds from t0."""
    # Two cases, both handled: the sink DID reopen (size collapses, fresh
    # bytes start at 0) or it did not (fresh bytes start at size0). Either
    # way we only ever look at bytes written after the command landed.
    t_trunc = None
    base = size0
    seen = 0
    tail = b""
    while time.time() < deadline:
        try:
            sz = ts.stat().st_size
        except OSError:
            time.sleep(0.01)
            continue
        if t_trunc is None and sz < size0:
            t_trunc = time.time() - t0
            base = 0
            seen = 0
            tail = b""
        if sz <= base + seen:
            time.sleep(0.01)
            continue
        try:
            with open(ts, "rb") as f:
                f.seek(base + seen)
                chunk = f.read(1 << 21)
        except OSError:
            time.sleep(0.01)
            continue
        if not chunk:
            time.sleep(0.01)
            continue
        if SEQ in (tail + chunk):
            return t_trunc, time.time() - t0
        seen += len(chunk)
        tail = chunk[-3:]
    return t_trunc, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["warm", "cacheless"], required=True)
    ap.add_argument("--ladder", default="36,34,31")
    ap.add_argument("--laps", type=int, default=2)
    ap.add_argument("--dwell", type=float, default=20.0)
    ap.add_argument("--label", default="")
    ap.add_argument("--cache-dir", default=str(WORK / "tapcache"))
    ap.add_argument("--no-lock", action="store_true")
    a = ap.parse_args()

    ladder = [int(x) for x in a.ladder.split(",")]
    WORK.mkdir(parents=True, exist_ok=True)
    Path(a.cache_dir).mkdir(parents=True, exist_ok=True)
    ts = WORK / "live.ts"
    cmd_f = WORK / "retune.cmd"
    ack_f = WORK / "retune.ack"
    log = WORK / f"chain_{a.arm}_{a.label or 'x'}.log"
    for p in (cmd_f, ack_f):
        try:
            p.unlink()
        except OSError:
            pass

    env = dict(os.environ, **BASE)
    env["STVT_EQ_TAP_CACHE"] = a.cache_dir
    if a.arm == "cacheless":
        env["STVT_PERSIST_RETUNE_CACHE"] = "0"

    holder = radio_lock = None
    if not a.no_lock:
        import radio_lock as _rl
        radio_lock = _rl
        holder = radio_lock.Holder("speed1-retune", "retune stopwatch", 80,
                                   wait_s=300)
        holder.__enter__()
        if not holder.ok:
            print("[bench] could not take the warden lock — standing down",
                  flush=True)
            return 2
    try:
        hygiene()
        rows = []
        with open(log, "w", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                [PY, str(REPO / "tools" / "tv_live.py"),
                 "--rf", str(ladder[0]), "--out", str(ts)],
                cwd=str(REPO), env=env, stdout=lf,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            try:
                # let the first channel come up and (in the warm arm) write
                # its cache before anything is timed
                print(f"[bench] {a.arm}: starting on RF{ladder[0]}, "
                      f"settling {a.dwell:.0f}s", flush=True)
                t_end = time.time() + a.dwell
                while time.time() < t_end and proc.poll() is None:
                    time.sleep(1)
                    if holder:
                        holder.heartbeat() if hasattr(holder, "heartbeat") \
                            else None
                if proc.poll() is not None:
                    raise RuntimeError("tv_live exited during warm-up — "
                                       "see " + str(log))
                seq = [rf for _ in range(a.laps) for rf in ladder[1:] + ladder[:1]]
                for i, rf in enumerate(seq, start=1):
                    mark = log.stat().st_size
                    try:
                        size0 = ts.stat().st_size
                    except OSError:
                        size0 = 0
                    t0 = time.time()
                    tmp = cmd_f.with_suffix(".tmp")
                    tmp.write_text(json.dumps({"rf": rf}), encoding="utf-8")
                    os.replace(tmp, cmd_f)
                    t_ack = None
                    dl = t0 + 15
                    while time.time() < dl:
                        if ack_f.exists():
                            t_ack = time.time() - t0
                            try:
                                ack_f.unlink()
                            except OSError:
                                pass
                            break
                        time.sleep(0.01)
                    t_trunc, t_vid = first_seq_header(ts, t0 + 40, t0,
                                                      size0)
                    # equalizer's own story for this transition
                    tail = log.read_text(errors="replace")[mark:]
                    warm = "WARM START" in tail
                    cold = "COLD START" in tail
                    errs = [float(x) for x in RE_EQ.findall(tail)][:40]
                    rec = {"arm": a.arm, "label": a.label, "i": i, "rf": rf,
                           "t_ack": round(t_ack, 3) if t_ack else None,
                           "t_trunc": round(t_trunc, 3) if t_trunc else None,
                           "t_video": round(t_vid, 3) if t_vid else None,
                           "warm_start": warm, "cold_start": cold,
                           "eq_err_first20": [round(e, 4) for e in errs[:20]]}
                    rows.append(rec)
                    print(f"  [{i}/{len(seq)}] -> RF{rf}: ack "
                          f"{rec['t_ack']}s  trunc {rec['t_trunc']}s  "
                          f"VIDEO {rec['t_video']}s  "
                          f"{'WARM' if warm else ('COLD' if cold else '-')}"
                          f"  err[0:3]={rec['eq_err_first20'][:3]}",
                          flush=True)
                    t_end = time.time() + a.dwell
                    while time.time() < t_end and proc.poll() is None:
                        time.sleep(1)
                        if radio_lock:
                            try:
                                radio_lock.heartbeat()
                            except Exception:
                                pass
                    if proc.poll() is not None:
                        print("  !! tv_live exited early", flush=True)
                        break
            finally:
                try:
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                    proc.wait(30)
                except Exception:
                    proc.kill()
        txt = log.read_text(errors="replace")
        oso = len(re.findall(r"\bOs?O\b|overflow", txt, re.I))
        vids = [r["t_video"] for r in rows if r["t_video"]]
        summary = {"arm": a.arm, "label": a.label, "n": len(rows),
                   "oso": oso,
                   "t_video_median": round(statistics.median(vids), 3) if vids else None,
                   "t_video_all": vids,
                   "warm_hits": sum(1 for r in rows if r["warm_start"]),
                   "cold_hits": sum(1 for r in rows if r["cold_start"]),
                   "rows": rows}
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary) + "\n")
        print(f"\n[bench] {a.arm}/{a.label}: n={len(rows)} "
              f"t_video median {summary['t_video_median']}s "
              f"all={vids} warm={summary['warm_hits']} "
              f"cold={summary['cold_hits']} OsO={oso}", flush=True)
    finally:
        if holder:
            try:
                holder.__exit__(None, None, None)
            except Exception:
                pass


if __name__ == "__main__":
    main()
