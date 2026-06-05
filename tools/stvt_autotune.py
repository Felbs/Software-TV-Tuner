#!/usr/bin/env python3
"""stvt_autotune.py — autonomous TV-quality tuner for WSL / SoapyRemote.

The "agent" that measures TV quality and searches the chain's config space
for the best picture. For each candidate config it:

  1. (re)spawns tv_live.py over the SoapyRemote transport with that config
  2. waits for live.ts to grow and the chain to lock
  3. measures objective decode quality with quality_judge.sh (ffmpeg
     null-decode -> fps + video/audio error rate -> 0-100 score), taking
     the median of several windows
  4. records the score (plus OsO overflow count + segment-sync % from the
     chain log), kills the chain, and cools the SDR down

It prints a leaderboard and writes the winning config to
~/.tv_tuner/best_quality.env (source it before launching the chain).

The quality meter (quality_judge.sh) is also usable standalone:
    STVT_LIVE_TS=tools/data/tv_live/live.ts tools/quality_judge.sh --program 3

Prereqs (same as the live chain): Windows SoapySDRServer running, socket
buffers raised. See HANDOFF.md.

Usage:
    python3 tools/stvt_autotune.py [--rf 34] [--program 3]
        [--samples 3] [--warmup 20] [--window 12] [--only LABEL,LABEL]
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIVE_TS = HERE / "data" / "tv_live" / "live.ts"
CHAIN_LOG = HERE / "data" / "tv_live" / "tv_tuner.tv_live.log"
JUDGE = HERE / "quality_judge.sh"
BEST_ENV = Path.home() / ".tv_tuner" / "best_quality.env"
STATE_JSON = Path("/tmp/stvt_autotune_quality.json")

# SoapyRemote transport (Windows host SoapySDRServer over TCP). Honors the
# environment if the caller already exported these (e.g. a non-default port).
REMOTE = {
    "STVT_SOAPY_ARGS": os.environ.get(
        "STVT_SOAPY_ARGS",
        "driver=remote,remote=127.0.0.1:55132,remote:driver=sdrplay"),
    "STVT_STREAM_ARGS": os.environ.get("STVT_STREAM_ARGS", "remote:prot=tcp"),
}
# Front-end gain / antenna — constant across configs (we tune the DSP).
FRONTEND = {
    "STVT_IFGR": os.environ.get("STVT_IFGR", "59"),
    "STVT_RFGAIN_SEL": os.environ.get("STVT_RFGAIN_SEL", "5"),
    "STVT_ANTENNA": os.environ.get("STVT_ANTENNA", "Antenna A"),
}

# Candidate configs, lean -> full quality. On a fast CPU the full-quality
# matched filter (SPS=1.5/RRC=8) should win; on a slow one it overflows
# (OsO) and a leaner config scores higher. The tuner decides empirically.
CONFIGS = [
    ("lean_hard",      {"STVT_SPS": "1.1", "STVT_RRC_SYMS": "4",
                        "STVT_EQ": "long", "STVT_VITERBI": "hard",
                        "STVT_RS": "stock", "STVT_TEISCRUB": "1"}),
    ("mid_soft",       {"STVT_SPS": "1.3", "STVT_RRC_SYMS": "6",
                        "STVT_EQ": "long", "STVT_VITERBI": "soft",
                        "STVT_RS": "stock", "STVT_TEISCRUB": "1"}),
    ("full_hard",      {"STVT_SPS": "1.5", "STVT_RRC_SYMS": "8",
                        "STVT_EQ": "long", "STVT_VITERBI": "hard",
                        "STVT_RS": "stock", "STVT_TEISCRUB": "1"}),
    ("full_soft",      {"STVT_SPS": "1.5", "STVT_RRC_SYMS": "8",
                        "STVT_EQ": "long", "STVT_VITERBI": "soft",
                        "STVT_RS": "stock", "STVT_TEISCRUB": "1"}),
    ("full_soft_eras", {"STVT_SPS": "1.5", "STVT_RRC_SYMS": "8",
                        "STVT_EQ": "long", "STVT_VITERBI": "soft",
                        "STVT_RS": "erasure", "STVT_TEISCRUB": "1"}),
]


def log(msg: str) -> None:
    print(f"[autotune {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def pids(pattern: str) -> list[int]:
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True,
                         text=True).stdout.split()
    return [int(p) for p in out if p.isdigit()]


def kill_chain() -> None:
    # [t] keeps the pattern from matching this tuner's own command line.
    for _ in range(2):
        ps = pids(r"[t]v_live\.py")
        if not ps:
            break
        for p in ps:
            try:
                os.kill(p, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(2)
    for p in pids(r"[t]v_live\.py"):
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass


def spawn_chain(rf: int, cfg: dict) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(REMOTE)
    env.update(FRONTEND)
    env.update(cfg)
    try:
        LIVE_TS.unlink()
    except FileNotFoundError:
        pass
    logf = open(CHAIN_LOG, "w")
    return subprocess.Popen(
        [sys.executable, "-u", "tv_live.py", "--rf", str(rf)],
        cwd=str(HERE), env=env, stdout=logf, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True)


def wait_lock(proc: subprocess.Popen, fill_mb: int, timeout: int) -> bool:
    """Wait until live.ts exceeds fill_mb (quality_judge needs >=50MB)."""
    need = fill_mb * 1_000_000
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            log("  chain process exited early")
            return False
        try:
            if LIVE_TS.stat().st_size >= need:
                return True
        except FileNotFoundError:
            pass
        time.sleep(2)
    return False


def measure(samples: int, window: int, program: int) -> list[int]:
    scores = []
    env = dict(os.environ)
    env["STVT_LIVE_TS"] = str(LIVE_TS)
    env["STVT_QUALITY_STATE"] = str(STATE_JSON)
    for i in range(samples):
        subprocess.run(
            ["bash", str(JUDGE), "--window", str(window),
             "--program", str(program)],
            env=env, capture_output=True, text=True,
            timeout=window + 40)
        try:
            st = json.loads(STATE_JSON.read_text())
            scores.append(int(st.get("score", 0)))
            log(f"  sample {i+1}/{samples}: score={st.get('score')} "
                f"fps={st.get('fps')} v_err/s={st.get('video_errors_per_sec')} "
                f"a_err/s={st.get('audio_errors_per_sec')}")
        except (OSError, json.JSONDecodeError, ValueError):
            scores.append(0)
            log(f"  sample {i+1}/{samples}: (no reading)")
    return scores


def chain_diag() -> dict:
    """Pull OsO overflow count + last segment-sync % from the chain log."""
    oso = 0
    segs = None
    try:
        txt = CHAIN_LOG.read_text(errors="ignore")
        oso = txt.count("OsO")
        for line in reversed(txt.splitlines()):
            if "segs_aligned" in line:
                segs = line.strip()[-60:]
                break
    except OSError:
        pass
    return {"oso": oso, "segs": segs}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=34)
    ap.add_argument("--program", type=int, default=3,
                    help="HD program number for the quality meter (RF34=3)")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--warmup", type=int, default=20,
                    help="seconds to let the equalizer settle before scoring")
    ap.add_argument("--window", type=int, default=12,
                    help="seconds of video each quality sample decodes")
    ap.add_argument("--fill-mb", type=int, default=60)
    ap.add_argument("--lock-timeout", type=int, default=45)
    ap.add_argument("--only", default="",
                    help="comma-separated config labels to test (default all)")
    args = ap.parse_args()

    todo = CONFIGS
    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        todo = [c for c in CONFIGS if c[0] in want]
    if not todo:
        log("no configs selected"); return 1

    log(f"tuning RF{args.rf} prog{args.program} over "
        f"{REMOTE['STVT_SOAPY_ARGS']}")
    log(f"{len(todo)} configs x {args.samples} samples "
        f"(window {args.window}s, warmup {args.warmup}s)")

    results = []
    try:
        for label, cfg in todo:
            log(f"=== config '{label}': "
                + " ".join(f"{k.replace('STVT_','')}={v}"
                           for k, v in cfg.items()))
            kill_chain()
            time.sleep(4)  # SDR release between tunes
            proc = spawn_chain(args.rf, cfg)
            if not wait_lock(proc, args.fill_mb, args.lock_timeout):
                log(f"  '{label}' did not fill {args.fill_mb}MB / locked — "
                    "scoring 0")
                results.append({"label": label, "cfg": cfg, "median": 0,
                                "scores": [], "diag": chain_diag(),
                                "reason": "no lock / no fill"})
                kill_chain()
                continue
            log(f"  locked; warmup {args.warmup}s ...")
            time.sleep(args.warmup)
            scores = measure(args.samples, args.window, args.program)
            diag = chain_diag()
            median = int(statistics.median(scores)) if scores else 0
            log(f"  -> median score {median}  OsO={diag['oso']}  "
                f"scores={scores}")
            results.append({"label": label, "cfg": cfg, "median": median,
                            "scores": scores, "diag": diag, "reason": "ok"})
            kill_chain()
    finally:
        kill_chain()

    results.sort(key=lambda r: r["median"], reverse=True)
    print("\n" + "=" * 64)
    print("  TV-QUALITY LEADERBOARD (higher = better picture)")
    print("=" * 64)
    print(f"  {'rank':<5}{'config':<16}{'score':<7}{'OsO':<6}scores")
    for i, r in enumerate(results, 1):
        print(f"  {i:<5}{r['label']:<16}{r['median']:<7}"
              f"{r['diag']['oso']:<6}{r['scores']}")
    print("=" * 64)

    if results and results[0]["median"] > 0:
        best = results[0]
        BEST_ENV.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# best TV-quality config found by stvt_autotune.py",
                 f"# RF{args.rf} prog{args.program}  median score "
                 f"{best['median']}  ({time.strftime('%Y-%m-%d %H:%M')})"]
        for k, v in {**REMOTE, **FRONTEND, **best["cfg"]}.items():
            lines.append(f'export {k}="{v}"')
        BEST_ENV.write_text("\n".join(lines) + "\n")
        print(f"\n  WINNER: {best['label']} (score {best['median']})")
        print(f"  saved -> {BEST_ENV}")
        print("  launch it with:")
        print(f"    set -a; . {BEST_ENV}; set +a; "
              f"python3 tools/tv_live.py --rf {args.rf}")
    else:
        print("\n  no config produced a watchable picture — check the SDR "
              "server / antenna.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        kill_chain()
        print("\n[autotune] interrupted; chain killed.")
        sys.exit(130)
