"""Auto-tune: A/B test chain configs on the live SDR, score each, pick the
best one for THIS antenna+RF+SNR. Saves the winning config back to the
antenna profile so future tunes use it automatically.

The scoring uses the FPLL telemetry the chain prints continuously:
  - mean|x|  → lower = tighter lock (target ≤ 0.05)
  - max|x|   → lower = less per-sample variance
  - in_rms   → higher = more useful signal energy
  - OsO mark → presence = chain CPU-bound, BAD

Score formula: (in_rms / 100) - 10*mean|x| - 5*max|x| - 50*OsO_count
Higher = better.

Configs tested (combinations the user is unlikely to enumerate by hand):
  - long + soft viterbi                                  (baseline)
  - pilot_dd + soft + FUSED                              (CPU efficient + better EQ)
  - pilot_dd_soft + soft + FUSED                         (best EQ, possibly OsO)
  - multifs_dd + soft + FUSED                            (multi-FS averaging)
  - long + soft + FUSED + SDR_AGC                        (hardware AGC adapts to fade)

Usage:
    python auto_tune.py --rf 7 --antenna "Antenna C"
    python auto_tune.py --rf 7 --antenna "Antenna C" --duration 20  # 20s per config
    python auto_tune.py --apply       # also runs the winning config + player
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE        = Path(__file__).resolve().parent
PROFILE_DIR = HERE / "profiles"
TV_LIVE     = Path(r"Z:\src\magic-tv-decoder\tools\tv_live.py")
PY          = os.path.join(os.environ["USERPROFILE"], "radioconda", "python.exe")
SDRPLAY_DLL = r"C:\Program Files\SDRplay\API\x64"

FPLL_RE = re.compile(
    r"(OsO)?\[fpll t=\s*([\d.]+)s\]\s+nco_freq_hz=([-\d.]+)\s+"
    r"mean\|x\|=([\d.]+)\s+rms_x=([\d.]+)\s+max\|x\|=([\d.]+)\s+"
    r"in_rms=([\d.]+)\s+out_rms=([\d.]+)"
)


@dataclass
class ConfigResult:
    name: str
    env: dict
    samples_collected: int = 0
    mean_x_avg: float = 0.0
    max_x_avg: float = 0.0
    in_rms_avg: float = 0.0
    out_rms_avg: float = 0.0
    oso_count: int = 0
    score: float = 0.0
    locked: bool = False
    error: str | None = None


# Curated config combinations to try, in (display_name, env_dict) form.
# RFNOTCH=1 + STVT_VITERBI=soft are baseline good and always on.
BASE_ENV = {
    "STVT_RFNOTCH":   "1",
    "STVT_VITERBI":   "soft",
    "STVT_IFGR":      "30",
    "STVT_RFGAIN_SEL": "4",
}

CONFIGS_TO_TEST = [
    ("long-EQ baseline",            {"STVT_EQ": "long"}),
    ("pilot_dd + FUSED",            {"STVT_EQ": "pilot_dd",
                                     "STVT_RXF_FUSED": "1",
                                     "STVT_RXF_FUSED_GAIN": "8"}),
    ("pilot_dd_soft + FUSED",       {"STVT_EQ": "pilot_dd_soft",
                                     "STVT_RXF_FUSED": "1",
                                     "STVT_RXF_FUSED_GAIN": "8"}),
    ("multifs_dd + FUSED",          {"STVT_EQ": "multifs_dd",
                                     "STVT_RXF_FUSED": "1",
                                     "STVT_RXF_FUSED_GAIN": "8"}),
    ("long + FUSED + SDR_AGC",      {"STVT_EQ": "long",
                                     "STVT_RXF_FUSED": "1",
                                     "STVT_RXF_FUSED_GAIN": "8",
                                     "STVT_SDR_AGC": "1"}),
]


def run_chain_briefly(rf: int, antenna: str, env_extra: dict, duration_sec: int) -> tuple[list[dict], str]:
    """Run tv_live.py for N seconds with given env, parse FPLL stats from stderr/stdout."""
    env = os.environ.copy()
    env.update(BASE_ENV)
    env.update(env_extra)
    env["STVT_ANTENNA"] = antenna
    env["PATH"] = SDRPLAY_DLL + os.pathsep + env.get("PATH", "")

    cmd = [PY, "-u", str(TV_LIVE), "--rf", str(rf)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace",
                             env=env)
    samples = []
    log_lines = []
    started = time.time()
    err = None
    try:
        for line in proc.stdout:
            log_lines.append(line)
            m = FPLL_RE.search(line)
            if m:
                oso = m.group(1) == "OsO"
                t = float(m.group(2))
                samples.append({
                    "t": t, "mean_x": float(m.group(4)), "max_x": float(m.group(6)),
                    "in_rms": float(m.group(7)), "out_rms": float(m.group(8)),
                    "oso": oso,
                })
            if time.time() - started > duration_sec:
                break
            # Bail early if chain crashes
            if proc.poll() is not None:
                err = f"chain exited (returncode {proc.returncode})"
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
    return samples, err or ""


def score_config(samples: list[dict]) -> ConfigResult:
    if not samples:
        return ConfigResult("?", {}, locked=False, error="no FPLL samples")
    # Use only stats after t > 30s (post-equalizer convergence)
    steady = [s for s in samples if s["t"] > 30.0]
    if not steady:
        steady = samples[len(samples) // 2:]   # fallback: use last half
    mean_x   = sum(s["mean_x"] for s in steady) / len(steady)
    max_x    = sum(s["max_x"]  for s in steady) / len(steady)
    in_rms   = sum(s["in_rms"] for s in steady) / len(steady)
    out_rms  = sum(s["out_rms"] for s in steady) / len(steady)
    oso      = sum(1 for s in steady if s["oso"])
    locked   = mean_x < 0.06
    # Score (higher is better)
    score = (in_rms / 100) - 10 * mean_x - 5 * max_x - 50 * (oso / len(steady))
    r = ConfigResult(name="?", env={}, samples_collected=len(steady),
                      mean_x_avg=mean_x, max_x_avg=max_x,
                      in_rms_avg=in_rms, out_rms_avg=out_rms,
                      oso_count=oso, score=score, locked=locked)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, required=True)
    ap.add_argument("--antenna", default="Antenna C")
    ap.add_argument("--duration", type=int, default=45,
                    help="seconds per config (need >30s to settle + steady-state samples)")
    ap.add_argument("--apply", action="store_true",
                    help="after picking winner, restart tv_live with it + launch player")
    ap.add_argument("--profile", default=None,
                    help="path to existing antenna profile JSON to update")
    args = ap.parse_args()

    print(f"[auto-tune] RF{args.rf}  {args.antenna}  duration={args.duration}s/config")
    print(f"[auto-tune] {len(CONFIGS_TO_TEST)} configs to test  (~{len(CONFIGS_TO_TEST) * args.duration}s total)")
    print()

    results: list[ConfigResult] = []
    for i, (name, env_extra) in enumerate(CONFIGS_TO_TEST, 1):
        print(f"[{i}/{len(CONFIGS_TO_TEST)}] {name}")
        print(f"    env: {env_extra}")
        samples, err = run_chain_briefly(args.rf, args.antenna, env_extra, args.duration)
        r = score_config(samples)
        r.name = name
        r.env = env_extra
        r.error = err or None
        results.append(r)
        if r.locked:
            print(f"    LOCKED  mean|x|={r.mean_x_avg:.4f}  max|x|={r.max_x_avg:.3f}  "
                  f"in_rms={r.in_rms_avg:.0f}  oso={r.oso_count}/{r.samples_collected}  "
                  f"score={r.score:.2f}")
        else:
            print(f"    NO LOCK ({err or 'mean|x| too high'})")
        time.sleep(2)  # SDR cooldown between runs

    # Summary table
    print("\n" + "=" * 78)
    print(f"{'config':<32} {'locked':<7} {'mean|x|':<8} {'max|x|':<7} {'in_rms':<7} {'OsO':<6} {'score':<7}")
    print("=" * 78)
    for r in sorted(results, key=lambda x: -x.score):
        lock = "YES" if r.locked else "no"
        print(f"{r.name:<32} {lock:<7} {r.mean_x_avg:<8.4f} {r.max_x_avg:<7.3f} "
              f"{r.in_rms_avg:<7.0f} {r.oso_count:<6} {r.score:<7.2f}")

    locked_results = [r for r in results if r.locked]
    if not locked_results:
        print("\n[auto-tune] NO config achieved lock — bad antenna setup")
        return
    winner = max(locked_results, key=lambda x: x.score)
    print(f"\n[auto-tune] WINNER: {winner.name}  (score={winner.score:.2f})")
    print(f"            env: {winner.env}")

    # Persist back to antenna profile
    prof_path = Path(args.profile) if args.profile else \
                PROFILE_DIR / f"RF{args.rf}_{args.antenna.replace(' ', '')}.json"
    if prof_path.exists():
        prof = json.loads(prof_path.read_text())
        prof["auto_tune_winner"] = {
            "name": winner.name, "env": winner.env, "score": winner.score,
            "mean_x": winner.mean_x_avg, "max_x": winner.max_x_avg,
            "in_rms": winner.in_rms_avg, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "all_results": [{"name": r.name, "score": r.score, "locked": r.locked,
                              "mean_x": r.mean_x_avg, "in_rms": r.in_rms_avg,
                              "oso": r.oso_count} for r in results],
        }
        prof_path.write_text(json.dumps(prof, indent=2))
        print(f"[auto-tune] saved winner to {prof_path}")
    else:
        print(f"[auto-tune] no profile at {prof_path} — run antenna_profile.py first")

    if args.apply:
        print("\n[auto-tune] launching tv_live with winning config + player...")
        chain_env = os.environ.copy()
        chain_env.update(BASE_ENV)
        chain_env.update(winner.env)
        chain_env["STVT_ANTENNA"] = args.antenna
        chain_env["PATH"] = SDRPLAY_DLL + os.pathsep + chain_env.get("PATH", "")
        chain = subprocess.Popen([PY, "-u", str(TV_LIVE), "--rf", str(args.rf)],
                                  env=chain_env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[auto-tune] chain pid {chain.pid} — waiting 30s for lock")
        time.sleep(30)
        player_cmd = [PY, str(HERE / "play_marginal.py"), "1"]
        print(f"[auto-tune] launching player: {' '.join(player_cmd)}")
        player = subprocess.Popen(player_cmd)
        print("[auto-tune] running. Ctrl-C to stop.")
        try:
            player.wait()
        except KeyboardInterrupt:
            pass
        finally:
            for p in (player, chain):
                if p.poll() is None:
                    p.terminate()


if __name__ == "__main__":
    main()
