"""config_shootout.py — the REFINEMENT stage of the universal tuner: at the
MER-calibrated gain, A/B the chain's recovery/tracking options and score each
with quality_judge (the objective fps/v_err oracle). For fluctuating multipath
signals — picks the config that turns "decodes" into "watchable/cable".

Each candidate: run chain SETTLE s, then judge a JUDGE_WIN s window.
Usage:
    python config_shootout.py --rf 31 [--ifgr 36] [--rfgain 2]
"""
import argparse, math, os, re, subprocess, time
from pathlib import Path

PY = r"C:\Users\user\radioconda\python.exe"
TV_LIVE = Path(r"Z:\src\magic-tv-decoder\tools\tv_live.py")
JUDGE = Path(r"Z:\src\adaptive-tv\quality_judge.py")
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
SDRPLAY_DLL = r"C:\Program Files\SDRplay\API\x64"
FFMPEG = r"C:\ffmpeg\bin"
LOG = Path(os.environ.get("TEMP", ".")) / "config_shootout.log"

SETTLE, JUDGE_WIN = 22, 15
TRAIN_RMS, CLIFF_DB = 5.0, 15.2
RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
RE_SCORE = re.compile(r"score=(\d+).*?fps=([\d.]+) v_err=([\d.]+)/s a_err=([\d.]+)")

# candidates: name -> extra env on top of the calibrated base
CANDIDATES = [
    ("baseline (RS=stock, LMS)", {}),
    ("erasure RS 7",             {"STVT_RS": "erasure", "STVT_RS_ERASURES": "7"}),
    ("erasure RS 20",            {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20"}),
    ("erasure + gear-LMS",       {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
                                  "STVT_EQ_GEAR_LMS": "1"}),
    ("erasure + RLS",            {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
                                  "STVT_EQ_RLS": "1"}),
    ("erasure + FS-avg x4",      {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
                                  "STVT_EQ_FS_AVG_DEPTH": "4"}),
    ("erasure + quality-reset",  {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
                                  "STVT_EQ_QUALITY_BAD_RMS": "8"}),
]


def base_env(args):
    e = os.environ.copy()
    e["PATH"] = SDRPLAY_DLL + os.pathsep + FFMPEG + os.pathsep + e.get("PATH", "")
    e.update({
        "STVT_ANTENNA": args.antenna, "STVT_IFGR": str(args.ifgr),
        "STVT_RFGAIN_SEL": str(args.rfgain), "STVT_EQ": "long",
        "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1", "STVT_DABNOTCH": "1",
        "STVT_RS": "stock", "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8",
        "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
        "STVT_EQ_TELEM": "1",
    })
    return e


def mer_of(text):
    errs = [float(m.group(1)) for m in RE_FS.finditer(text)]
    tail = errs[len(errs) // 3:] if len(errs) >= 3 else errs
    if not tail: return 0.0
    return sum(20.0 * math.log10(TRAIN_RMS / e) for e in tail if e > 0) / len(tail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=31)
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--ifgr", type=int, default=36)
    ap.add_argument("--rfgain", type=int, default=2)
    ap.add_argument("--program", type=int, default=1)
    ap.add_argument("--only", default="", help="comma substrings to keep")
    ap.add_argument("--reps", type=int, default=1, help="alternating repeats")
    args = ap.parse_args()
    cands = CANDIDATES
    if args.only:
        keys = [k.strip() for k in args.only.split(",")]
        cands = [c for c in CANDIDATES if any(k in c[0] for k in keys)]
    cands = cands * args.reps

    print(f"  config shootout RF{args.rf} rfgain={args.rfgain} IFGR={args.ifgr} "
          f"prog={args.program}  ({len(cands)} runs)\n")
    print(f"  {'config':<26}{'score':>6}{'fps':>7}{'v_err/s':>9}{'MER':>7}")
    results = []
    for name, extra in cands:
        env = base_env(args); env.update(extra)
        if LIVE.exists():
            try: LIVE.unlink()
            except OSError: pass
        with open(LOG, "w") as lf:
            ch = subprocess.Popen([PY, "-u", str(TV_LIVE), "--rf", str(args.rf)],
                                  env=env, stdout=lf, stderr=subprocess.STDOUT)
            time.sleep(SETTLE)
            try:
                out = subprocess.run(
                    [PY, str(JUDGE), "--program", str(args.program),
                     "--window", str(JUDGE_WIN)],
                    env=env, capture_output=True, text=True,
                    timeout=JUDGE_WIN + 30).stdout
            except Exception:
                out = ""
            ch.terminate()
            try: ch.wait(timeout=6)
            except Exception: ch.kill()
        m = RE_SCORE.search(out)
        score = int(m.group(1)) if m else 0
        fps = float(m.group(2)) if m else 0.0
        verr = float(m.group(3)) if m else 999.0
        mer = mer_of(LOG.read_text(errors="ignore"))
        print(f"  {name:<26}{score:>6}{fps:>7.1f}{verr:>9.1f}{mer:>7.2f}")
        results.append((score, -verr, name))
        time.sleep(2)
    # average repeated runs of the same config
    agg = {}
    for score, nverr, name in results:
        agg.setdefault(name, []).append((score, -nverr))
    print()
    ranked = sorted(agg.items(),
                    key=lambda kv: -sum(s for s, _ in kv[1]) / len(kv[1]))
    for name, runs in ranked:
        avg_s = sum(s for s, _ in runs) / len(runs)
        avg_v = sum(v for _, v in runs) / len(runs)
        print(f"  avg {name:<26} score={avg_s:5.1f} v_err={avg_v:6.1f}/s "
              f"(n={len(runs)})")
    print(f"\n  winner: {ranked[0][0]}")


if __name__ == "__main__":
    main()
