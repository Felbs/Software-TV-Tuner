"""lock_test.py — for each candidate RF channel, run the chain briefly and report
whether the FPLL pilot actually LOCKS (mean|x|>0) and the input level. Finds a
channel worth a full decode attempt instead of guessing.
"""
import os, re, subprocess, time
from pathlib import Path

PY = r"C:\Users\user\radioconda\python.exe"
TV_LIVE = Path(r"Z:\src\magic-tv-decoder\tools\tv_live.py")
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
SDRPLAY_DLL = r"C:\Program Files\SDRplay\API\x64"

ANTENNA = "Antenna C"
IFGR, RFGAIN = "26", "2"
CHANNELS = [35, 19, 27, 34, 31, 23, 15]
WINDOW = 13

FPLL = re.compile(r"mean\|x\|=([\d.]+).*?in_rms=([\d.]+)")


def base_env():
    env = os.environ.copy()
    env["PATH"] = SDRPLAY_DLL + os.pathsep + env.get("PATH", "")
    env.update({
        "STVT_ANTENNA": ANTENNA, "STVT_IFGR": IFGR, "STVT_RFGAIN_SEL": RFGAIN,
        "STVT_EQ": "long", "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
        "STVT_DABNOTCH": "1", "STVT_RS": "stock", "STVT_SPS": "1.1",
        "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1",
        "STVT_EQ_LKG_RMS": "1.0",
    })
    return env


def count_headers():
    if not LIVE.exists():
        return 0
    with open(LIVE, "rb") as f:
        return f.read().count(b"\x00\x00\x01\xb3")


def main():
    print(f"  lock test on {ANTENNA} IFGR={IFGR} rfgain={RFGAIN}\n")
    print(f"  {'RF':>4}{'mean|x| peak':>14}{'in_rms':>9}{'hdrs':>7}{'  verdict'}")
    for rf in CHANNELS:
        if LIVE.exists():
            try: LIVE.unlink()
            except Exception: pass
        log = Path(f"C:/Users/user/AppData/Local/Temp/claude/lock_rf{rf}.log")
        with open(log, "w") as lf:
            ch = subprocess.Popen([PY, "-u", str(TV_LIVE), "--rf", str(rf)],
                                  env=base_env(), stdout=lf, stderr=subprocess.STDOUT)
        time.sleep(WINDOW)
        ch.terminate()
        try: ch.wait(timeout=6)
        except Exception: ch.kill()
        text = log.read_text(errors="ignore")
        mxs = [float(m.group(1)) for m in FPLL.finditer(text)]
        irs = [float(m.group(2)) for m in FPLL.finditer(text)]
        peak_mx = max(mxs) if mxs else 0.0
        in_rms = irs[-1] if irs else 0.0
        hdrs = count_headers()
        if hdrs >= 3:
            verdict = "*** DECODES ***"
        elif peak_mx > 0.01:
            verdict = "pilot locks (no data yet)"
        elif peak_mx > 0:
            verdict = "faint lock"
        else:
            verdict = "no lock"
        print(f"  {rf:>4}{peak_mx:>14.4f}{in_rms:>9.1f}{hdrs:>7}  {verdict}")
        time.sleep(3)


if __name__ == "__main__":
    main()
