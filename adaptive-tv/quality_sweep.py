"""quality_sweep.py — autonomous chain-config troubleshooter.

Uses quality_judge.measure_once() as an OBJECTIVE judge to A/B chain configs
and find the one that decodes cleanest — no human "is it glitchy?" needed.
For each config it (re)starts the chain, waits for convergence, scores a
window, and reports a ranked table. Leaves the chain running on the winner.

Built to chase the "clean RF lock but glitchy picture" case: when in_rms runs
hot the signal clips on peaks and corrupts data the pilot-lock can't see, so
we sweep gain (and RS mode) and let the decoder-error score pick the winner.

Usage:
    python quality_sweep.py --rf 34 --antenna "Antenna A" --program 3
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from quality_judge import measure_once

PY = os.path.join(os.environ["USERPROFILE"], "radioconda", "python.exe")
TV_LIVE = r"Z:\src\magic-tv-decoder\tools\tv_live.py"
LIVE_TS = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
SDRPLAY = r"C:\Program Files\SDRplay\API\x64"

BASE = {"STVT_EQ": "long", "STVT_VITERBI": "soft", "STVT_SPS": "1.1",
        "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1",
        "STVT_EQ_LKG_RMS": "1.0", "STVT_RFNOTCH": "1"}

# label -> overrides. Sweep gain (fix hot/clipping) x RS mode (error recovery).
CONFIGS = [
    ("baseline_ifgr45_stock",  {"STVT_IFGR": "45", "STVT_RFGAIN_SEL": "3", "STVT_RS": "stock"}),
    ("cooler_ifgr52_stock",    {"STVT_IFGR": "52", "STVT_RFGAIN_SEL": "3", "STVT_RS": "stock"}),
    ("cooler_ifgr58_stock",    {"STVT_IFGR": "58", "STVT_RFGAIN_SEL": "2", "STVT_RS": "stock"}),
    ("ifgr52_erasure",         {"STVT_IFGR": "52", "STVT_RFGAIN_SEL": "3", "STVT_RS": "erasure", "STVT_RS_ERASURES": "20"}),
    ("ifgr45_erasure",         {"STVT_IFGR": "45", "STVT_RFGAIN_SEL": "3", "STVT_RS": "erasure", "STVT_RS_ERASURES": "20"}),
]


def start_chain(rf, antenna, overrides):
    if LIVE_TS.exists():
        try: LIVE_TS.unlink()
        except Exception: pass
    env = os.environ.copy()
    env["PATH"] = SDRPLAY + os.pathsep + env.get("PATH", "")
    env.update(BASE)
    env["STVT_ANTENNA"] = antenna
    env.update(overrides)
    return subprocess.Popen([PY, "-u", TV_LIVE, "--rf", str(rf)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=34)
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--program", type=int, default=3)
    ap.add_argument("--window", type=int, default=12)
    ap.add_argument("--converge", type=int, default=25)
    args = ap.parse_args()

    results = []
    for label, ov in CONFIGS:
        print(f"\n=== {label}: {ov} ===", flush=True)
        proc = start_chain(args.rf, args.antenna, ov)
        # wait for convergence (live.ts past 12MB + real decode)
        t0 = time.time(); ready = False
        while time.time() - t0 < args.converge + 25:
            time.sleep(3)
            if LIVE_TS.exists() and LIVE_TS.stat().st_size > 18 * 1024 * 1024:
                ready = True
                if time.time() - t0 >= args.converge:
                    break
        score = measure_once(args.program, args.window) if ready else 0
        results.append((label, ov, score))
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
        time.sleep(2)

    results.sort(key=lambda r: -r[2])
    print("\n" + "=" * 60)
    print("  SWEEP RESULTS (objective decoder-error score, higher=better)")
    print("=" * 60)
    for label, ov, score in results:
        tier = ("cable" if score >= 90 else "watchable" if score >= 60
                else "glitchy" if score >= 30 else "broken")
        print(f"  {score:>3}  {tier:<10} {label}")
    win_label, win_ov, win_score = results[0]
    print(f"\n  WINNER: {win_label} (score {win_score}) -> {win_ov}")
    # leave the chain running on the winner
    print("  restarting chain on winner (left running)...")
    start_chain(args.rf, args.antenna, win_ov)
    print(f"  done. winner env: {dict(**BASE, **win_ov, STVT_ANTENNA=args.antenna)}")


if __name__ == "__main__":
    main()
