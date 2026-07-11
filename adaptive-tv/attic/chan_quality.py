"""chan_quality.py — decode-quality (fps + v_err) of the strong channels at the
working gain, to pick the one with the cleanest multipath path for viewing.
"""
import sys
import os, re, subprocess, time
from pathlib import Path

PY = sys.executable
TV_LIVE = Path(r"Z:\src\magic-tv-decoder\tools\tv_live.py")
JUDGE = Path(r"Z:\src\adaptive-tv\quality_judge.py")
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
SDRPLAY_DLL = r"C:\Program Files\SDRplay\API\x64"

ANTENNA, IFGR, RFGAIN = "Antenna A", "40", "3"
CHANNELS = ["31", "15", "36", "17", "21"]
SETTLE, JUDGE_WIN = 18, 12


def env():
    e = os.environ.copy()
    e["PATH"] = SDRPLAY_DLL + os.pathsep + e.get("PATH", "")
    e.update({
        "STVT_ANTENNA": ANTENNA, "STVT_IFGR": IFGR, "STVT_RFGAIN_SEL": RFGAIN,
        "STVT_EQ": "long", "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
        "STVT_DABNOTCH": "1", "STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
        "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1",
        "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
    })
    return e


def headers():
    if not LIVE.exists(): return 0
    with open(LIVE, "rb") as f: return f.read().count(b"\x00\x00\x01\xb3")


def main():
    print(f"  channel quality {ANTENNA} IFGR={IFGR} rfgain={RFGAIN} EQ=long\n")
    print(f"  {'RF':>4}{'hdrs':>7}{'fps':>7}{'v_err/s':>9}{'a_err/s':>9}")
    rows = []
    for rf in CHANNELS:
        if LIVE.exists():
            try: LIVE.unlink()
            except Exception: pass
        ch = subprocess.Popen([PY, "-u", str(TV_LIVE), "--rf", rf], env=env(),
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        time.sleep(SETTLE)
        h = headers()
        try:
            out = subprocess.run([PY, str(JUDGE), "--program", "1",
                                  "--window", str(JUDGE_WIN)], env=env(),
                                 capture_output=True, text=True,
                                 timeout=JUDGE_WIN + 25).stdout
        except Exception:
            out = ""
        ch.terminate()
        try: ch.wait(timeout=6)
        except Exception: ch.kill()
        sc = re.search(r"fps=([\d.]+).*?v_err=([\d.]+)/s a_err=([\d.]+)", out)
        fps = float(sc.group(1)) if sc else 0.0
        verr = float(sc.group(2)) if sc else 999.0
        aerr = float(sc.group(3)) if sc else 999.0
        print(f"  {rf:>4}{h:>7}{fps:>7.1f}{verr:>9.1f}{aerr:>9.1f}")
        rows.append((rf, fps, verr))
        time.sleep(3)
    rows.sort(key=lambda r: (-(r[1] > 2), r[2]))
    print(f"\n  cleanest: RF{rows[0][0]}  fps={rows[0][1]:.1f} v_err/s={rows[0][2]:.1f}")


if __name__ == "__main__":
    main()
