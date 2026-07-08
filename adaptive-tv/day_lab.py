"""day_lab.py — the workday campaign (runs ~07:30-17:00 unattended).

Daylight is the honest window (law) — this is the fair benchmark the
evening cubes can't give. Each ~55 min cycle:
  1. GLASS SOAK: RF36/Philips 90 s — tracks the strong-signal floor
     through the day (thermal/time stability of "flawless").
  2. TRI-ANTENNA CENSUS: each antenna's best channels, 45 s MER
     samples — densifies the time-knob map with DAYTIME hours.
  3. SPECIMEN HUNT: any mid-cliff dwell (12.5-16.2 dB) -> 45 s RAM IQ
     capture (max 6/day, 2/channel) for the replay lab.
  4. SCAN BENCH (one antenna per cycle, rotating): validates the
     three-verdict scanner speed/accuracy in daylight.
At 17:00: E7 vote batch on the day's specimens, then the panel comes
back up for the evening. STOP file: lab/day_lab/STOP.
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
LAB = HERE / "lab" / "day_lab"
LAB.mkdir(parents=True, exist_ok=True)
STOP = LAB / "STOP"
LOG = HERE / "day_lab.jsonl"
CAPS = HERE / "lab" / "captures"
TOOLS = Path(r"Z:\src\magic-tv-decoder\tools")

GAINS = {36: (3, 40), 34: (2, 32), 15: (1, 32), 7: (5, 32), 9: (5, 32),
         21: (2, 32)}
DEFAULT_GAIN = (3, 40)
CENSUS = [("Antenna B", "philips", [36, 9, 15]),
          ("Antenna A", "rabbit", [21, 34]),
          ("Antenna C", "discone", [7])]
SCAN_ROTATION = [("Antenna B", "philips"), ("Antenna A", "rabbit"),
                 ("Antenna C", "discone")]
MID_CLIFF = (12.5, 16.2)
MAX_SPECIMENS = 6
END_AT = os.environ.get("DAY_LAB_END", "18:00")


def log_event(o):
    o["t"] = time.strftime("%H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(o) + "\n")
    print(f"[day_lab {o['t']}] {o}", flush=True)


def stopped():
    return STOP.exists()


def past_end():
    now = time.localtime()
    h, m = map(int, END_AT.split(":"))
    return now.tm_hour * 60 + now.tm_min >= h * 60 + m


def scan_env(port):
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
    return env


def main():
    log_event({"event": "DAY-LAB-START"})
    specimens, per_key = [], {}
    cycle = 0
    while not stopped() and not past_end():
        cycle += 1
        # 1. glass soak
        try:
            s = oc.sample(36, "Antenna B", 3, 40, secs=90)
            s.update({"event": "GLASS", "cycle": cycle})
            log_event(s)
        except Exception as e:
            log_event({"event": "glass-error", "err": str(e)[:100]})
        # 2 + 3. census + specimen hunt
        for port, ant, rfs in CENSUS:
            for rf in rfs:
                if stopped() or past_end():
                    break
                rfg, ifgr = GAINS.get(rf, DEFAULT_GAIN)
                try:
                    s = oc.sample(rf, port, rfg, ifgr, secs=45)
                except Exception as e:
                    log_event({"event": "sample-error", "rf": rf,
                               "ant": ant, "err": str(e)[:100]})
                    continue
                s.update({"rf": rf, "ant": ant, "cycle": cycle})
                log_event(dict(s))
                mer = s.get("mer_med")
                key = f"{ant}_rf{rf}"
                # H-DIP (2026-07-08, from the night's zero-specimen
                # result): the mid-cliff MEDIAN band was empty all
                # night — marginality lives in the DIPS. A breather
                # (healthy median, cliff-diving low tail or high bad%)
                # is specimen-worthy: that's the RF9-evening disease.
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
                    out = CAPS / (f"day_{key}_mer{mer:.1f}_"
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
                            log_event({"event": "SPECIMEN",
                                       "file": out.name, "mer": mer})
                    except Exception as e:
                        log_event({"event": "specimen-error",
                                   "err": str(e)[:100]})
        # 4. rotating scan bench
        port, ant = SCAN_ROTATION[(cycle - 1) % len(SCAN_ROTATION)]
        t0 = time.time()
        try:
            p = subprocess.run([PY, "-u", str(TOOLS / "tv_tuner.py"),
                                "--scan"], input="1\n", env=scan_env(port),
                               capture_output=True, text=True, timeout=900)
            locks = len(re.findall(r"lock_attempt|\"lock\": true",
                                   p.stdout or ""))
            log_event({"event": "scan-bench", "ant": ant,
                       "secs": round(time.time() - t0, 1),
                       "tail": (p.stdout or "").strip().splitlines()[-2:]})
        except Exception as e:
            log_event({"event": "scan-bench-error", "ant": ant,
                       "err": str(e)[:100]})
        time.sleep(180)          # breathe between cycles
    # E7 batch on the day's specimens
    for spec in specimens:
        if stopped():
            break
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
            log_event({"event": "E7-error", "err": str(e)[:100]})
    # evening: hand the rig back
    try:
        subprocess.Popen([PY, "-u", str(HERE / "tv_tuna_panel.py")],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        log_event({"event": "PANEL-RESTORED"})
    except Exception as e:
        log_event({"event": "panel-restore-error", "err": str(e)[:80]})
    log_event({"event": "DAY-LAB-END", "specimens": len(specimens)})


if __name__ == "__main__":
    main()
