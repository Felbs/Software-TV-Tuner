"""night_lab.py — the slow overnight campaign (2026-07-07, Fable runway
to 7/12 so patience is the design goal, not speed).

Mission set by the user: collect the data that makes MARGINAL decoding
better, and prove the upgraded SCANNER (MER early-verdict) faster and
more accurate. Phases:

  -1  dawn forecast (00Z balloon) + FM beacon oracle          (~4 min)
   0  SCANNER BENCHMARK: full scan on each antenna with the new
      MER-verdict scanner; timing + per-channel MER into the map
   1  SPECIMEN-HUNTING CUBE until 04:45: hourly MER samples across
      the antenna x channel grid; any dwell in the MID-CLIFF band
      (12.5-16.2 dB) triggers a 45 s RAM IQ specimen — building the
      marginal-signal testbed library the replay lab is starving for
   2  E7 BATCH at ~04:45: 3-pass vote on every specimen taken tonight;
      heal-rate vs MER is the "when does voting help" curve
   3  05:00 ambush3 (unchanged proven dawn hunt, Philips RF9)

Stop early: create lab/night_lab/STOP. All events -> night_lab.jsonl.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import overnight_cube as oc                      # noqa: E402

PY = sys.executable
LAB = HERE / "lab" / "night_lab"
LAB.mkdir(parents=True, exist_ok=True)
STOP = LAB / "STOP"
LOG = HERE / "night_lab.jsonl"
CAPS = HERE / "lab" / "captures"
TOOLS = Path(r"Z:\src\magic-tv-decoder\tools")

GAINS = {36: (3, 40), 34: (2, 32), 15: (1, 32), 7: (5, 32), 9: (5, 32),
         21: (2, 32)}
DEFAULT_GAIN = (3, 40)
# port map since 7/07 rewire: rabbit=A, philips=B, discone=C
GRID = [("Antenna B", "philips", [9, 15, 21, 34, 36, 7]),
        ("Antenna A", "rabbit", [21, 34, 9, 7]),
        ("Antenna C", "discone", [7, 9])]
MID_CLIFF = (12.5, 16.2)      # the band worth a specimen
MAX_SPECIMENS = 8             # ~12 GB disk budget
CUBE_END = "04:45"
AMBUSH_AT = "05:00"


def log_event(o):
    o["t"] = time.strftime("%H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(o) + "\n")
    print(f"[night_lab {o['t']}] {o}", flush=True)


def stopped():
    return STOP.exists()


def capture_specimen(rf, port, rfg, ifgr, out, secs=45):
    """Capture IQ with the DUD-EATER discipline. oc.sample() just killed
    the chain, so the SDR needs a beat to release and attempt #1 after a
    kill is a near-guaranteed wedge. BUG FOUND 2026-07-08: the old
    immediate subprocess.run fired into the un-released SDR, failed to
    open in ~1s, wrote no file, and was silently skipped -> THREE nights
    of zero specimens while marginal signals were plainly present
    (RF7 discone 14.54 dB @ 20:08). Settle, then retry."""
    time.sleep(8)                       # let the SDR fully release
    for attempt in (1, 2, 3):
        try:
            out.unlink()
        except OSError:
            pass
        if attempt == 3:
            # deep-wedge cure before the last try (the post-kill SDR
            # sometimes won't release without a service bounce)
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Restart-Service SDRplayAPIService -Force"],
                    capture_output=True, timeout=60)
                time.sleep(6)
            except Exception:
                pass
        try:
            r = subprocess.run(
                [PY, "-u", str(HERE / "iq_capture.py"),
                 "--rf", str(rf), "--secs", str(secs),
                 "--antenna", port, "--rfgain", str(rfg),
                 "--ifgr", str(ifgr), "--out", str(out)],
                capture_output=True, text=True, timeout=secs + 120)
        except Exception as e:
            log_event({"event": "specimen-error", "err": str(e)[:100]})
            return False
        m = re.search(r'"continuity_pct":\s*([\d.]+)', r.stdout or "")
        cont = float(m.group(1)) if m else 0.0
        mb = round(out.stat().st_size / 1e6, 1) if out.exists() else 0.0
        # diagnostic: WHY a capture passes/fails (cont vs size), so misses
        # stop being a black box. Monitor doesn't notify on cap-try.
        log_event({"event": "cap-try", "rf": rf, "ant": port,
                   "attempt": attempt, "cont": cont, "mb": mb})
        # continuity is SAMPLE continuity (SDR streaming), independent of
        # signal quality — a real capture is 80-100%, a wedge is near 0.
        # 80 floor accepts marginal-but-present carriers, rejects duds.
        if mb > 10 and cont >= 80.0:
            return True
        time.sleep(5)                   # SDR release before the retry
    return False


def hhmm_passed(hhmm):
    now = time.localtime()
    h, m = map(int, hhmm.split(":"))
    # night wraps midnight: times before 12:00 belong to tomorrow-side
    tod = now.tm_hour * 60 + now.tm_min
    tgt = h * 60 + m
    if tgt < 720 and tod >= 720:      # target is after midnight, now isn't
        return False
    return tod >= tgt


# ── phase -1: forecast + oracle ────────────────────────────────────
def phase_forecast():
    for tool, secs in (("dawn_score2.py", 120), ("beacon_oracle.py", 180)):
        if stopped():
            return
        try:
            r = subprocess.run([PY, "-u", str(HERE / tool)],
                               capture_output=True, text=True, timeout=secs)
            tail = (r.stdout or "").strip().splitlines()[-2:]
            log_event({"event": "forecast", "tool": tool, "out": tail})
        except Exception as e:
            log_event({"event": "forecast-error", "tool": tool,
                       "err": str(e)[:100]})


# ── phase 0: scanner benchmark per antenna ─────────────────────────
def phase_scan_bench():
    scan_json = TOOLS / "data" / "scan.json"
    for port, ant, _ in GRID:
        if stopped():
            return
        env = dict(os.environ)
        env.update({"STVT_ANTENNA": port, "STVT_EQ_TELEM": "1",
                    "STVT_DABNOTCH": "0", "STVT_IQ_RING": "0",
                    "STVT_VITERBI": "soft", "STVT_EQ": "long",
                    "STVT_RS": "erasure", "STVT_RS_ERASURES": "0",
                    "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8",
                    "STVT_EQ_MOD12_GUARD": "1",
                    "STVT_EQ_TAP_CACHE": str(HERE / "lab" / "tapcache"),
                    "PATH": r"C:\Program Files\SDRplay\API\x64;"
                            + env.get("PATH", "")})
        t0 = time.time()
        try:
            p = subprocess.run([PY, "-u", str(TOOLS / "tv_tuner.py"),
                                "--scan"], input="1\n", env=env,
                               capture_output=True, text=True, timeout=900)
            dur = round(time.time() - t0, 1)
            dst = LAB / f"scan_{ant}.json"
            if scan_json.exists():
                dst.write_text(scan_json.read_text(encoding="utf-8"),
                               encoding="utf-8")
            floors = len(re.findall(r"MER floor", p.stdout or ""))
            locks = (p.stdout or "").count('"lock": true')
            log_event({"event": "scan-bench", "ant": ant, "secs": dur,
                       "early_rejects": floors,
                       "out_tail": (p.stdout or "").strip()
                       .splitlines()[-2:]})
        except Exception as e:
            log_event({"event": "scan-bench-error", "ant": ant,
                       "err": str(e)[:120]})
        time.sleep(5)


# ── phase 1: specimen-hunting cube ─────────────────────────────────
def phase_cube():
    specimens = []
    per_key = {}
    cycle = 0
    while not stopped() and not hhmm_passed(CUBE_END):
        cycle += 1
        for port, ant, rfs in GRID:
            for rf in rfs:
                if stopped() or hhmm_passed(CUBE_END):
                    break
                rfg, ifgr = GAINS.get(rf, DEFAULT_GAIN)
                try:
                    s = oc.sample(rf, port, rfg, ifgr, secs=45)
                except Exception as e:
                    log_event({"event": "sample-error", "rf": rf,
                               "ant": ant, "err": str(e)[:100]})
                    continue
                mer = s.get("mer_med")
                s.update({"event": None, "rf": rf, "ant": ant,
                          "cycle": cycle})
                s.pop("event", None)
                log_event(dict(s))
                # specimen worth catching: classic mid-cliff median OR a
                # BREATHER (healthy median, dips to the cliff / lossy) —
                # the RF9-evening disease lives in the low tail (H-DIP)
                key = f"{ant}_rf{rf}"
                p10 = s.get("mer_p10")
                badpct = (100 * s.get("rs_bad", 0) /
                          max(1, s.get("rs_pkts", 0)))
                breather = (mer is not None and mer >= 15.5 and
                            ((p10 is not None and p10 <= 15.2)
                             or badpct >= 1.5))
                midcliff = (mer is not None
                            and MID_CLIFF[0] <= mer < MID_CLIFF[1])
                if ((midcliff or breather)
                        and len(specimens) < MAX_SPECIMENS
                        and per_key.get(key, 0) < 2):
                    out = CAPS / (f"night_{key}_mer{mer:.1f}_"
                                  f"{time.strftime('%H%M')}.cs16")
                    if capture_specimen(rf, port, rfg, ifgr, out):
                        specimens.append(str(out))
                        per_key[key] = per_key.get(key, 0) + 1
                        log_event({"event": "SPECIMEN", "file": out.name,
                                   "mer": mer, "p10": p10,
                                   "bad_pct": round(badpct, 2)})
                    else:
                        log_event({"event": "specimen-miss", "rf": rf,
                                   "ant": ant, "mer": mer})
        time.sleep(120)          # slow campaign: breathe between cycles
    (LAB / "specimens.json").write_text(json.dumps(specimens, indent=1),
                                        encoding="utf-8")
    return specimens


# ── phase 2: E7 batch on tonight's specimens ───────────────────────
def phase_e7(specimens):
    for spec in specimens:
        if stopped():
            return
        rf_m = re.search(r"rf(\d+)", spec)
        env = dict(os.environ)
        env["STVT_DABNOTCH"] = ("0" if rf_m and int(rf_m.group(1)) < 14
                                else "1")
        try:
            r = subprocess.run([PY, "-u", str(HERE / "e7_vote.py"), spec,
                                "--passes", "3"], env=env,
                               capture_output=True, text=True, timeout=1500)
            tail = [ln for ln in (r.stdout or "").splitlines()
                    if "union:" in ln or "frames:" in ln or "healed" in ln]
            log_event({"event": "E7", "spec": Path(spec).name, "out": tail})
        except Exception as e:
            log_event({"event": "E7-error", "spec": Path(spec).name,
                       "err": str(e)[:100]})


def main():
    log_event({"event": "NIGHT-LAB-START",
               "grid": [(a, r) for _, a, r in GRID]})
    phase_forecast()
    phase_scan_bench()
    specimens = phase_cube() or []
    log_event({"event": "cube-done", "specimens": len(specimens)})
    phase_e7(specimens)
    # wait for ambush hour, then hand off to the proven dawn hunt
    while not stopped() and not hhmm_passed(AMBUSH_AT):
        time.sleep(60)
    if not stopped():
        log_event({"event": "AMBUSH-HANDOFF"})
        subprocess.run([PY, "-u", str(HERE / "ambush3.py")])
    # morning: hand the rig back — panel up, ready for the user
    try:
        subprocess.Popen([PY, "-u", str(HERE / "tv_tuna_panel.py")],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        log_event({"event": "PANEL-RESTORED"})
    except Exception as e:
        log_event({"event": "panel-restore-error", "err": str(e)[:80]})
    log_event({"event": "NIGHT-LAB-END"})


if __name__ == "__main__":
    main()
