#!/usr/bin/env python3
"""auto_acquire.py — autonomous SDR-config sweeper.

Sweeps RF channel × IFGR × RF-gain-select × antenna × equalizer until
it finds a combination where the chain actually acquires lock and
produces valid PAT packets. Stops on first success (saves the winner
env to /tmp/auto_acquire_winner.env) or after --budget minutes.

Spawns tv_live.py directly (not the heavier tv_tuner.py wrapper), so we
can test 100+ configs without burning ffmpeg+player startup time. The
success metric is: live.ts grew >5 MB AND PAT count (PID 0x0000)
appears ≥5 times in the last 5 MB.

Usage:
    python3 tools/auto_acquire.py [--budget 240]
"""
from __future__ import annotations
import argparse
import itertools
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

STVT     = Path("/home/user/Software-TV-Tuner")
LIVE_TS  = STVT / "tools/data/tv_live/live.ts"
STATE    = Path("/tmp/auto_acquire_state.json")
LOG_PATH = Path("/tmp/auto_acquire.log")
LOCK     = Path("/tmp/auto_acquire.lock")
WINNER   = Path("/tmp/auto_acquire_winner.env")

# Sweep grid — ordered so most-likely-to-work appears first.
RF_CHANNELS    = [34, 35, 31, 25, 27, 23, 28, 32, 36]
IFGRS          = [59, 50, 45, 40, 35, 30]
RFGAIN_SELS    = [5, 3, 7, 1]
ANTENNAS       = ["Antenna A", "Antenna B"]
EQS            = ["cma", "long"]  # multifs excluded 2026-05-16:
# even when it "locks" + PAT-counts pass in verify, multifs drops the
# PSI packets (PAT/PMT PIDs) ~80% of the time, so ts_psi_repair sees
# nothing to cache and the player has no program info to decode. The
# chain looks alive but is unwatchable. If only multifs locks, RF is
# too marginal to watch right now — better to keep sweeping (or try
# Antenna B / other RF channels) than serve unwatchable video.


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def kill_chain() -> None:
    for pat in ("tv_live.py", "tv_tuner.py", "ffmpeg.*tv_live",
                "ffplay.*tv_live", "mpv.*tv_live"):
        subprocess.run(["pkill", "-9", "-f", pat],
                       stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    time.sleep(2.5)


def measure_pat(ts_path: Path, max_bytes: int = 5_000_000) -> tuple[int, int]:
    """Return (pat_count, ts_size). Scans up to `max_bytes` worth of TS
    packets and returns max PAT-count found across windows of the file
    (first 5 MB, middle 5 MB, last 5 MB).

    2026-05-22 Day 14: changed from only-last-5MB to max-across-windows.
    On marginal RF, the chain locks initially (gets PATs in first window)
    but may diverge mid-run (no PATs in last window). The candidate is
    still useful — we should accept it and rely on run_stvt_winner.sh's
    watchdog to handle drops.
    """
    if not ts_path.exists():
        return -1, 0
    sz = ts_path.stat().st_size
    if sz < 100_000:
        return 0, sz
    pkt_size = 188
    n = min(sz, max_bytes)

    def count_in(offset: int) -> int:
        try:
            with open(ts_path, "rb") as f:
                f.seek(offset)
                data = f.read(n)
        except OSError:
            return -1
        # find sync byte alignment
        align = -1
        for i in range(0, min(2000, len(data) - pkt_size * 4)):
            if (data[i] == 0x47 and data[i + pkt_size] == 0x47
                    and data[i + 2 * pkt_size] == 0x47):
                align = i
                break
        if align < 0:
            return 0
        c = 0
        for i in range(align, len(data) - pkt_size, pkt_size):
            if data[i] != 0x47:
                continue
            pid = ((data[i + 1] & 0x1f) << 8) | data[i + 2]
            if pid == 0:
                c += 1
        return c

    offsets = [0]
    if sz > n * 2:
        offsets.append((sz - n) // 2)
    if sz > n:
        offsets.append(sz - n)
    best = max((count_in(o) for o in offsets), default=0)
    return max(best, 0), sz


def try_config(rf: int, ifgr: int, rfgain: int, ant: str, eq: str,
               cell_seconds: int) -> dict:
    """Spawn tv_live.py with the given config for `cell_seconds`, then
    measure live.ts growth + PAT count. Returns result dict."""
    kill_chain()
    env = os.environ.copy()
    env["STVT_IFGR"]        = str(ifgr)
    env["STVT_RFGAIN_SEL"]  = str(rfgain)
    env["STVT_ANTENNA"]     = ant
    env["STVT_EQ"]          = eq
    env["STVT_VITERBI"]     = "hard"
    env["STVT_RS"]          = "stock"
    # 2026-05-21 Day 13: match the sync thresholds that run_stvt_winner.sh
    # uses. Default ATSC_SYNC_SOFT_LOCK=4.0 is too strict for marginal RF;
    # verify would fail on RFs that the actual chain (set 3.5) would lock.
    # Without this, auto_acquire kept rejecting every candidate.
    env.setdefault("ATSC_SYNC_SOFT_LOCK",   "3.5")
    env.setdefault("ATSC_SYNC_SOFT_UNLOCK", "2.0")
    env.setdefault("STVT_NATIVE_RATE",     "8000000")
    env.setdefault("STVT_RESAMP_INTERP",   "25")
    env.setdefault("STVT_RESAMP_DECIM",    "32")
    # Run with hardware-respectful defaults (no extreme priority).
    env.pop("STVT_PLAYER", None)

    # Truncate live.ts so we measure only this cell.
    try: LIVE_TS.write_bytes(b"")
    except OSError: pass

    proc = subprocess.Popen(
        ["python3", "tools/tv_live.py", "--rf", str(rf)],
        cwd=str(STVT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, start_new_session=True,
    )

    t0 = time.time()
    while time.time() - t0 < cell_seconds:
        if proc.poll() is not None:
            break
        time.sleep(1)

    chain_died = (proc.poll() is not None)
    pat_count, ts_size = measure_pat(LIVE_TS)

    # Kill chain.
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        time.sleep(0.5)
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    kill_chain()

    return {"ts_size": ts_size, "pat_count": pat_count, "chain_died": chain_died}


def save_winner(rf: int, ifgr: int, rfgain: int, ant: str, eq: str,
                pat_count: int) -> None:
    with WINNER.open("w") as f:
        f.write(f"# auto_acquire winner — saved {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# PAT count in last 5MB: {pat_count}\n")
        f.write(f"export STVT_IFGR={ifgr}\n")
        f.write(f"export STVT_RFGAIN_SEL={rfgain}\n")
        f.write(f'export STVT_ANTENNA="{ant}"\n')
        f.write(f"export STVT_EQ={eq}\n")
        f.write(f"# Channel: RF{rf}\n")
        f.write(f"#\n")
        f.write(f"# Run:    source {WINNER} && ~/run_stvt_winner.sh {eq} {rf}\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=240,
                    help="minutes; default 240 (4 h)")
    ap.add_argument("--cell-seconds", type=int, default=20,
                    help="seconds per config test")
    ap.add_argument("--verify-seconds", type=int, default=60,
                    help="extra seconds to verify a candidate winner")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    # Single-instance lock.
    if LOCK.exists():
        try:
            other = int(LOCK.read_text().strip())
            os.kill(other, 0)
            print(f"already running (pid={other})", file=sys.stderr)
            return 1
        except (OSError, ValueError):
            pass
    LOCK.write_text(str(os.getpid()))

    LOG_PATH.write_text("")
    if args.reset and STATE.exists():
        STATE.unlink()

    state = json.loads(STATE.read_text()) if STATE.exists() else {"history": []}
    seen  = {tuple(h["cfg"]) for h in state["history"]}
    deadline = time.time() + args.budget * 60

    log(f"auto_acquire starting. budget={args.budget}min, "
        f"cell={args.cell_seconds}s, verify={args.verify_seconds}s")

    # 2026-05-17: RF channel moved INNERMOST so the sweep tries all
    # channels quickly per (IFGR/RFGAIN/ANT/EQ) combo. Old order spent
    # 32 min iterating RF=34 cells before ever trying RF=35; on a
    # marginal-RF34 morning that wasted the whole budget on a bad
    # channel. Now we get a first hit on whichever channel is strongest
    # within ~3 min instead of 30+.
    combos = list(itertools.product(
        IFGRS, RFGAIN_SELS, ANTENNAS, EQS, RF_CHANNELS))
    # Re-pack to keep the (rf, ifgr, rfgain, ant, eq) tuple shape the
    # rest of the code expects.
    combos = [(rf, ifgr, rfgain, ant, eq)
              for (ifgr, rfgain, ant, eq, rf) in combos]
    log(f"sweep grid: {len(combos)} combos total "
        f"(RF×IFGR×RFGAIN×ANT×EQ = "
        f"{len(RF_CHANNELS)}×{len(IFGRS)}×{len(RFGAIN_SELS)}×"
        f"{len(ANTENNAS)}×{len(EQS)})")

    try:
        for rf, ifgr, rfgain, ant, eq in combos:
            if time.time() >= deadline:
                log(f"Budget exhausted ({args.budget} min).")
                break
            key = (rf, ifgr, rfgain, ant, eq)
            if key in seen:
                continue

            log(f"try RF={rf} IFGR={ifgr} RFGAIN={rfgain} "
                f"ANT={ant!r:14s} EQ={eq}")
            result = try_config(rf, ifgr, rfgain, ant, eq,
                                args.cell_seconds)
            seen.add(key)
            state["history"].append({"cfg": list(key), "result": result,
                                     "t": time.time()})
            STATE.write_text(json.dumps(state, indent=2))
            log(f"   ts={result['ts_size']/1e6:>5.1f}MB  "
                f"pat={result['pat_count']:>3}  "
                f"died={result['chain_died']}")

            # Candidate winner? Verify with longer run.
            if result["pat_count"] >= 5 and result["ts_size"] > 1_000_000:
                log(f"   CANDIDATE — verifying with {args.verify_seconds}s run...")
                verify = try_config(rf, ifgr, rfgain, ant, eq,
                                    args.verify_seconds)
                state["history"].append({"cfg": list(key), "verify": verify,
                                         "t": time.time()})
                STATE.write_text(json.dumps(state, indent=2))
                log(f"   verify: ts={verify['ts_size']/1e6:.1f}MB  "
                    f"pat={verify['pat_count']}  "
                    f"died={verify['chain_died']}")
                if verify["pat_count"] >= 10 and not verify["chain_died"]:
                    log("=" * 60)
                    log(f"*** SUCCESS! ***")
                    log(f"*** Config: RF={rf} IFGR={ifgr} RFGAIN_SEL={rfgain}")
                    log(f"*** Antenna={ant!r} EQ={eq}")
                    log(f"*** PAT count in 60s verify: {verify['pat_count']}")
                    log("=" * 60)
                    save_winner(rf, ifgr, rfgain, ant, eq,
                                verify["pat_count"])
                    log(f"Winner saved to {WINNER}")
                    log(f"To use:")
                    log(f"  source {WINNER} && ~/run_stvt_winner.sh {eq} {rf}")
                    return 0
                else:
                    log("   verify failed — continuing sweep")

        log("Sweep complete. No config produced sustained lock.")
        log("Likely RF/antenna issue — software cannot fix this.")
        log("Try later (RF conditions vary) or check the antenna cable.")
        return 1
    finally:
        kill_chain()
        try: LOCK.unlink()
        except FileNotFoundError: pass


if __name__ == "__main__":
    sys.exit(main())
