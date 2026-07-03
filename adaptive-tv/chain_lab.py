"""chain_lab.py — offline Bayesian optimization of the decode chain.

The problem with live A/B: two configs, one noisy non-stationary signal,
30-60 s per measurement. The fix: replay a FROZEN IQ capture through the
chain (tv_replay.py) so every config sees the exact same samples — the
measurement becomes deterministic — then let Optuna's Bayesian search
(TPE) explore the full multi-knob space instead of one axis at a time.

Workflow:
    # 1. sanity: is replay deterministic on this machine?
    python chain_lab.py verify   --iq tools\\data\\ab_test\\ab_rf34_30s.cf32
    # 2. score one config by hand
    python chain_lab.py run      --iq <capture> --set STVT_RS=erasure
    # 3. the main event (resumable; rerun to add trials to the same study)
    python chain_lab.py optimize --iq <capture> --trials 10 --study rf34
    # 4. what did it learn?
    python chain_lab.py report   --study rf34

Scoring: MPEG-2 seq-headers in the output TS (decode volume) minus a
penalty for ffmpeg null-decode error lines (decode cleanliness), plus MER
from the equalizer telemetry as a diagnostic. Replay runs at IDLE process
priority so a live TV chain keeps its cycles.
"""
import argparse, hashlib, json, math, os, re, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = HERE / "lab"
PY = r"C:\Users\user\radioconda\python.exe"
REPLAY = Path(r"Z:\src\magic-tv-decoder\tools\tv_replay.py")
SDRPLAY_DLL = r"C:\Program Files\SDRplay\API\x64"
FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
IDLE_PRIORITY = 0x00000040          # subprocess creationflags: don't fight live TV

RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
TRAIN_RMS, CLIFF_DB = 5.0, 15.2

# Baseline = the production chain defaults (mirrors tv_live/tune_antenna).
BASE_ENV = {
    "STVT_EQ": "long", "STVT_VITERBI": "soft", "STVT_RS": "stock",
    "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1",
    "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
    "STVT_FPLL_FOLD": "1", "STVT_EQ_TELEM": "1",
}


def replay(iq: Path, overrides: dict, tag: str = "run") -> dict:
    """Run the chain once on the capture with BASE_ENV+overrides. Returns
    metrics: headers, err_lines, mer_db, ts_bytes, score, elapsed."""
    LAB.mkdir(exist_ok=True)
    out = LAB / f"{tag}_{os.getpid()}.ts"
    log = LAB / f"{tag}_{os.getpid()}.log"
    env = os.environ.copy()
    env["PATH"] = SDRPLAY_DLL + os.pathsep + env.get("PATH", "")
    env.update(BASE_ENV)
    env.update({k: str(v) for k, v in overrides.items()})
    t0 = time.time()
    # generous timeout: replay of a 30 s capture is ~real-time-ish at idle prio
    cap_secs = iq.stat().st_size / 8 / 8_000_000
    subprocess.run([PY, "-u", str(REPLAY), "--iq", str(iq),
                    "--out", str(out), "--log", str(log)],
                   env=env, timeout=cap_secs * 8 + 120,
                   creationflags=IDLE_PRIORITY)
    elapsed = time.time() - t0

    data = out.read_bytes() if out.exists() else b""
    headers = data.count(b"\x00\x00\x01\xb3")
    ts_bytes = len(data)

    # cleanliness: ffmpeg null-decode of every video stream, count error lines
    err_lines = 0
    if ts_bytes > 1_000_000:
        try:
            p = subprocess.run([FFMPEG, "-v", "error", "-i", str(out),
                                "-map", "0:v?", "-f", "null", "-"],
                               capture_output=True, text=True,
                               timeout=cap_secs * 6 + 60,
                               creationflags=IDLE_PRIORITY)
            err_lines = sum(1 for ln in p.stderr.splitlines() if ln.strip())
        except Exception:
            err_lines = 10_000
    # MER from telemetry (skip first third: convergence)
    mer_db = 0.0
    if log.exists():
        errs = [float(m.group(1)) for m in
                RE_FS.finditer(log.read_text(errors="ignore"))]
        tail = errs[len(errs) // 3:]
        vals = [20.0 * math.log10(TRAIN_RMS / e) for e in tail if e > 0]
        mer_db = sum(vals) / len(vals) if vals else 0.0

    sha = hashlib.sha256(data).hexdigest()[:16] if data else "-"
    for f in (out, log):
        try: f.unlink()
        except OSError: pass
    # score: decode volume, penalized by decode errors. err weight chosen so
    # ~20 error lines costs one header — cleanliness breaks header ties
    # without ever beating "more real video decoded".
    score = headers - 0.05 * err_lines
    return {"headers": headers, "err_lines": err_lines,
            "mer_db": round(mer_db, 2), "ts_mb": round(ts_bytes / 1e6, 1),
            "score": round(score, 2), "elapsed_s": round(elapsed, 1),
            "sha16": sha}


# ── the search space ───────────────────────────────────────────────
def suggest(trial) -> dict:
    """Chain knobs Optuna may adjust. Defaults sit inside every range so the
    baseline is reachable. Conditional params only appear when their master
    switch is on (TPE handles this natively)."""
    o = {}
    o["STVT_RS"] = trial.suggest_categorical("rs", ["stock", "erasure"])
    if o["STVT_RS"] == "erasure":
        o["STVT_RS_ERASURES"] = trial.suggest_int("rs_erasures", 6, 20)
    o["STVT_EQ_BETA"] = trial.suggest_float("eq_beta", 1e-5, 4e-4, log=True)
    o["STVT_EQ_LEAK"] = trial.suggest_float("eq_leak", 1e-4, 2e-3, log=True)
    lkg = trial.suggest_categorical("lkg", [0, 1])
    o["STVT_EQ_LKG"] = lkg
    if lkg:
        o["STVT_EQ_LKG_RMS"] = trial.suggest_float("lkg_rms", 0.8, 2.0)
    o["STVT_EQ_FS_AVG_DEPTH"] = trial.suggest_categorical("fs_avg", [1, 2, 4])
    q = trial.suggest_categorical("quality_reset", [0, 6, 8, 10])
    if q:
        o["STVT_EQ_QUALITY_BAD_RMS"] = q
    gear = trial.suggest_categorical("gear", [0, 1])
    if gear:
        o["STVT_EQ_GEAR_LMS"] = 1
        o["STVT_EQ_BETA_SLOW"] = trial.suggest_float("beta_slow", 2e-7, 5e-6,
                                                     log=True)
    o["STVT_RRC_SYMS"] = trial.suggest_categorical("rrc_syms", [6, 8, 10])
    o["STVT_SPS"] = trial.suggest_categorical("sps", ["1.1", "1.5"])
    o["STVT_FPLL_ALPHA"] = trial.suggest_float("fpll_alpha", 3e-4, 3e-3,
                                               log=True)
    return o


def cmd_verify(args):
    iq = Path(args.iq)
    print(f"  determinism check on {iq.name} (2 identical baseline runs)...")
    a = replay(iq, {}, "verify_a")
    print(f"    run A: headers={a['headers']} err={a['err_lines']} "
          f"MER={a['mer_db']} sha={a['sha16']} ({a['elapsed_s']}s)")
    b = replay(iq, {}, "verify_b")
    print(f"    run B: headers={b['headers']} err={b['err_lines']} "
          f"MER={b['mer_db']} sha={b['sha16']} ({b['elapsed_s']}s)")
    same = a["sha16"] == b["sha16"]
    print(f"  -> {'DETERMINISTIC (bit-identical TS)' if same else 'MISMATCH'}")
    if not same and a["headers"] == b["headers"]:
        print("     (same header count — scoring still stable enough to use)")


def cmd_run(args):
    over = dict(kv.split("=", 1) for kv in (args.set or []))
    m = replay(Path(args.iq), over, "manual")
    print(json.dumps({**m, "overrides": over}, indent=2))


def cmd_optimize(args):
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    LAB.mkdir(exist_ok=True)
    storage = f"sqlite:///{(LAB / 'optuna.db').as_posix()}"
    study = optuna.create_study(study_name=args.study, storage=storage,
                                direction="maximize", load_if_exists=True,
                                sampler=optuna.samplers.TPESampler(seed=7))
    iq = Path(args.iq)

    # trial 0 of a fresh study = the production baseline, so the search
    # always knows what it has to beat
    if not study.trials:
        base = replay(iq, {}, "t_base")
        study.add_trial(optuna.trial.create_trial(
            params={}, distributions={}, value=base["score"],
            user_attrs={"metrics": base, "overrides": {}}))
        print(f"  baseline: score={base['score']} headers={base['headers']} "
              f"err={base['err_lines']} MER={base['mer_db']}")

    def objective(trial):
        over = suggest(trial)
        m = replay(iq, over, f"t{trial.number}")
        trial.set_user_attr("metrics", m)
        trial.set_user_attr("overrides", over)
        print(f"  trial {trial.number:>3}: score={m['score']:>8.2f}  "
              f"headers={m['headers']:>4}  err={m['err_lines']:>4}  "
              f"MER={m['mer_db']:>5.2f}  ({m['elapsed_s']}s)", flush=True)
        return m["score"]

    study.optimize(objective, n_trials=args.trials)
    bt = study.best_trial
    print(f"\n  study '{args.study}': {len(study.trials)} trials total")
    print(f"  BEST score={bt.value:.2f}  params={bt.params}")
    print(f"  env: {bt.user_attrs.get('overrides', {})}")


def cmd_report(args):
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    storage = f"sqlite:///{(LAB / 'optuna.db').as_posix()}"
    study = optuna.load_study(study_name=args.study, storage=storage)
    done = [t for t in study.trials if t.value is not None]
    done.sort(key=lambda t: -t.value)
    print(f"  study '{args.study}': {len(done)} scored trials\n")
    print(f"  {'rank':>4} {'score':>9} {'hdrs':>5} {'err':>5} {'MER':>6}  params")
    for i, t in enumerate(done[:10]):
        m = t.user_attrs.get("metrics", {})
        print(f"  {i:>4} {t.value:>9.2f} {m.get('headers', '?'):>5} "
              f"{m.get('err_lines', '?'):>5} {m.get('mer_db', '?'):>6}  "
              f"{t.params if t.params else '(baseline)'}")
    if len(done) >= 8:
        try:
            imp = optuna.importance.get_param_importances(study)
            print("\n  knob importance (which variables actually matter):")
            for k, v in imp.items():
                print(f"    {k:<16} {v:.3f}")
        except Exception as e:
            print(f"\n  (importance unavailable: {e})")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify");   v.add_argument("--iq", required=True)
    r = sub.add_parser("run");      r.add_argument("--iq", required=True)
    r.add_argument("--set", action="append", metavar="K=V")
    o = sub.add_parser("optimize"); o.add_argument("--iq", required=True)
    o.add_argument("--trials", type=int, default=10)
    o.add_argument("--study", default="default")
    p = sub.add_parser("report");   p.add_argument("--study", default="default")
    args = ap.parse_args()
    {"verify": cmd_verify, "run": cmd_run,
     "optimize": cmd_optimize, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
