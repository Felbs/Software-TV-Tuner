"""scan_gate_study.py — prove (and tune) the two-stage scan on the 35 real
OTA fixtures, offline.

Question 1 (correctness): does the TWO-STAGE sweep — 2.05 ms stage A, then
the unchanged full-dwell detector wherever the prescreen fires — reach the
SAME verdict as today's single-stage 100 ms sweep on every fixture, for
every gate `tv_tuner.run_scan` applies?

  strict  pilot_snr >= 30.0  and sharp >= 26.25 and vsb >= 2.4
  rescue  (not strict) and pilot_snr >= 30.0 and sharp >= 18.0
  weak    (not hot) and pilot_snr >= 15.0 and sharp >= 8.0 and vsb >= -14.0
  atsc3   (not hot, not weak) and atsc3_db >= 10.0 and rms >= floor+4 and rf>=14

Question 2 (tuning): which prescreen thresholds keep all of that while
paying the full dwell on as few frequencies as possible?

Calls the PRODUCTION detector (`sdr_sweep._analyze`) and the PRODUCTION
prescreen (`sdr_sweep.prescreen`). Read-only, no SDR. radioconda python.
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import sdr_sweep                                          # noqa: E402

FIX = REPO / "tools" / "scan_lab" / "fixtures"
FS = 8_000_000

T_SNR, T_SHARP, T_VSB = 30.0, 26.25, 2.4
W_SNR, W_SHARP, W_VSB = 15.0, 8.0, -14.0
RESCUE_SHARP = 18.0
RMS_MARGIN = 4.0

DWELLS = [("fastA", 16_384), ("prod100", 800_000), ("full200", 1_600_000)]
SETTLE = 0.040


def verdicts(m: dict, rf: int, rms_floor: float) -> dict:
    snr = m.get("pilot_snr_db", float("-inf"))
    sh = m.get("pilot_sharpness_db", float("-inf"))
    vs = m.get("vsb_asymmetry_db", float("-inf"))
    a3 = m.get("atsc3_db", float("-inf"))
    rms = m.get("rms_dbfs", float("-inf"))
    strict = snr >= T_SNR and sh >= T_SHARP and vs >= T_VSB
    rescue = (not strict) and snr >= T_SNR and sh >= RESCUE_SHARP
    hot = strict or rescue
    weak = (not hot) and snr >= W_SNR and sh >= W_SHARP and vs >= W_VSB
    atsc3 = ((not hot) and (not weak) and a3 >= 10.0
             and rms >= rms_floor + RMS_MARGIN and rf is not None and rf >= 14)
    return {"hot": hot, "weak": weak, "atsc3": atsc3}


def margin(m: dict) -> float:
    """dB of slack on the tightest of the three strict criteria."""
    return min(m.get("pilot_snr_db", -999) - T_SNR,
               m.get("pilot_sharpness_db", -999) - T_SHARP,
               m.get("vsb_asymmetry_db", -999) - T_VSB)


def load():
    man = json.load(open(FIX / "manifest.json"))
    met = {}
    for rf, c in sorted(man["captures"].items(), key=lambda kv: int(kv[0])):
        x = np.fromfile(FIX / c["file"], dtype=np.complex64)
        met[int(rf)] = {name: sdr_sweep._analyze(x[:n], FS, "atsc")
                        for name, n in DWELLS if x.size >= n}
    floors = {}
    for name, _n in DWELLS:
        vals = sorted(m[name]["rms_dbfs"] for m in met.values()
                      if name in m and m[name]["rms_dbfs"] > -150)
        floors[name] = vals[len(vals) // 2] if vals else -999.0
    return met, floors


def two_stage(met, floors, snr_bar, sharp_bar):
    """Simulate the shipped two-stage sweep. Returns (verdict per rf,
    confirmed rf list)."""
    out, confirmed = {}, []
    for rf, m in met.items():
        a = m["fastA"]
        if sdr_sweep.prescreen(a, snr_bar, sharp_bar):
            confirmed.append(rf)
            out[rf] = verdicts(m["prod100"], rf, floors["prod100"])
        else:
            out[rf] = verdicts(a, rf, floors["fastA"])
    return out, confirmed


def main():
    met, floors = load()
    n = len(met)
    print(f"pilot offset in use: {sdr_sweep.PILOT_OFFSET_HZ:+.3f} Hz")
    print(f"shipped prescreen  : snr >= {sdr_sweep.PRESCREEN_SNR_DB} dB, "
          f"sharp >= {sdr_sweep.PRESCREEN_SHARP_DB} dB")
    print("rms floor per dwell: " +
          "  ".join(f"{k}={v:+.2f}" for k, v in floors.items()) + "\n")

    truth = {rf: verdicts(met[rf]["prod100"], rf, floors["prod100"])
             for rf in met}
    print("=== PRODUCTION (single-stage 100 ms) = the thing we must not change")
    for k in ("hot", "weak", "atsc3"):
        print(f"  {k:<6}:", sorted(rf for rf in truth if truth[rf][k]))
    print()

    # ── Q2: prescreen tuning sweep ──────────────────────────────────────
    print("=== prescreen sweep: confirms & verdict mismatches vs production")
    print(f"{'snr':>5} {'sharp':>6} {'confirm':>8} {'radio_s':>8} {'speedup':>8} "
          f"{'hotFN':>6} {'hotFP':>6} {'weakX':>6} {'a3X':>4}")
    legacy_s = n * (SETTLE + 0.10)
    best = None
    for snr_bar, sharp_bar in itertools.product(
            [10, 12, 13, 14, 15], [3, 4, 4.5, 5, 5.5, 6, 6.5, 7]):
        v, conf = two_stage(met, floors, snr_bar, sharp_bar)
        hot_fn = sum(1 for rf in met if truth[rf]["hot"] and not v[rf]["hot"])
        hot_fp = sum(1 for rf in met if v[rf]["hot"] and not truth[rf]["hot"])
        weak_x = sum(1 for rf in met if v[rf]["weak"] != truth[rf]["weak"])
        a3_x = sum(1 for rf in met if v[rf]["atsc3"] != truth[rf]["atsc3"])
        radio = n * (SETTLE + 0.00205) + len(conf) * 0.10
        clean = (hot_fn == 0 and hot_fp == 0 and weak_x == 0 and a3_x == 0)
        if clean and (best is None or len(conf) < best[2]):
            best = (snr_bar, sharp_bar, len(conf), radio)
        print(f"{snr_bar:>5} {sharp_bar:>6} {len(conf):>8} {radio:>8.2f} "
              f"{legacy_s/radio:>7.2f}x {hot_fn:>6} {hot_fp:>6} {weak_x:>6} "
              f"{a3_x:>4}{'   <- clean' if clean else ''}")
    if best:
        print(f"\nTIGHTEST PRESCREEN WITH ZERO VERDICT CHANGES: "
              f"snr >= {best[0]}, sharp >= {best[1]} "
              f"-> {best[2]}/{n} confirmed, {best[3]:.2f}s radio "
              f"({legacy_s/best[3]:.2f}x vs {legacy_s:.2f}s)")
    else:
        print("\nNO prescreen setting preserves every verdict — "
              "stage A must not gate the weak/atsc3 classification.")

    # ── Q1: the shipped setting, per fixture ────────────────────────────
    v, conf = two_stage(met, floors, None, None)
    print(f"\n=== per fixture, SHIPPED two-stage "
          f"({sdr_sweep.PRESCREEN_SNR_DB}/{sdr_sweep.PRESCREEN_SHARP_DB}) ===")
    print(f"{'RF':>3} {'A:snr':>7} {'A:shrp':>7} {'A:vsb':>7} {'A:marg':>7} "
          f"{'pre':>4} | {'P:snr':>7} {'P:shrp':>7} {'P:marg':>7} "
          f"| {'2s.hot':>6} {'pr.hot':>6} {'2s.wk':>6} {'pr.wk':>6} verdict")
    tp = fp = fn = tn = 0
    hot_marg, empty_marg, mism = [], [], []
    for rf in sorted(met):
        a, p = met[rf]["fastA"], met[rf]["prod100"]
        pre = rf in conf
        if truth[rf]["hot"] and v[rf]["hot"]:
            tp += 1
        elif truth[rf]["hot"]:
            fn += 1
        elif v[rf]["hot"]:
            fp += 1
        else:
            tn += 1
        bad = [k for k in ("hot", "weak", "atsc3") if v[rf][k] != truth[rf][k]]
        if bad:
            mism.append((rf, bad))
        (hot_marg if truth[rf]["hot"] else empty_marg).append(margin(a))
        print(f"{rf:>3} {a['pilot_snr_db']:7.1f} {a['pilot_sharpness_db']:7.1f} "
              f"{a['vsb_asymmetry_db']:7.1f} {margin(a):7.2f} "
              f"{'Y' if pre else '.':>4} | {p['pilot_snr_db']:7.1f} "
              f"{p['pilot_sharpness_db']:7.1f} {margin(p):7.2f} "
              f"| {str(v[rf]['hot']):>6} {str(truth[rf]['hot']):>6} "
              f"{str(v[rf]['weak']):>6} {str(truth[rf]['weak']):>6} "
              f"{'MISMATCH:' + ','.join(bad) if bad else 'ok'}")

    print(f"\nHOT (= the phase-2 channel list): TP={tp} FP={fp} FN={fn} TN={tn}")
    print(f"stage-A worst-case strict margin, HOT channels : "
          f"{min(hot_marg):+.2f} dB")
    print(f"stage-A closest strict margin, EMPTY channels  : "
          f"{max(empty_marg):+.2f} dB")
    print(f"confirmed {len(conf)}/{n}: {sorted(conf)}")
    print("VERDICT MISMATCHES:", mism if mism else "NONE (hot/weak/atsc3 all match)")

    radio = n * (SETTLE + 0.00205) + len(conf) * 0.10
    print(f"\nradio time, {n} freqs, settle {SETTLE*1000:.0f} ms:"
          f"\n  single-stage 100 ms : {legacy_s:6.2f} s"
          f"\n  two-stage shipped   : {radio:6.2f} s  ({legacy_s/radio:.2f}x)")

    # ── stage-A vs 200 ms, the dossier's own table, for the record ──────
    print("\n=== stage-A (2.05 ms) vs full 200 ms, strict-gate only "
          "(dossier §2.2 reproduction) ===")
    for name in ("fastA", "prod100", "full200"):
        hot = sorted(rf for rf in met
                     if verdicts(met[rf][name], rf, floors[name])["hot"])
        mg = [margin(met[rf][name]) for rf in hot]
        print(f"  {name:<8} hot={hot} worst_margin={min(mg):+.2f} dB")


if __name__ == "__main__":
    main()
