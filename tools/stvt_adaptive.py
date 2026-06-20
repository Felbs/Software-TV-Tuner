#!/usr/bin/env python3
"""stvt_adaptive — self-healing, config-adaptive chain controller.

The dynamic "tune the chain to ANY antenna (and ANY box)" layer. Instead of
running one fixed config and restarting it on failure (what stvt_run.sh does),
this picks the RICHEST decoder config that holds real-time RIGHT NOW for the
current antenna + signal + CPU, and shifts up/down as conditions change.

Why this is the answer (measured all session): this box is CPU-bound at the
lean baseline, so every quality lever (sharper matched filter, erasure RS,
soft Viterbi, DFE, TEISCRUB) tips it under real-time on a weak signal but is
free headroom on a strong one or a faster box. No single fixed config is right
everywhere — you have to MEASURE and ADAPT. That is exactly what a TV's
firmware does; this is the software equivalent.

CORE SIGNAL — live.ts growth rate (real-time fraction), read with stat().
Zero CPU cost. NEVER ffprobe/ffmpeg-null the live chain — that steals the
decoder's core and CAUSES the droughts we'd be trying to fix.

THE DISCRIMINATOR — OsO overflow markers in the chain log:
  * rate low  + OsO climbing  -> the chain can't keep up = config too heavy
                                 -> STEP DOWN a tier (lean out, recover real-time)
  * rate low  + no OsO        -> a SIGNAL FADE (bits didn't arrive; no config
                                 recovers them) -> HOLD, don't thrash configs
  * rate solid + headroom     -> spare capacity -> STEP UP a tier (more quality)

Audio-pop protection lives in the PLAYER (stvt_play_hd.sh: ffmpeg
+discardcorrupt + mpv alimiter), which has CPU headroom under GPU decode — so
it is tier-independent and always on. The chain stays lean.

Usage:
  python3 tools/stvt_adaptive.py --rf 31 --program 1 [--start-tier 2]
  (source ~/.stvt_autocal.env first, or pass --ifgr; Ctrl-C stops cleanly)
"""
from __future__ import annotations
import argparse, os, signal, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIVE_TS = HERE / "data" / "tv_live" / "live.ts"
CHAIN_LOG = HERE / "data" / "tv_live" / "tv_tuner.tv_live.log"
REALTIME_BPS = 2_540_000        # ATSC payload bytes/sec = 100% real-time

# Config tiers, leanest (0) -> richest (N-1). Each is env overrides on top of
# the production base. The controller climbs toward richer until real-time
# pressure (OsO) pushes it back. Tier 0 is the always-real-time floor.
TIERS = [
    {"name": "lean",     "env": {"STVT_SPS": "1.1", "STVT_RRC_SYMS": "4",
                                 "STVT_VITERBI": "hard", "STVT_RS": "stock"}},
    {"name": "erasure",  "env": {"STVT_SPS": "1.1", "STVT_RRC_SYMS": "4",
                                 "STVT_VITERBI": "hard", "STVT_RS": "erasure"}},
    {"name": "sharp_mf", "env": {"STVT_SPS": "1.3", "STVT_RRC_SYMS": "6",
                                 "STVT_VITERBI": "hard", "STVT_RS": "stock"}},
    {"name": "full",     "env": {"STVT_SPS": "1.5", "STVT_RRC_SYMS": "8",
                                 "STVT_VITERBI": "soft", "STVT_RS": "erasure"}},
]

BASE_ENV = {"STVT_EQ": "long", "STVT_FPLL_FOLD": "1", "STVT_FPLL_BLOCK_NCO": "1",
            "STVT_MPV_HWDEC": "auto"}          # GPU decode frees the chain's core

# Control parameters.
SAMPLE_SEC      = 8       # how often we sample rate + OsO
RT_FLOOR        = 0.90    # below this real-time fraction = "not keeping up"
RT_GOOD         = 0.96    # above this = solid, candidate to step up
DOWN_STRIKES    = 2       # consecutive (low+OsO) samples to step DOWN (fast)
UP_STRIKES      = 6       # consecutive solid samples to step UP (conservative)
COOLDOWN_SAMPLES = 3      # ignore decisions for N samples after a tier change
DEAD_RESTARTS_MAX = 20    # bound on chain-death restarts


def log(msg: str):
    print(f"[adaptive {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def pgrep(pat: str) -> list[int]:
    out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True).stdout
    return [int(x) for x in out.split() if x.isdigit()]


def teardown():
    """Graceful stop of chain+player; never -9 (SDRplay firmware wedge)."""
    for pat in (r"stvt_run\.sh", r"stvt_play_hd", r"python3.*tv_live\.py",
                r"^mpv ", r"tail -c 25000000"):
        for p in pgrep(pat):
            try: os.kill(p, signal.SIGTERM)
            except ProcessLookupError: pass
    # wait until the chain releases the SDR
    for _ in range(20):
        if not pgrep(r"python3.*tv_live\.py"):
            break
        time.sleep(1)
    time.sleep(3)                                # SDRplay close/open cooldown


def launch(tier_idx: int, rf: int, prog: int, gain_env: dict):
    env = dict(os.environ)
    env.update(BASE_ENV)
    env.update(gain_env)
    env.update(TIERS[tier_idx]["env"])
    try: LIVE_TS.unlink()
    except FileNotFoundError: pass
    # stvt_run.sh supervises the player + within-tier drought re-acquire; we own
    # the cross-tier adaptation. setsid so it survives and we kill by pattern.
    subprocess.Popen(
        ["setsid", "bash", str(HERE / "stvt_run.sh"), str(rf), str(prog)],
        cwd=str(HERE), env=env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    log(f"launched tier {tier_idx} '{TIERS[tier_idx]['name']}' "
        f"({' '.join(k.replace('STVT_','')+'='+v for k,v in TIERS[tier_idx]['env'].items())})")


def ts_size() -> int:
    try: return LIVE_TS.stat().st_size
    except FileNotFoundError: return 0


def oso_count() -> int:
    try: return CHAIN_LOG.read_text(errors="ignore").count("OsO")
    except OSError: return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rf", type=int, required=True)
    ap.add_argument("--program", type=int, default=1)
    ap.add_argument("--start-tier", type=int, default=2,
                    help="initial tier index (default 2 = sharp_mf; it adapts)")
    ap.add_argument("--ifgr", default=os.environ.get("STVT_IFGR", "30"))
    ap.add_argument("--antenna", default=os.environ.get("STVT_ANTENNA", "Antenna A"))
    args = ap.parse_args()

    gain_env = {"STVT_IFGR": str(args.ifgr), "STVT_RFGAIN_SEL": "5",
                "STVT_ANTENNA": args.antenna}
    tier = max(0, min(len(TIERS) - 1, args.start_tier))

    stop = {"v": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("v", True))
    log(f"starting: RF{args.rf} prog{args.program} IFGR={args.ifgr} "
        f"'{args.antenna}', start tier {tier} '{TIERS[tier]['name']}'")

    teardown()
    launch(tier, args.rf, args.program, gain_env)
    last_size = ts_size(); last_oso = oso_count()
    low_oso_run = 0; good_run = 0; cooldown = COOLDOWN_SAMPLES
    dead_restarts = 0

    try:
        while not stop["v"]:
            time.sleep(SAMPLE_SEC)
            # chain alive?
            if not pgrep(r"python3.*tv_live\.py --rf %d" % args.rf):
                if dead_restarts >= DEAD_RESTARTS_MAX:
                    log("max chain-death restarts hit — stopping"); break
                dead_restarts += 1
                log(f"chain not running — restart #{dead_restarts} at tier {tier}")
                teardown(); launch(tier, args.rf, args.program, gain_env)
                last_size = ts_size(); last_oso = oso_count()
                cooldown = COOLDOWN_SAMPLES; continue

            size = ts_size(); oso = oso_count()
            dsize = size - last_size
            doso = oso - last_oso
            last_size, last_oso = size, oso
            if dsize < 0:                          # rotation; skip this sample
                continue
            rate = dsize / SAMPLE_SEC / REALTIME_BPS
            overloaded = doso > 0                   # new overflows since last sample

            tag = "OsO" if overloaded else "   "
            log(f"tier {tier} '{TIERS[tier]['name']}'  rate {rate*100:4.0f}%  {tag}")

            if cooldown > 0:
                cooldown -= 1
                continue

            # --- decision logic -------------------------------------------
            if rate < RT_FLOOR and overloaded:
                # chain can't keep up => config too heavy => step DOWN
                low_oso_run += 1; good_run = 0
                if low_oso_run >= DOWN_STRIKES and tier > 0:
                    tier -= 1
                    log(f"** CPU-bound (low rate + OsO) -> step DOWN to tier "
                        f"{tier} '{TIERS[tier]['name']}'")
                    teardown(); launch(tier, args.rf, args.program, gain_env)
                    last_size = ts_size(); last_oso = oso_count()
                    low_oso_run = 0; cooldown = COOLDOWN_SAMPLES
            elif rate >= RT_GOOD and not overloaded:
                # solid + headroom => try a richer tier
                good_run += 1; low_oso_run = 0
                if good_run >= UP_STRIKES and tier < len(TIERS) - 1:
                    tier += 1
                    log(f"** solid headroom -> step UP to tier {tier} "
                        f"'{TIERS[tier]['name']}' (will fall back if it can't hold)")
                    teardown(); launch(tier, args.rf, args.program, gain_env)
                    last_size = ts_size(); last_oso = oso_count()
                    good_run = 0; cooldown = COOLDOWN_SAMPLES
            else:
                # low rate but NO OsO = signal fade (no config recovers it), or
                # mid-band — just hold and let the signal come back.
                low_oso_run = 0; good_run = 0
    finally:
        log("stopping — tearing down chain")
        teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
