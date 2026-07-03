"""tv_soak.py — headless sentinel: watch the RF floor until the cable
window opens, keeping the chain calibrated the whole time.

No player, no windows — chain only, scored by liveness-guarded stream
metrics every minute. Every 30 min (or when quality degrades hard) it
re-probes gain cells and adopts the best. When the floor supports the
cable criterion (gaps/min <= 3 with full header rate) for 20 straight
minutes, it logs CABLE WINDOW OPEN — the signal that it's time to bring
the TV back up for the user.

Log tokens: SCORE / RECAL / SWITCHED / CABLE WINDOW OPEN / FATAL
"""
import json, os, re, statistics, subprocess, sys, time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tv_lab import (ts_metrics, build_env, start_chain, kill_chain,
                    kill_players, wait_decode, log, LIVE, PY, ROTATE_BYTES)

RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
CAL = str(Path(__file__).resolve().parent / "mer_gain_cal.py")

ARGS = SimpleNamespace(rf=36, program=3, antenna="Antenna A",
                       ifgr=32, rfgain=2)
CABLE_GAPS, CABLE_HDRS, HOLD_S = 3.0, 4.5, 20 * 60


def recal():
    """Probe two gain cells, return (rfgain, ifgr) of the better by MER."""
    kill_chain(); time.sleep(2)
    best, best_mer = (ARGS.rfgain, ARGS.ifgr), -1
    for rfsel, ifgr in ((2, 32), (3, 40)):
        try:
            out = subprocess.run(
                [PY, CAL, "--rf", str(ARGS.rf), "--antenna", ARGS.antenna,
                 "--cells", f"{rfsel}:{ifgr}"],
                capture_output=True, text=True, timeout=120,
                env=build_env(ARGS, 0)).stdout
            m = re.search(r"MER=([\d.]+)", out)
            mer = float(m.group(1)) if m else 0.0
        except Exception:
            mer = 0.0
        if mer > best_mer:
            best, best_mer = (rfsel, ifgr), mer
    return best, best_mer


def main():
    kill_players(); kill_chain(); time.sleep(2)
    env = build_env(ARGS, 0)
    chain = start_chain(ARGS.rf, env)
    if not wait_decode():
        log("FATAL initial chain no video"); sys.exit(1)
    log(f"sentinel up: RF{ARGS.rf} {ARGS.rfgain}:{ARGS.ifgr}, "
        f"watching for gaps/min<={CABLE_GAPS} + hdrs/s>={CABLE_HDRS} "
        f"held {HOLD_S//60} min")
    hold = None
    last_recal = time.time()
    recent = []
    while True:
        time.sleep(60)
        if chain.poll() is not None:
            log("chain died — relaunch")
            chain = start_chain(ARGS.rf, env)
            wait_decode(); continue
        if LIVE.exists() and LIVE.stat().st_size > ROTATE_BYTES:
            log("rotate 6GB")
            chain.terminate()
            try: chain.wait(timeout=6)
            except Exception: chain.kill()
            time.sleep(2)
            chain = start_chain(ARGS.rf, env); wait_decode(); continue
        m = ts_metrics(60)
        if not m: continue
        ok = m["gaps_min"] <= CABLE_GAPS and m["hdrs_s"] >= CABLE_HDRS
        recent.append(m["gaps_min"]); recent[:-15] = []
        log(f"SCORE gaps/min={m['gaps_min']:.1f} hdrs/s={m['hdrs_s']:.1f} "
            f"real={m['real_pct']:.0f}% {'OK' if ok else '--'}"
            + (f" hold={int(time.time()-hold)}s" if ok and hold else ""))
        if ok:
            hold = hold or time.time()
            if time.time() - hold >= HOLD_S:
                log("CABLE WINDOW OPEN — floor has supported cable "
                    "quality for 20 min")
                hold = None
        else:
            hold = None
        # periodic / degradation-triggered recal
        degraded = len(recent) >= 10 and statistics.median(recent[-10:]) > 40
        if time.time() - last_recal > 1800 or degraded:
            log(f"RECAL ({'degraded' if degraded else 'periodic'})")
            chain.terminate()
            try: chain.wait(timeout=6)
            except Exception: chain.kill()
            (rfsel, ifgr), mer = recal()
            if (rfsel, ifgr) != (ARGS.rfgain, ARGS.ifgr):
                log(f"SWITCHED gain {ARGS.rfgain}:{ARGS.ifgr} -> "
                    f"{rfsel}:{ifgr} (MER {mer:.2f})")
                ARGS.rfgain, ARGS.ifgr = rfsel, ifgr
                env = build_env(ARGS, 0)
            last_recal = time.time()
            chain = start_chain(ARGS.rf, env)
            wait_decode()


if __name__ == "__main__":
    main()
