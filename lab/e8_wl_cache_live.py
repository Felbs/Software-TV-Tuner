"""e8_wl_cache_live.py — LIVE validation of the WL warm-start tap cache.

The port was measured offline (13.3x faster to settled MER on a replayed
capture). Offline is not enough: the live path is where the previous cache bugs
lived — the `static` latch that stopped a persistent chain rebinding, and the
short-visit case that never wrote anything. So: three real visits to a real
channel off a real antenna.

  visit 1  cold  (cache dir emptied first)  -> must WRITE a cache
  visit 2  warm                             -> must print [eq-wl] WARM START
  visit 3  warm                             -> must still warm-start (idempotent)

Scored on the metric the topology actually supports. In tv_live the field sync
is found UPSTREAM of the equalizer, so an equalizer warm start cannot move
first_fs_seg; what it moves is how fast the equalizer converges. So we score
early-run fs_err_rms from the [eq-wl] telemetry (MER = 20*log10(5/err)) and
compare cold vs warm, exactly as the offline test did.

Runs under lab/exclusive_tv.py because the passive observatory daemons open the
SDR directly and would starve the decode.
"""
import os
import re
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path

PY = os.path.join(os.environ.get("USERPROFILE", ""), "radioconda", "python.exe")
REPO = Path(r"Z:\src\magic-tv-decoder")
OUT = REPO / "lab" / "night3" / "wl_cache_live"
TC = OUT / "tapcache"
RF = int(os.environ.get("E8_RF", "34"))
SECS = float(os.environ.get("E8_SECS", "45"))
ANT = os.environ.get("E8_ANT", "Antenna B")

MER = lambda e: 20 * math.log10(5.0 / e) if e > 0 else float("nan")


def visit(tag):
    ts = OUT / f"{tag}.ts"
    log = OUT / f"{tag}.log"
    env = dict(os.environ)
    env.update({
        "STVT_EQ": "wl",
        "STVT_FPLL_FOLD": "1",          # required by STVT_EQ=wl
        "STVT_EQ_TAP_CACHE": str(TC),
        "STVT_ANTENNA": ANT,
        "STVT_EQ_TELEM": "1",
        "STVT_EQ_TELEM_EVERY": "1",
        "STVT_VITERBI": "soft",
        "STVT_RS": "erasure",
        # bias-T OFF by default. The RSPdx bias-T lives on port B ONLY, and port
        # B currently carries "Old Faithful" — a PASSIVE TV yagi that needs no
        # phantom power and may be DC-shorted through its matching network.
        # This script shipped with STVT_BIAST=1 on 7/30 and duly powered it for
        # three runs before anyone noticed; the banked law is "never power it
        # blindly". Only set this when an ACTIVE antenna/LNA is on the selected
        # port, and set it deliberately per-run, never as a script default.
        "STVT_BIAST": os.environ.get("E8_BIAST", "0"),
        "STVT_NB": "0",
    })
    with open(log, "wb") as lf:
        p = subprocess.Popen(
            [PY, str(REPO / "tools" / "tv_live.py"), "--rf", str(RF),
             "--out", str(ts)],
            cwd=str(REPO), env=env, stdout=lf, stderr=subprocess.STDOUT)
        time.sleep(SECS)
        # SIGINT-preferred (banked law): CTRL_BREAK lets the flowgraph stop()
        # run, which is what persists the cache on a clean shutdown.
        try:
            p.send_signal(subprocess.signal.CTRL_BREAK_EVENT)
        except Exception:
            p.terminate()
        try:
            p.wait(timeout=40)
        except subprocess.TimeoutExpired:
            p.kill(); p.wait(timeout=20)
    return log, ts


def score(tag, log, ts):
    txt = log.read_text(errors="ignore")
    errs = [float(m) for m in
            re.findall(r"\[eq-wl t=[^\]]*\] fs=\d+ fs_err_rms=([0-9.]+)", txt)]
    warm = len(re.findall(r"\[eq-wl\] WARM START", txt))
    saved = len(re.findall(r"\[eq-wl\] cache persisted", txt))
    fs_seg = re.findall(r"first_fs_seg=(\d+)", txt)
    size = ts.stat().st_size / 1e6 if ts.exists() else 0.0
    print(f"\n-- {tag} --", flush=True)
    print(f"   WARM START lines={warm}  cache-persisted lines={saved}  "
          f"first_fs_seg={fs_seg[-1] if fs_seg else 'n/a'}  ts={size:.0f} MB  "
          f"field syncs={len(errs)}", flush=True)
    if errs:
        print(f"   first fs      MER {MER(errs[0]):6.2f} dB", flush=True)
        for n in (5, 20, 50):
            if len(errs) >= n:
                print(f"   mean first {n:<3} MER {MER(sum(errs[:n])/n):6.2f} dB",
                      flush=True)
        tail = errs[-50:]
        print(f"   settled       MER {MER(sum(tail)/len(tail)):6.2f} dB", flush=True)
    return {"tag": tag, "warm": warm, "saved": saved, "errs": errs, "mb": size}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    if TC.exists():
        shutil.rmtree(TC)          # guarantee visit 1 is genuinely COLD
    TC.mkdir(parents=True, exist_ok=True)
    print(f"=== E8 LIVE WL cache: RF{RF} {ANT}, {SECS:.0f}s per visit ===",
          flush=True)
    res = []
    for i, tag in enumerate(("v1_cold", "v2_warm", "v3_warm"), 1):
        log, ts = visit(tag)
        res.append(score(tag, log, ts))
        print(f"   cache dir now: "
              f"{[p.name for p in sorted(TC.iterdir())] or 'EMPTY'}", flush=True)
        if i < 3:
            time.sleep(8)          # let the SDRplay driver fully release
    print("\n=== VERDICT ===", flush=True)
    cold, warm = res[0], res[1]
    if cold["errs"] and warm["errs"]:
        for n in (5, 20):
            if len(cold["errs"]) >= n and len(warm["errs"]) >= n:
                c = MER(sum(cold["errs"][:n]) / n)
                w = MER(sum(warm["errs"][:n]) / n)
                print(f"first {n:<3} field syncs: cold {c:6.2f} dB -> "
                      f"warm {w:6.2f} dB   ({w-c:+.2f} dB)", flush=True)
    # Test the FILE, not the stop() log line. Measured 2026-07-30: CTRL_BREAK
    # does not get GNU Radio's stop() to run, so "cache persisted on stop" never
    # prints on Windows with our kill path — yet the cache is written anyway by
    # the periodic STVT_EQ_CACHE_EVERY tick. Scoring the log line reported
    # "cold wrote a cache: NO" while visit 2 was demonstrably warm-starting from
    # that very file. The artifact is the evidence; the log line is not.
    wrote = any(p.name.endswith(".wl") for p in TC.iterdir()) if TC.exists() else False
    print(f"cold wrote a cache: {'YES' if wrote else 'NO'}"
          f"   (via periodic persist; stop()-save lines seen: {cold['saved']})",
          flush=True)
    print(f"visit 2 warm-started: {'YES' if warm['warm'] else 'NO'}", flush=True)
    print(f"visit 3 warm-started: "
          f"{'YES' if res[2]['warm'] else 'NO'}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
