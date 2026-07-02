"""gain_sweep.py — does MORE amplification help, or are we SNR-limited?

Runs the chain at a ladder of IFGR values (IFGR is INVERTED: lower = MORE gain)
and counts real MPEG-2 sequence headers decoded in a fixed window at each. If the
header count climbs as gain increases, the system is gain-starved (another amp
stage / more LNA gain would help). If it plateaus or drops, it's SNR/capture
limited and amplification won't help.
"""
import os, subprocess, time, sys
from pathlib import Path

PY = r"C:\Users\user\radioconda\python.exe"
TV_LIVE = Path(r"Z:\src\magic-tv-decoder\tools\tv_live.py")
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
SDRPLAY_DLL = r"C:\Program Files\SDRplay\API\x64"

RF = 34
ANTENNA = "Antenna A"
IFGR_LADDER = ["42", "38", "34", "30", "26", "22"]   # left=less gain -> right=more gain
WINDOW = 16   # seconds of decode per rung


def count_headers(path, mb=8):
    if not path.exists():
        return 0
    with open(path, "rb") as f:
        f.seek(0, 2)
        sz = f.tell()
        f.seek(max(0, sz - mb * 1024 * 1024))
        data = f.read()
    return data.count(b"\x00\x00\x01\xb3")


def base_env(ifgr):
    env = os.environ.copy()
    env["PATH"] = SDRPLAY_DLL + os.pathsep + env.get("PATH", "")
    env.update({
        "STVT_ANTENNA": ANTENNA, "STVT_IFGR": ifgr, "STVT_RFGAIN_SEL": "3",
        "STVT_EQ": "long", "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
        "STVT_DABNOTCH": "1", "STVT_RS": "stock", "STVT_SPS": "1.1",
        "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1",
        "STVT_EQ_LKG_RMS": "1.0",
    })
    return env


def main():
    print(f"  gain sweep on RF{RF} / {ANTENNA}  (IFGR inverted: lower = MORE gain)\n")
    print(f"  {'IFGR':>5}{'gain':>8}{'seq_hdrs':>10}{'verdict':>16}")
    results = []
    for ifgr in IFGR_LADDER:
        if LIVE.exists():
            try: LIVE.unlink()
            except Exception: pass
        ch = subprocess.Popen([PY, "-u", str(TV_LIVE), "--rf", str(RF)],
                              env=base_env(ifgr),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(WINDOW)
        hdrs = count_headers(LIVE)
        ch.terminate()
        try: ch.wait(timeout=6)
        except Exception: ch.kill()
        time.sleep(3)   # let the SDR fully release before next rung
        gain_lbl = f"{59-int(ifgr)}dB"   # rough relative gain
        results.append((ifgr, hdrs))
        print(f"  {ifgr:>5}{gain_lbl:>8}{hdrs:>10}")

    print()
    best = max(results, key=lambda r: r[1])
    hdr_vals = [h for _, h in results]
    # is the trend rising with gain (later rungs > earlier) or flat/falling?
    low_gain_avg = sum(hdr_vals[:2]) / 2
    high_gain_avg = sum(hdr_vals[-2:]) / 2
    print("  " + "=" * 50)
    if best[1] == 0:
        print("  No rung decoded — signal too weak at every gain.")
    elif high_gain_avg > low_gain_avg * 1.25:
        print(f"  RISING with gain -> GAIN-STARVED. Best IFGR={best[0]} ({best[1]} hdrs).")
        print("  A higher-gain LNA / second amp stage would likely help.")
    elif high_gain_avg < low_gain_avg * 0.75:
        print(f"  FALLING at high gain -> OVERDRIVE. Best IFGR={best[0]} ({best[1]} hdrs).")
        print("  Already enough gain; MORE amplification hurts (clipping/intermod).")
    else:
        print(f"  FLAT across gain -> SNR/CAPTURE limited. Best IFGR={best[0]} ({best[1]} hdrs).")
        print("  An amplifier won't help; the antenna isn't capturing enough signal.")
    print("  " + "=" * 50)


if __name__ == "__main__":
    main()
