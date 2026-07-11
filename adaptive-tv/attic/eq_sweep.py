"""eq_sweep.py — at a fixed gain, try each equalizer on a channel and score the
HD program. Finds the EQ that best handles this signal's multipath. Reports lock
stability (fraction of fpll samples with mean|x|>0) + decode score.
"""
import sys
import os, re, subprocess, time
from pathlib import Path

PY = sys.executable
TV_LIVE = Path(r"Z:\src\magic-tv-decoder\tools\tv_live.py")
JUDGE = Path(r"Z:\src\adaptive-tv\quality_judge.py")
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
SDRPLAY_DLL = r"C:\Program Files\SDRplay\API\x64"

RF, ANTENNA, IFGR, RFGAIN, PROGRAM = "31", "Antenna A", "40", "3", "1"
EQS = ["long", "cma", "multifs_dd", "pilot_dd_soft", "multifs"]
SETTLE, JUDGE_WIN = 16, 10

FPLL = re.compile(r"mean\|x\|=([\d.]+)")


def env(eq):
    e = os.environ.copy()
    e["PATH"] = SDRPLAY_DLL + os.pathsep + e.get("PATH", "")
    e.update({
        "STVT_ANTENNA": ANTENNA, "STVT_IFGR": IFGR, "STVT_RFGAIN_SEL": RFGAIN,
        "STVT_EQ": eq, "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
        "STVT_DABNOTCH": "1", "STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
        "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1",
        "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
    })
    return e


def main():
    print(f"  EQ sweep RF{RF} {ANTENNA} IFGR={IFGR} prog{PROGRAM}\n")
    print(f"  {'EQ':>14}{'lock%':>8}{'score':>7}{'fps':>7}{'v_err/s':>9}")
    best = None
    for eq in EQS:
        if LIVE.exists():
            try: LIVE.unlink()
            except Exception: pass
        log = Path(f"C:/Temp/stvt_lab/eq_{eq}.log")
        with open(log, "w") as lf:
            ch = subprocess.Popen([PY, "-u", str(TV_LIVE), "--rf", RF],
                                  env=env(eq), stdout=lf, stderr=subprocess.STDOUT)
        time.sleep(SETTLE)
        try:
            out = subprocess.run([PY, str(JUDGE), "--program", PROGRAM,
                                  "--window", str(JUDGE_WIN)],
                                 env=env(eq), capture_output=True, text=True,
                                 timeout=JUDGE_WIN + 25).stdout
        except Exception:
            out = ""
        ch.terminate()
        try: ch.wait(timeout=6)
        except Exception: ch.kill()
        mxs = [float(m.group(1)) for m in FPLL.finditer(log.read_text(errors="ignore"))]
        lockpct = 100.0 * sum(1 for v in mxs if v > 0) / len(mxs) if mxs else 0.0
        sc = re.search(r"score=(\d+).*?fps=([\d.]+).*?v_err=([\d.]+)", out)
        score = int(sc.group(1)) if sc else 0
        fps = float(sc.group(2)) if sc else 0.0
        verr = float(sc.group(3)) if sc else 0.0
        print(f"  {eq:>14}{lockpct:>7.0f}%{score:>7}{fps:>7.1f}{verr:>9.1f}")
        if best is None or (fps, -verr) > (best[2], -best[3]):
            best = (eq, score, fps, verr)
        time.sleep(3)
    print()
    if best:
        print(f"  BEST: EQ={best[0]}  score={best[1]} fps={best[2]:.1f} v_err/s={best[3]:.1f}")


if __name__ == "__main__":
    main()
