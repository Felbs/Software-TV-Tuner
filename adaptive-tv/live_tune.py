"""live_tune.py — overnight autonomous LIVE tuning campaign.

The lab's replay optimization overfits to frozen captures (proven 2026-07-02:
replay champion scored 0 live). This is the honest version: every Optuna
trial starts the REAL chain on REAL RF, settles, and is scored by the real
quality judge (fps + concealment errors). Signal drift across the night is a
feature — a config only wins by being good for hours of changing sky.

Scoring: fps is normalized to the stream's native rate (60 vs 30 fps muxes
judged fairly — the RF34 lesson), then full-rate configs compete on v_err.

    python live_tune.py --hours 6 --channels "36:3,34:3,21:1" --study night1

At the end: relaunches live TV (chain + player) on the best config found and
writes lab/live_tune_report.txt. Log milestones: NEW BEST / DONE / FATAL.
"""
import argparse, json, os, re, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = HERE / "lab"
PY = r"C:\Users\user\radioconda\python.exe"
TV_LIVE = Path(r"Z:\src\magic-tv-decoder\tools\tv_live.py")
JUDGE = HERE / "quality_judge.py"
PLAYER = HERE / "play_marginal.py"
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
SDRPLAY_DLL = r"C:\Program Files\SDRplay\API\x64"
FFMPEG_DIR = r"C:\ffmpeg\bin"

RE_SCORE = re.compile(r"score=(\d+).*?fps=([\d.]+) v_err=([\d.]+)/s a_err=([\d.]+)")

SETTLE, WIN, NWIN = 25, 12, 2      # per-trial: settle + NWIN judge windows

BASE = {
    "STVT_EQ": "long", "STVT_VITERBI": "soft", "STVT_SPS": "1.1",
    "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1",
    "STVT_RFNOTCH": "1", "STVT_DABNOTCH": "1", "STVT_ANTENNA": "Antenna A",
    "STVT_SDR_AGC": "1", "STVT_EQ_TELEM": "1",
}


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)


def kill_stragglers():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'tv_live' -and $_.ProcessId -ne "
                    f"{os.getpid()}" + " } | ForEach-Object { Stop-Process -Id $_.ProcessId "
                    "-Force -ErrorAction SilentlyContinue }"],
                   capture_output=True, timeout=30)


def build_env(over):
    env = os.environ.copy()
    env["PATH"] = SDRPLAY_DLL + os.pathsep + FFMPEG_DIR + os.pathsep + env.get("PATH", "")
    env.update(BASE)
    env.update({k: str(v) for k, v in over.items()})
    return env


def start_chain(rf, env):
    if LIVE.exists():
        try: LIVE.unlink()
        except OSError: pass
    return subprocess.Popen([PY, "-u", str(TV_LIVE), "--rf", str(rf)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cc_probe(env):
    """Extract CEA-608 captions from the tail of live.ts. Returns
    (chars, wordlike_ratio, sample). Captions are known-structured English —
    the content-layer truth: real words = clean delivery, garbage = corruption
    that fps alone can miss."""
    try:
        snap = LAB / "cc_probe.ts"
        with open(LIVE, "rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - 30 * 1024 * 1024))
            snap.write_bytes(f.read())
        # relative path + cwd: the movie filter chokes on Windows drive colons
        p = subprocess.run(
            [FFMPEG_DIR + r"\ffmpeg.exe", "-v", "error", "-f", "lavfi",
             "-i", f"movie={snap.name}[out0+subcc]", "-map", "0:s:0",
             "-f", "srt", "-"],
            env=env, cwd=str(LAB), capture_output=True, text=True, timeout=60)
        text = re.sub(r"\d+\n[\d:,>\- ]+\n|<[^>]+>", "", p.stdout)
        words = re.findall(r"[A-Za-z']{2,}", text)
        letters = sum(len(w) for w in words)
        chars = len(re.sub(r"\s", "", text))
        ratio = round(letters / chars, 2) if chars else 0.0
        sample = " ".join(text.split())[:120]
        try: snap.unlink()
        except OSError: pass
        return chars, ratio, sample
    except Exception:
        return 0, 0.0, ""


def judge_once(program, env):
    try:
        out = subprocess.run([PY, str(JUDGE), "--program", str(program),
                              "--window", str(WIN)],
                             env=env, capture_output=True, text=True,
                             timeout=WIN + 45).stdout
    except Exception:
        return None
    m = RE_SCORE.search(out)
    if not m: return None
    return {"score": int(m.group(1)), "fps": float(m.group(2)),
            "v_err": float(m.group(3)), "a_err": float(m.group(4))}


def eval_config(rf, program, over):
    """One live trial: chain up, settle, NWIN judge windows, chain down.
    Returns (objective, detail). Objective: normalized fps first, then
    low v_err. 1000-scale so v_err subtraction can't flip a full-rate
    config below a broken one."""
    env = build_env(over)
    ch = start_chain(rf, env)
    try:
        time.sleep(SETTLE)
        wins = [w for w in (judge_once(program, env) for _ in range(NWIN)) if w]
        cc_chars, cc_ratio, cc_sample = cc_probe(env) if wins else (0, 0.0, "")
    finally:
        ch.terminate()
        try: ch.wait(timeout=6)
        except Exception: ch.kill()
    if not wins:
        return -100.0, {"note": "no judge output"}
    fps = sum(w["fps"] for w in wins) / len(wins)
    verr = sum(w["v_err"] for w in wins) / len(wins)
    aerr = sum(w["a_err"] for w in wins) / len(wins)
    native = 59.94 if fps > 36 else 29.97      # fps-normalization (RF34 fix)
    rate = min(1.0, fps / (native * 0.86))     # >=86% of native = full-rate
    if rate >= 1.0:
        obj = 1000.0 - verr - 2.0 * aerr       # full rate: compete on errors
    else:
        obj = rate * 800.0 - verr              # not full rate: fps dominates
    return obj, {"fps": round(fps, 1), "v_err": round(verr, 1),
                 "a_err": round(aerr, 1), "native": native, "n": len(wins),
                 "cc_chars": cc_chars, "cc_ratio": cc_ratio,
                 "cc": cc_sample}


def suggest(trial, channels):
    o = {}
    ch = trial.suggest_categorical("channel", [f"{rf}:{pr}" for rf, pr in channels])
    o["_rf"], o["_prog"] = (int(x) for x in ch.split(":"))
    o["STVT_RFGAIN_SEL"] = trial.suggest_categorical("rfgain", [2, 3, 4])
    o["STVT_AGC_SETPOINT"] = trial.suggest_categorical("setpoint", [-20, -25])
    rs = trial.suggest_categorical("rs", ["stock", "erasure"])
    o["STVT_RS"] = rs
    if rs == "erasure":
        o["STVT_RS_ERASURES"] = trial.suggest_int("erasures", 5, 12)
    o["STVT_EQ_LKG_RMS"] = trial.suggest_categorical("lkg_rms", [0.8, 1.0, 1.4])
    q = trial.suggest_categorical("quality_reset", [0, 8])
    if q: o["STVT_EQ_QUALITY_BAD_RMS"] = q
    o["STVT_FPLL_ALPHA"] = trial.suggest_categorical("fpll_alpha",
                                                     [0.001, 0.002])
    return o


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--channels", default="36:3,34:3,21:1",
                    help="rf:program pairs")
    ap.add_argument("--study", default="night_live")
    ap.add_argument("--no-tv", action="store_true",
                    help="skip TV relaunch at the end")
    args = ap.parse_args()
    channels = [tuple(int(x) for x in c.split(":"))
                for c in args.channels.split(",")]
    deadline = time.time() + args.hours * 3600

    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    LAB.mkdir(exist_ok=True)
    storage = f"sqlite:///{(LAB / 'optuna.db').as_posix()}"
    study = optuna.create_study(study_name=args.study, storage=storage,
                                direction="maximize", load_if_exists=True,
                                sampler=optuna.samplers.TPESampler(seed=11))
    jsonl = open(LAB / f"{args.study}_trials.jsonl", "a")
    best = [-1e9]
    kill_stragglers()
    log(f"CAMPAIGN START hours={args.hours} channels={channels} "
        f"study={args.study}")

    def objective(trial):
        over = suggest(trial, channels)
        rf, prog = over.pop("_rf"), over.pop("_prog")
        obj, detail = eval_config(rf, prog, over)
        rec = {"trial": trial.number, "t": time.strftime("%H:%M:%S"),
               "rf": rf, "prog": prog, "obj": round(obj, 1),
               **detail, "params": trial.params}
        jsonl.write(json.dumps(rec) + "\n"); jsonl.flush()
        trial.set_user_attr("detail", rec)
        log(f"trial {trial.number:>3}  RF{rf}  obj={obj:>7.1f}  {detail}")
        if obj > best[0]:
            best[0] = obj
            log(f"NEW BEST obj={obj:.1f} RF{rf} params={trial.params}")
        if time.time() > deadline:
            trial.study.stop()
        return obj

    try:
        study.optimize(objective, timeout=args.hours * 3600 + 120)
    except Exception as e:
        log(f"FATAL optimize loop: {e!r}")
    kill_stragglers()

    done = [t for t in study.trials if t.value is not None and t.params]
    done.sort(key=lambda t: -t.value)
    report = LAB / "live_tune_report.txt"
    with open(report, "w") as f:
        f.write(f"live_tune '{args.study}' — {len(done)} trials, "
                f"finished {time.strftime('%Y-%m-%d %H:%M')}\n\n")
        for i, t in enumerate(done[:12]):
            f.write(f"{i:>3}  obj={t.value:>7.1f}  "
                    f"{json.dumps(t.user_attrs.get('detail', t.params))}\n")
    log(f"report -> {report}")
    if not done:
        log("DONE (no successful trials)"); return

    bt = done[0]
    ch = bt.params["channel"]; rf, prog = (int(x) for x in ch.split(":"))
    over = {"STVT_RFGAIN_SEL": bt.params["rfgain"],
            "STVT_AGC_SETPOINT": bt.params["setpoint"],
            "STVT_RS": bt.params["rs"],
            "STVT_EQ_LKG_RMS": bt.params["lkg_rms"],
            "STVT_FPLL_ALPHA": bt.params["fpll_alpha"]}
    if bt.params["rs"] == "erasure":
        over["STVT_RS_ERASURES"] = bt.params.get("erasures", 7)
    if bt.params.get("quality_reset"):
        over["STVT_EQ_QUALITY_BAD_RMS"] = bt.params["quality_reset"]
    log(f"DONE best obj={bt.value:.1f} RF{rf} prog={prog} params={bt.params}")

    if args.no_tv: return
    env = build_env(over)
    chain = start_chain(rf, env)
    t0 = time.time(); ok = False
    while time.time() - t0 < 60:
        time.sleep(3)
        if LIVE.exists() and LIVE.stat().st_size > 12 * 1024 * 1024:
            with open(LIVE, "rb") as fh:
                fh.seek(-min(8 * 1024 * 1024, LIVE.stat().st_size), 2)
                if fh.read().count(b"\x00\x00\x01\xb3") >= 3:
                    ok = True; break
    if ok:
        subprocess.Popen([PY, str(PLAYER), str(prog), "--tail-mb", "15",
                          "--strong"], env=env)
        log(f"TV LAUNCHED on RF{rf} prog {prog} with best config")
    else:
        log("TV launch failed to lock; leaving chain down")
        chain.terminate()


if __name__ == "__main__":
    main()
