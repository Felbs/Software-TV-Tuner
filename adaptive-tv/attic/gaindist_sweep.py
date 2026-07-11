"""gaindist_sweep.py — strong-but-glitchy signal: trade front-end (RF) gain for
IF gain to kill intermod from an overdriving amp. Tries (rfgain_sel, IFGR) combos
on RF31/long and reports lock%, fps, and v_err/s for program 1. Lowest v_err wins.
"""
import sys
import os, re, subprocess, time
from pathlib import Path

PY = sys.executable
TV_LIVE = Path(r"Z:\src\magic-tv-decoder\tools\tv_live.py")
JUDGE = Path(r"Z:\src\adaptive-tv\quality_judge.py")
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
SDRPLAY_DLL = r"C:\Program Files\SDRplay\API\x64"

RF, ANTENNA, PROGRAM = "31", "Antenna A", "1"
# (rfgain_sel, IFGR): higher rfgain_sel = LESS front-end gain = less intermod
COMBOS = [("3", "40"), ("4", "34"), ("4", "38"), ("5", "32"), ("5", "36"), ("6", "30")]
SETTLE, JUDGE_WIN = 16, 10
FPLL = re.compile(r"mean\|x\|=([\d.]+).*?in_rms=([\d.]+)")


def env(rfgain, ifgr):
    e = os.environ.copy()
    e["PATH"] = SDRPLAY_DLL + os.pathsep + e.get("PATH", "")
    e.update({
        "STVT_ANTENNA": ANTENNA, "STVT_IFGR": ifgr, "STVT_RFGAIN_SEL": rfgain,
        "STVT_EQ": "long", "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
        "STVT_DABNOTCH": "1", "STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
        "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1",
        "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
    })
    return e


def main():
    print(f"  gain-distribution sweep RF{RF} {ANTENNA} EQ=long prog{PROGRAM}\n")
    print(f"  {'rfgain':>7}{'IFGR':>6}{'in_rms':>8}{'lock%':>7}{'fps':>7}{'v_err/s':>9}")
    best = None
    for rfgain, ifgr in COMBOS:
        if LIVE.exists():
            try: LIVE.unlink()
            except Exception: pass
        log = Path(f"C:/Temp/stvt_lab/gd_{rfgain}_{ifgr}.log")
        with open(log, "w") as lf:
            ch = subprocess.Popen([PY, "-u", str(TV_LIVE), "--rf", RF],
                                  env=env(rfgain, ifgr), stdout=lf,
                                  stderr=subprocess.STDOUT)
        time.sleep(SETTLE)
        try:
            out = subprocess.run([PY, str(JUDGE), "--program", PROGRAM,
                                  "--window", str(JUDGE_WIN)],
                                 env=env(rfgain, ifgr), capture_output=True,
                                 text=True, timeout=JUDGE_WIN + 25).stdout
        except Exception:
            out = ""
        ch.terminate()
        try: ch.wait(timeout=6)
        except Exception: ch.kill()
        txt = log.read_text(errors="ignore")
        mxs = [float(m.group(1)) for m in FPLL.finditer(txt)]
        irs = [float(m.group(2)) for m in FPLL.finditer(txt)]
        lockpct = 100.0 * sum(1 for v in mxs if v > 0) / len(mxs) if mxs else 0.0
        in_rms = irs[-1] if irs else 0.0
        sc = re.search(r"score=(\d+).*?fps=([\d.]+).*?v_err=([\d.]+)", out)
        fps = float(sc.group(2)) if sc else 0.0
        verr = float(sc.group(3)) if sc else 999.0
        print(f"  {rfgain:>7}{ifgr:>6}{in_rms:>8.1f}{lockpct:>6.0f}%{fps:>7.1f}{verr:>9.1f}")
        # prefer real frames (fps>2) with lowest v_err
        key = (fps > 2, -verr) if fps > 2 else (False, -verr)
        if best is None or key > best[0]:
            best = (key, rfgain, ifgr, fps, verr)
        time.sleep(3)
    print()
    if best:
        print(f"  BEST: rfgain={best[1]} IFGR={best[2]}  fps={best[3]:.1f} v_err/s={best[4]:.1f}")


if __name__ == "__main__":
    main()
