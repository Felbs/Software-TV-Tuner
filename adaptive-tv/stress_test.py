"""stress_test.py — extended playback stress test + telemetry correlator.

Runs the chain for several minutes and, every cycle, measures decode QUALITY
(quality_judge score) AND samples the chain's live RF telemetry, then correlates
them. Answers: is the glitching STEADY (constant marginal SNR), INTERMITTENT
(fades / dropouts), or BURSTY (USB overflow / clipping events)? And does it line
up with mean|x| spikes, in_rms swings, peak-clipping, or sample overflows?

Output: a per-cycle table + a summary with score stability, worst dips, clip/
overflow counts, and a plain-language diagnosis.

Usage:
    python stress_test.py --rf 34 --antenna "Antenna A" --program 3 --minutes 4
"""
import argparse
import json
import os
import re
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
QJ_STATE = Path(os.environ["TEMP"]) / "quality_state.json"
CHAIN_LOG = Path(os.environ["TEMP"]) / "stress_chain.log"

BASE = {"STVT_EQ": "long", "STVT_VITERBI": "soft", "STVT_SPS": "1.1",
        "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1",
        "STVT_EQ_LKG_RMS": "1.0", "STVT_RFNOTCH": "1",
        "STVT_IFGR": "45", "STVT_RFGAIN_SEL": "3",
        "STVT_RS": "erasure", "STVT_RS_ERASURES": "20"}

FPLL_RE = re.compile(r"mean\|x\|=([\d.]+).*?max\|x\|=([\d.]+)\s+in_rms=([\d.]+)")
OVF_RE = re.compile(r"overflow|OsO|dropped|\bsObO\b|timed out", re.I)


def start_chain(rf, antenna):
    if LIVE_TS.exists():
        try: LIVE_TS.unlink()
        except Exception: pass
    env = os.environ.copy()
    env["PATH"] = SDRPLAY + os.pathsep + env.get("PATH", "")
    env.update(BASE); env["STVT_ANTENNA"] = antenna
    logf = open(CHAIN_LOG, "w")
    return subprocess.Popen([PY, "-u", TV_LIVE, "--rf", str(rf)], env=env,
                            stdout=subprocess.DEVNULL, stderr=logf), logf


def parse_telemetry(text):
    """From a slice of chain stderr, return (mean_avg, maxx_peak, in_rms_avg,
    clip_count, overflow_count). clip = max|x| >= 1.0 (peak clipping)."""
    means, maxxs, inrms = [], [], []
    clips = 0
    for m in FPLL_RE.finditer(text):
        mn, mx, ir = float(m.group(1)), float(m.group(2)), float(m.group(3))
        means.append(mn); maxxs.append(mx); inrms.append(ir)
        if mx >= 1.0:
            clips += 1
    ovf = len(OVF_RE.findall(text))
    avg = lambda L: (sum(L) / len(L)) if L else 0.0
    return avg(means), (max(maxxs) if maxxs else 0.0), avg(inrms), clips, ovf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=34)
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--program", type=int, default=3)
    ap.add_argument("--minutes", type=float, default=4.0)
    ap.add_argument("--window", type=int, default=12)
    args = ap.parse_args()

    print(f"[stress] starting chain RF{args.rf}/{args.antenna} (erasure-RS winner config)...")
    proc, logf = start_chain(args.rf, args.antenna)
    # converge
    t0 = time.time()
    while time.time() - t0 < 50:
        time.sleep(3)
        if LIVE_TS.exists() and LIVE_TS.stat().st_size > 18 * 1024 * 1024:
            break
    print(f"[stress] running for {args.minutes:.0f} min, sampling every ~{args.window+6}s\n")
    print(f"  {'t':>5} {'score':>5} {'tier':<10} {'fps':>5} {'v/s':>6} {'a/s':>5} "
          f"{'mean|x|':>7} {'maxpk':>6} {'in_rms':>6} {'clip':>4} {'ovf':>4}")

    rows = []
    log_pos = 0
    end = time.time() + args.minutes * 60
    while time.time() < end:
        score = measure_once(args.program, args.window)
        st = json.loads(QJ_STATE.read_text()) if QJ_STATE.exists() else {}
        # read new chain-log text since last position
        text = ""
        try:
            with open(CHAIN_LOG, "r", errors="replace") as f:
                f.seek(log_pos); text = f.read(); log_pos = f.tell()
        except Exception:
            pass
        mn, mxpk, ir, clips, ovf = parse_telemetry(text)
        t = int(time.time() - t0)
        row = dict(t=t, score=score, tier=st.get("tier", "?"),
                   fps=st.get("fps", 0), v=st.get("video_errors_per_sec", 0),
                   a=st.get("audio_errors_per_sec", 0),
                   mean=mn, maxpk=mxpk, in_rms=ir, clip=clips, ovf=ovf)
        rows.append(row)
        print(f"  {t:>5} {score:>5} {row['tier']:<10} {row['fps']:>5} {row['v']:>6} "
              f"{row['a']:>5} {mn:>7.3f} {mxpk:>6.2f} {ir:>6.0f} {clips:>4} {ovf:>4}", flush=True)
        time.sleep(4)

    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()
    logf.close()

    # summary + diagnosis
    scores = [r["score"] for r in rows] or [0]
    vs = [r["v"] for r in rows] or [0]
    total_clip = sum(r["clip"] for r in rows)
    total_ovf = sum(r["ovf"] for r in rows)
    lo, hi, avg = min(scores), max(scores), sum(scores) / len(scores)
    swing = hi - lo
    print("\n" + "=" * 60)
    print("  STRESS TEST SUMMARY")
    print("=" * 60)
    print(f"  cycles            {len(rows)}")
    print(f"  score  avg/min/max {avg:.0f} / {lo} / {hi}   (swing {swing})")
    print(f"  video err/s  avg   {sum(vs)/len(vs):.1f}  (max {max(vs)})")
    print(f"  peak-clip events   {total_clip}")
    print(f"  overflow markers   {total_ovf}")
    # diagnosis
    print("-" * 60)
    if avg >= 90:
        diag = "HEALTHY — cable quality, no action needed."
    elif total_ovf > 3:
        diag = ("BURSTY OVERFLOWS — USB can't keep up (long/marginal cable or "
                "CPU). Use a short, direct USB cable / powered hub.")
    elif total_clip > 3:
        diag = ("PEAK CLIPPING — signal too hot on transients; raise IFGR "
                "(less gain) a few steps.")
    elif swing >= 25:
        diag = ("INTERMITTENT FADING — score swings widely while RF lock holds. "
                "Marginal antenna/feedline: signal fades in and out. Likely the "
                "long USB or routed coax degrading SNR vs the morning setup; "
                "fix the physical path (short USB, direct coax, antenna height).")
    else:
        diag = ("STEADY-MARGINAL — consistently below cable quality. SNR is a few "
                "dB short everywhere. Physical signal/feedline limit, not config.")
    print(f"  DIAGNOSIS: {diag}")
    print("=" * 60)


if __name__ == "__main__":
    main()
