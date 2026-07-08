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
                # mid-cliff dwell -> specimen (the starving testbed class)
                key = f"{ant}_rf{rf}"
                if (mer is not None and MID_CLIFF[0] <= mer < MID_CLIFF[1]
                        and len(specimens) < MAX_SPECIMENS
                        and per_key.get(key, 0) < 2):
                    out = CAPS / (f"night_{key}_mer{mer:.1f}_"
                                  f"{time.strftime('%H%M')}.cs16")
                    try:
                        subprocess.run(
                            [PY, "-u", str(HERE / "iq_capture.py"),
                             "--rf", str(rf), "--secs", "45",
                             "--antenna", port, "--rfgain", str(rfg),
                             "--ifgr", str(ifgr), "--out", str(out)],
                            capture_output=True, timeout=200)
                        if out.exists() and out.stat().st_size > 10e6:
                            specimens.append(str(out))
                            per_key[key] = per_key.get(key, 0) + 1
                            log_event({"event": "SPECIMEN", "file": out.name,
                                       "mer": mer})
                    except Exception as e:
                        log_event({"event": "specimen-error",
                                   "err": str(e)[:100]})
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
    log_event({"event": "NIGHT-LAB-END"})


if __name__ == "__main__":
    main()
