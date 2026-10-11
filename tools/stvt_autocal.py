#!/usr/bin/env python3
"""stvt_autocal — per-antenna auto-calibration ("deliver on any antenna").

Everything you tune by hand for a NEW antenna — gain, then equalizer — this
finds automatically by MEASURING decode on that antenna's own live signal,
then writes the winners to an env file the run-scripts source.

It is the orchestrator that chains the pieces already in the tree into one
startup calibration pass, using the fast DETERMINISTIC capture->replay method
(record_iq + tv_replay) rather than live-spawning. (Distinct from the older
stvt_autotune.py, which tunes only the DSP config over SoapyRemote at a FIXED
gain — gain is exactly the per-antenna lever that one can't find.)

Pipeline:
  1. GAIN sweep   — short IQ capture at several IFGR values, score decode%;
                    keep the gain that decodes best. Too-low IFGR clips and
                    too-high starves, so the best-decode point self-finds the
                    sweet spot (no separate clip test needed). THIS is the
                    lever that changes most antenna-to-antenna.
  2. EQ selection — one longer capture at the winning gain, replay through the
                    real-time-capable equalizer configs, keep the best.
  3. WRITE        — ~/.stvt_autocal.env with STVT_IFGR + the equalizer knobs
                    + a human-readable report.

SDR is single-access: run when nothing else holds the SDR (before watching).
  python3 tools/stvt_autocal.py --rf 9 [--antenna "Antenna A"]
  source ~/.stvt_autocal.env && tools/stvt_run.sh 9 <prog>
"""
from __future__ import annotations
import argparse, os, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ENV_OUT = Path.home() / ".stvt_autocal.env"
TMP = Path("/tmp/stvt_autocal")
TMP.mkdir(exist_ok=True)

# Production base every equalizer config inherits.
BASE_ENV = dict(
    STVT_RS="stock", STVT_VITERBI="hard", STVT_EQ="long",
    STVT_SPS="1.1", STVT_RRC_SYMS="4",
    STVT_FPLL_FOLD="1", STVT_FPLL_BLOCK_NCO="1",
)

# Real-time-capable equalizer candidates. The full RLS / full DFE are excluded
# by default (proven not to hold real-time on a CPU-bound box); --eq-budget
# loose lets a faster machine consider heavier ones.
EQ_CANDIDATES = [
    ("baseline",  {}),
    ("erasure",   {"STVT_RS": "erasure"}),
    ("beta_2e-5", {"STVT_EQ_BETA": "2e-5"}),
    ("dfe_trim",  {"STVT_EQ_DFE": "1", "STVT_EQ_DFE_NFB": "32",
                   "STVT_EQ_DFE_STRIDE": "4", "STVT_EQ_DFE_FF": "0"}),
]
EQ_HEAVY = [
    ("dfe_full",  {"STVT_EQ_DFE": "1", "STVT_EQ_DFE_NFB": "64",
                   "STVT_EQ_DFE_STRIDE": "2", "STVT_EQ_DFE_FF": "1"}),
]

GAIN_CANDIDATES = [20, 30, 40, 50]   # RSPdx IFGR floor ~20


def log(msg: str):
    print(f"[autocal] {msg}", flush=True)


# SDRplay needs a moment to release between a close and the next open, or the
# re-open fails "no available RSP devices" — the open/close churn we hit all
# session. Cool down after every capture and retry once.
SDR_COOLDOWN = 4.0


def capture(rf, secs, ifgr, antenna, out: Path) -> bool:
    errlog = TMP / "capture.err"
    for attempt in (1, 2):
        out.unlink(missing_ok=True)
        with open(errlog, "wb") as ef:
            subprocess.run(
                [sys.executable, str(HERE / "record_iq.py"), "--rf", str(rf),
                 "--seconds", str(int(round(secs))), "--ifgr", str(ifgr),
                 "--rfgain-sel", "5", "--antenna", antenna, "--format", "cs16",
                 "--out", str(out)],
                cwd=REPO, stdout=subprocess.DEVNULL, stderr=ef)
        ok = out.exists() and out.stat().st_size > 1_000_000
        time.sleep(SDR_COOLDOWN)              # let the SDR release before next open
        if ok:
            return True
        if attempt == 1:
            log(f"    (capture retry after cooldown; last err: "
                f"{errlog.read_text(errors='ignore').strip().splitlines()[-1:]})")
    return False


def good_pct(ts: Path) -> float:
    """Fraction of valid (non-null) TS packets — the decode-quality metric."""
    try:
        data = ts.read_bytes()
    except FileNotFoundError:
        return 0.0
    good = tot = 0
    for i in range(len(data) // 188):
        pkt = data[i * 188:i * 188 + 188]
        if pkt[0] != 0x47:
            tot += 1; continue
        if (((pkt[1] & 0x1f) << 8) | pkt[2]) != 0x1fff:
            good += 1
        tot += 1
    return 100.0 * good / tot if tot else 0.0


def score(iq: Path, extra: dict):
    out = TMP / "score.ts"
    out.unlink(missing_ok=True)
    env = dict(os.environ); env.update(BASE_ENV); env.update(extra)
    t0 = time.time()
    subprocess.run([sys.executable, str(HERE / "tv_replay.py"), "--iq", str(iq),
                    "--out", str(out), "--log", str(TMP / "score.log")],
                   cwd=REPO, env=env, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    wall = time.time() - t0
    pct = good_pct(out)
    out.unlink(missing_ok=True)
    return pct, wall


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rf", type=int, required=True)
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--gain-secs", type=float, default=4.0)
    ap.add_argument("--eq-secs", type=float, default=15.0)
    ap.add_argument("--gains", default=",".join(map(str, GAIN_CANDIDATES)))
    ap.add_argument("--eq-budget", choices=["realtime", "loose"],
                    default="realtime",
                    help="realtime=only configs that hold real-time (default); "
                         "loose=also weigh heavier DFE/RLS (for fast CPUs)")
    args = ap.parse_args()

    gains = [int(x) for x in args.gains.split(",") if x.strip()]
    log(f"calibrating RF{args.rf} on '{args.antenna}' — gains {gains}")

    # ---- Step 1: gain sweep ------------------------------------------------
    log("step 1/3: gain sweep (the big per-antenna lever)")
    iq_g = TMP / "gain.cs16"
    best_gain, best_gain_pct = gains[len(gains) // 2], -1.0
    for g in gains:
        if not capture(args.rf, args.gain_secs, g, args.antenna, iq_g):
            log(f"  IFGR={g:>2}: capture failed (no signal / SDR busy)"); continue
        pct, _ = score(iq_g, {})
        mark = ""
        if pct > best_gain_pct:
            best_gain_pct, best_gain = pct, g; mark = "  <- best"
        log(f"  IFGR={g:>2}: decode {pct:5.1f}%{mark}")
    iq_g.unlink(missing_ok=True)
    if best_gain_pct < 0:
        log("no gain produced a capture — SDR busy or antenna dead. Aborting.")
        return 1
    log(f"  => gain IFGR={best_gain} ({best_gain_pct:.1f}%)")

    # ---- Step 2: equalizer selection ---------------------------------------
    log("step 2/3: equalizer selection (at best gain)")
    iq_e = TMP / "eq.cs16"
    best_eq_name, best_eq_extra = "baseline", {}
    if not capture(args.rf, args.eq_secs, best_gain, args.antenna, iq_e):
        log("  capture failed — keeping baseline equalizer")
    else:
        cands = EQ_CANDIDATES + (EQ_HEAVY if args.eq_budget == "loose" else [])
        best_eq_pct = -1.0
        for name, extra in cands:
            pct, wall = score(iq_e, extra)
            realtime = wall < 1.30 * args.eq_secs
            mark = ""
            if (realtime or args.eq_budget == "loose") and pct > best_eq_pct:
                best_eq_pct, best_eq_name, best_eq_extra = pct, name, extra
                mark = "  <- best"
            elif not realtime:
                mark = "  (too slow)"
            log(f"  {name:<10} {pct:5.1f}%  {wall:4.1f}s{mark}")
        log(f"  => equalizer '{best_eq_name}' ({best_eq_pct:.1f}%)")
    iq_e.unlink(missing_ok=True)

    # ---- Step 3: write the tuned config ------------------------------------
    log("step 3/3: writing config")
    chosen = {"STVT_IFGR": str(best_gain), "STVT_RFGAIN_SEL": "5",
              "STVT_ANTENNA": args.antenna}
    chosen.update(best_eq_extra)
    out_lines = [f"# stvt_autocal — RF{args.rf}, '{args.antenna}'  "
                 f"({time.strftime('%Y-%m-%d %H:%M')})",
                 f"# gain IFGR={best_gain} ({best_gain_pct:.1f}%), "
                 f"equalizer '{best_eq_name}'"]
    for k, v in chosen.items():
        out_lines.append(f'export {k}="{v}"' if " " in str(v) else f"export {k}={v}")
    ENV_OUT.write_text("\n".join(out_lines) + "\n")
    print("\n=== AUTOCAL RESULT ===")
    print(ENV_OUT.read_text())
    print(f"Use it:  source {ENV_OUT} && tools/stvt_run.sh {args.rf} <prog>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
