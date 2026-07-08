"""reg_check.py — did tonight's changes make GOOD signals worse?

Live paired A/B on one channel: chain with tonight's full panel env
(IQ ring on) vs identical env with the ring off. Metrics per arm from
the chain's own telemetry + the honest TS: MER median/min, RS bad rate,
TEI packets per MB of live.ts. Two rounds, ABBA order, ~2 min per arm.

    python reg_check.py --rf 9 --ant "Antenna B" --secs 110
"""
import argparse
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = Path(r"Z:\src\magic-tv-decoder\tools")
LIVE = TOOLS / "data" / "tv_live" / "live.ts"
PY = sys.executable
RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
RE_RS = re.compile(r"pkts=(\d+) ec=\d+ era_dec=\d+ era_ok=\d+ "
                   r"miscorr=\d+ bad=(\d+)")


def base_env(rf, ant, ring, arsenal=False):
    env = dict(os.environ)
    env["PATH"] = r"C:\Program Files\SDRplay\API\x64;" + env.get("PATH", "")
    env.update({
        "STVT_ANTENNA": ant, "STVT_IFGR": "32", "STVT_RFGAIN_SEL": "5",
        "STVT_SDR_AGC": "1", "STVT_AGC_SETPOINT": "-20",
        "STVT_EQ": "long", "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
        "STVT_DABNOTCH": "0" if rf < 14 else "1",
        "STVT_RS": "erasure", "STVT_RS_ERASURES": "0",
        "STVT_EQ_MOD12_GUARD": "1", "STVT_SPS": "1.1",
        "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1",
        "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
        "STVT_EQ_TELEM": "1", "STVT_EQ_CIR": "1",
        "STVT_EQ_TAP_CACHE": str(HERE / "lab" / "tapcache"),
        "STVT_IQ_RING": "35" if ring else "0",
        "STVT_IQ_RING_DIR": str(HERE / "lab" / "e7_ring"),
    })
    if arsenal:
        # the full marginal-signal arsenal, deliberately fired at a
        # STRONG channel: does it help, wash, or hurt? (user question
        # 7/07 late — data over doctrine)
        env.update({"STVT_RS_ERASURES": "14", "STVT_SOVA": "1",
                    "STVT_EQ_DFE": "1", "STVT_EQ_DFE_ANCHOR": "1",
                    "STVT_EQ_RESEED": "1"})
    return env


def run_arm(rf, ant, ring, secs, log_path, arsenal=False):
    try:
        LIVE.unlink()
    except OSError:
        pass
    lf = open(log_path, "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen([PY, "-u", str(TOOLS / "tv_live.py"),
                          "--rf", str(rf)],
                         env=base_env(rf, ant, ring, arsenal),
                         stdout=lf, stderr=subprocess.STDOUT)
    time.sleep(secs)
    p.kill()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    lf.close()
    time.sleep(4)                      # SDR release

    text = Path(log_path).read_text(errors="ignore")
    mers = [20 * math.log10(5 / float(v))
            for v in RE_FS.findall(text) if float(v) > 0]
    rs = RE_RS.findall(text)
    pkts, bad = (int(rs[-1][0]), int(rs[-1][1])) if rs else (0, 0)
    tei = mb = 0
    if LIVE.exists():
        data = LIVE.read_bytes()
        mb = len(data) / 1e6
        for i in range(0, len(data) - 188, 188):
            if data[i] == 0x47 and data[i + 1] & 0x80:
                tei += 1
    out = {
        "ring": ring, "arsenal": arsenal,
        "mer_med": round(sorted(mers)[len(mers) // 2], 2) if mers else None,
        "mer_min": round(min(mers), 2) if mers else None,
        "n_mer": len(mers),
        "rs_pkts": pkts, "rs_bad": bad,
        "bad_pct": round(100 * bad / pkts, 3) if pkts else None,
        "tei_per_mb": round(tei / mb, 2) if mb else None,
        "ts_mb": round(mb, 1),
    }
    print(f"[arm ring={'ON ' if ring else 'OFF'}] {out}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=9)
    ap.add_argument("--ant", default="Antenna B")
    ap.add_argument("--secs", type=int, default=110)
    ap.add_argument("--arsenal-arm", action="store_true",
                    help="add a 5th arm: full cliff arsenal on this "
                         "channel (help/wash/hurt on strong signals?)")
    args = ap.parse_args()

    lab = HERE / "lab"
    arms = []
    # ABBA: ring ON, OFF, OFF, ON — cancels slow drift
    for i, ring in enumerate([True, False, False, True]):
        arms.append(run_arm(args.rf, args.ant, ring,
                            args.secs, lab / f"reg_{i}_{int(ring)}.log"))
    if args.arsenal_arm:
        arms.append(run_arm(args.rf, args.ant, True, args.secs,
                            lab / "reg_4_arsenal.log", arsenal=True))

    on = [a for a in arms if a["ring"]]
    off = [a for a in arms if not a["ring"]]

    def agg(rows, k):
        v = [r[k] for r in rows if r[k] is not None]
        return round(sum(v) / len(v), 3) if v else None

    print("\n=== VERDICT (ring ON vs OFF, ABBA x2) ===")
    for k in ("mer_med", "mer_min", "bad_pct", "tei_per_mb"):
        print(f"  {k:10s}: ON={agg(on, k)}  OFF={agg(off, k)}")


if __name__ == "__main__":
    main()
