"""tune_antenna.py — ONE COMMAND that makes TV work on whatever antenna is
plugged in. The MER-era orchestrator: every decision is driven by the
equalizer's own fs_err_rms telemetry (= live MER), not by guesswork.

Pipeline:
  1. SWEEP    all UHF channels on the port: in-band shelf + raw level
  2. SURVEY   every carrier gets coarse MER cells -> classified:
                CLEAN          decodes, low impulse load
                IMPULSE        MER above cliff but rail transients corrupt it
                BELOW-CLIFF    locks, honest dB deficit reported
                PHANTOM        big shelf that never field-syncs = not ATSC
              Overload staircases (hot amp/LNA chains) are auto-rescued by
              extending the grid into deep attenuation (the rfgain=8 island).
  3. REFINE   fine MER cells around the best channel's best coarse cell
  4. JUDGE    real decoded quality (fps / v_err via quality_judge); if the
              margin is thin, A/B the cliff-edge recovery configs
  5. VERDICT  honest per-channel report + antenna profile JSON saved to
              profiles/, reusable with --quick until the RF path changes

Usage:
    python tune_antenna.py                          # full auto on Antenna A
    python tune_antenna.py --antenna "Antenna B" --biast   # powered LNA port
    python tune_antenna.py --name attic-bare        # label the saved profile
    python tune_antenna.py --play                   # launch TV when done
"""
import argparse, json, math, os, re, subprocess, sys, time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from mer_gain_cal import run_cell, CLIFF_DB, PY, TV_LIVE, LIVE, SDRPLAY_DLL

JUDGE = HERE / "quality_judge.py"
PROFILES = HERE / "profiles"
FFMPEG = r"C:\ffmpeg\bin"
LOG = Path(os.environ.get("TEMP", ".")) / "tune_antenna.log"

RE_SCORE = re.compile(r"score=(\d+).*?fps=([\d.]+) v_err=([\d.]+)/s")

CH = {rf: 473 + (rf - 14) * 6 for rf in range(14, 37)}

COARSE = [(2, 32), (3, 40), (4, 48)]            # spans hot -> quiet
DEEP   = [(5, 40), (6, 44), (7, 44), (8, 40), (8, 48), (9, 44)]  # LNA island
CARRIER_SHELF = 5.0       # sweep shelf dB to count as a carrier
PHANTOM_SHELF = 10.0      # big shelf + zero syncs anywhere = not ATSC
IMPULSE_RAILS = 8         # avg rails at/above this while MER>cliff = impulse
GOOD_SCORE = 60           # judge score that ends the search happy
QUIET_VERR = 15.0         # v_err/s at/below this = visibly glitch-free; a
                          # score of 100 with 25 v_err/s still shows the
                          # occasional artifact burst, so keep hunting

# cliff-edge recovery configs worth A/B-ing when margin is thin
RESCUE_CONFIGS = [
    ("erasure+quality-reset", {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
                               "STVT_EQ_QUALITY_BAD_RMS": "8"}),
    ("erasure20",             {"STVT_RS": "erasure", "STVT_RS_ERASURES": "20"}),
]


# ── stage 1: sweep ─────────────────────────────────────────────────
def sweep(antenna, biast=False, ifgr=40, rfgain=3):
    import numpy as np
    import SoapySDR
    SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
    from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
    RATE, FFT = 8_000_000, 4096
    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, RATE)
    try: sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception: pass
    sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", float(ifgr))
    try: sdr.writeSetting("rfgain_sel", str(rfgain))
    except Exception: pass
    sdr.setAntenna(SOAPY_SDR_RX, 0, antenna)
    try: sdr.writeSetting("biasT_ctrl", "true" if biast else "false")
    except Exception: pass
    if biast: time.sleep(1.0)
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32); sdr.activateStream(st)
    buf = np.empty(FFT, dtype=np.complex64)
    win = np.hanning(FFT).astype(np.float32)
    rows = {}
    for rf in sorted(CH):
        sdr.setFrequency(SOAPY_SDR_RX, 0, CH[rf] * 1e6)
        time.sleep(0.15)
        acc = np.zeros(FFT); n = 0
        t0 = time.time()
        while time.time() - t0 < 0.8:
            sr = sdr.readStream(st, [buf], FFT, timeoutUs=int(0.4e6))
            if sr.ret < FFT: continue
            acc += np.abs(np.fft.fftshift(np.fft.fft(buf * win))) ** 2; n += 1
        if n == 0: continue
        psd = acc / n
        bh = RATE / FFT; dc = FFT // 2; dh = int(100_000 / bh)
        lo, hi = dc - int(3e6 / bh), dc + int(3e6 / bh)
        m = np.ones(FFT, bool); m[dc - dh:dc + dh] = False
        shelf = 10 * np.log10(np.mean(psd[lo:hi][m[lo:hi]]) /
                              (np.mean(np.concatenate([psd[:lo], psd[hi:]])) + 1e-20) + 1e-20)
        rows[rf] = shelf
    sdr.deactivateStream(st); sdr.closeStream(st)
    del sdr           # release the device — the chain needs it next
    time.sleep(1.0)
    return rows


# ── stage 4 helper: judge real decoded quality at one setting ──────
def judge(rf, antenna, rfsel, ifgr, extra=None, biast=False, program=1,
          settle=22, window=15):
    env = os.environ.copy()
    env["PATH"] = SDRPLAY_DLL + os.pathsep + FFMPEG + os.pathsep + env.get("PATH", "")
    if biast: env["STVT_BIAST"] = "1"
    env.update({
        "STVT_ANTENNA": antenna, "STVT_IFGR": str(ifgr),
        "STVT_RFGAIN_SEL": str(rfsel), "STVT_EQ": "long",
        "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
        # DAB band III = US VHF-hi; never notch RF7-13 (2026-07-04 law)
        "STVT_DABNOTCH": "0" if rf < 14 else "1",
        "STVT_RS": "stock", "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8",
        "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
        "STVT_EQ_TELEM": "1",
    })
    if extra: env.update(extra)
    if LIVE.exists():
        try: LIVE.unlink()
        except OSError: pass
    with open(LOG, "w") as lf:
        ch = subprocess.Popen([PY, "-u", str(TV_LIVE), "--rf", str(rf)],
                              env=env, stdout=lf, stderr=subprocess.STDOUT)
        time.sleep(settle)
        try:
            out = subprocess.run([PY, str(JUDGE), "--program", str(program),
                                  "--window", str(window)],
                                 env=env, capture_output=True, text=True,
                                 timeout=window + 30).stdout
        except Exception:
            out = ""
        ch.terminate()
        try: ch.wait(timeout=6)
        except Exception: ch.kill()
    m = RE_SCORE.search(out)
    if not m: return 0, 0.0, 999.0
    return int(m.group(1)), float(m.group(2)), float(m.group(3))


# ── stage 2: survey one channel with coarse cells ──────────────────
def survey_channel(rf, antenna, shelf, biast, secs):
    cells = []
    for rfsel, ifgr in COARSE:
        mer, irms, rail, hdrs = run_cell(rf, antenna, ifgr, rfsel, secs, biast)
        cells.append({"rfgain": rfsel, "ifgr": ifgr, "mer": round(mer, 2),
                      "in_rms": round(irms, 1), "rails": rail, "hdrs": hdrs})
        print(f"      {rfsel}:{ifgr}  MER {mer:5.2f}  rails {rail:>2}  hdrs {hdrs}")
        time.sleep(2)
    best = max(cells, key=lambda c: c["mer"])
    # OVERLOAD STAIRCASE: MER improves monotonically toward the low-gain edge
    # and still rails there -> the sweet spot is beyond the coarse grid (hot
    # amp or wideband LNA). Extend into deep attenuation until it peaks.
    mers = [c["mer"] for c in cells]
    if best is cells[-1] and mers == sorted(mers) and (best["rails"] > 3 or
                                                       best["mer"] < CLIFF_DB):
        print(f"      overload staircase -> extending grid into deep attenuation")
        for rfsel, ifgr in DEEP:
            mer, irms, rail, hdrs = run_cell(rf, antenna, ifgr, rfsel, secs, biast)
            cells.append({"rfgain": rfsel, "ifgr": ifgr, "mer": round(mer, 2),
                          "in_rms": round(irms, 1), "rails": rail, "hdrs": hdrs})
            print(f"      {rfsel}:{ifgr}  MER {mer:5.2f}  rails {rail:>2}  hdrs {hdrs}")
            time.sleep(2)
        best = max(cells, key=lambda c: c["mer"])
    locked = [c for c in cells if c["mer"] > 0]
    if not locked:
        klass = "PHANTOM" if shelf >= PHANTOM_SHELF else "NO-LOCK"
    elif best["mer"] < CLIFF_DB:
        klass = "BELOW-CLIFF"
    else:
        above = [c for c in cells if c["mer"] >= CLIFF_DB]
        avg_rails = sum(c["rails"] for c in above) / len(above)
        klass = "IMPULSE" if avg_rails >= IMPULSE_RAILS else "CLEAN"
    return {"rf": rf, "shelf": round(shelf, 1), "class": klass,
            "best": best, "cells": cells}


# ── stage 3: refine around the winning coarse cell ─────────────────
def refine(rf, antenna, best, biast, secs):
    r0, i0 = best["rfgain"], best["ifgr"]
    seen = {(c, i) for c, i in [(r0, i0)]}
    cand = [(r0, i0 - 4), (r0, i0 + 4), (max(0, r0 - 1), i0), (r0 + 1, i0)]
    cells = [dict(best)]
    for rfsel, ifgr in cand:
        if (rfsel, ifgr) in seen or not (20 <= ifgr <= 59): continue
        seen.add((rfsel, ifgr))
        mer, irms, rail, hdrs = run_cell(rf, antenna, ifgr, rfsel, secs, biast)
        cells.append({"rfgain": rfsel, "ifgr": ifgr, "mer": round(mer, 2),
                      "in_rms": round(irms, 1), "rails": rail, "hdrs": hdrs})
        print(f"      {rfsel}:{ifgr}  MER {mer:5.2f}  rails {rail:>2}  hdrs {hdrs}")
        time.sleep(2)
    # prefer highest MER; break near-ties (<0.3 dB) toward fewer rails
    cells.sort(key=lambda c: (-c["mer"], c["rails"]))
    top = cells[0]
    for c in cells[1:]:
        if top["mer"] - c["mer"] < 0.3 and c["rails"] < top["rails"]:
            top = c
    return top


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--biast", action="store_true", help="power a bias-T LNA")
    ap.add_argument("--name", default="", help="profile name (default: port)")
    ap.add_argument("--secs", type=int, default=14, help="seconds per MER cell")
    ap.add_argument("--top", type=int, default=6, help="carriers to survey")
    ap.add_argument("--play", action="store_true", help="launch TV at the end")
    args = ap.parse_args()
    name = args.name or args.antenna.replace(" ", "_")
    t_start = time.time()

    print("=" * 66)
    print(f"  TUNE ANTENNA  [{name}]  port={args.antenna}  biast={args.biast}")
    print("=" * 66)

    # 1 — sweep
    print("\n  [1/5] channel sweep...")
    shelves = sweep(args.antenna, args.biast)
    carriers = sorted(((rf, s) for rf, s in shelves.items() if s >= CARRIER_SHELF),
                      key=lambda t: -t[1])[:args.top]
    for rf, s in carriers:
        print(f"    RF{rf:>2} ({CH[rf]} MHz)  shelf {s:+.1f} dB")
    if not carriers:
        print("    NO carriers >= +5 dB. Verdict: nothing to decode on this "
              "port — check antenna connection / try another port.")
        return

    # 2 — survey
    print(f"\n  [2/5] MER survey ({len(carriers)} carriers x coarse cells)...")
    surveyed = []
    for rf, s in carriers:
        print(f"    RF{rf}:")
        surveyed.append(survey_channel(rf, args.antenna, s, args.biast, args.secs))

    ranked = sorted((s for s in surveyed if s["class"] in ("CLEAN", "IMPULSE")),
                    key=lambda s: (s["class"] != "CLEAN", -s["best"]["mer"]))
    below = [s for s in surveyed if s["class"] == "BELOW-CLIFF"]

    if not ranked:
        print("\n  [verdict] no channel above the 15.2 dB cliff on this antenna.")
        for s in below:
            print(f"    RF{s['rf']}: best MER {s['best']['mer']} "
                  f"({s['best']['mer']-CLIFF_DB:+.1f} dB vs cliff) — "
                  "aim/reposition/bigger antenna to bridge the gap")
        _save_profile(name, args, shelves, surveyed, None, t_start)
        return

    # 3 — refine best channel
    champ = ranked[0]
    print(f"\n  [3/5] refine RF{champ['rf']} around "
          f"{champ['best']['rfgain']}:{champ['best']['ifgr']}...")
    top = refine(champ["rf"], args.antenna, champ["best"], args.biast, args.secs)
    print(f"    -> rfgain={top['rfgain']} IFGR={top['ifgr']}  "
          f"MER {top['mer']} ({top['mer']-CLIFF_DB:+.2f} vs cliff)")

    # 4 — judge real quality; rescue configs if thin; fall through channels
    print(f"\n  [4/5] quality judge...")
    winner = None
    for s in ranked:
        g = top if s is champ else s["best"]
        best_cfg = "baseline"
        score, fps, verr = judge(s["rf"], args.antenna, g["rfgain"], g["ifgr"],
                                 {}, args.biast)
        print(f"    RF{s['rf']} @ {g['rfgain']}:{g['ifgr']} [baseline]  "
              f"score {score}  fps {fps:.1f}  v_err {verr:.1f}/s")
        # rescue configs when the score is low OR the picture is technically
        # full-rate but still bursty (erasure RS eats impulse bursts)
        if score < GOOD_SCORE or verr > QUIET_VERR:
            for cfg_name, cfg in RESCUE_CONFIGS:
                s2, f2, v2 = judge(s["rf"], args.antenna, g["rfgain"], g["ifgr"],
                                   cfg, args.biast)
                print(f"    RF{s['rf']} @ {g['rfgain']}:{g['ifgr']} [{cfg_name}]  "
                      f"score {s2}  fps {f2:.1f}  v_err {v2:.1f}/s")
                if (s2, -v2) > (score, -verr):
                    score, fps, verr, best_cfg = s2, f2, v2, cfg_name
                if score >= GOOD_SCORE and verr <= QUIET_VERR: break
        s["judge"] = {"score": score, "fps": fps, "v_err": verr,
                      "config": best_cfg, "gain": g}
        if winner is None or (score, -verr) > (winner["judge"]["score"],
                                               -winner["judge"]["v_err"]):
            winner = s
        # stop early only on a QUIET win; a bursty 100 keeps the search alive
        if score >= GOOD_SCORE and verr <= QUIET_VERR:
            break

    # 5 — verdict + profile
    print(f"\n  [5/5] VERDICT for [{name}]")
    for s in surveyed:
        extra = ""
        if s.get("judge"):
            extra = (f"  quality {s['judge']['score']}/100 @ "
                     f"{s['judge']['gain']['rfgain']}:{s['judge']['gain']['ifgr']}"
                     f" [{s['judge']['config']}]")
        elif s["class"] == "BELOW-CLIFF":
            extra = f"  ({s['best']['mer']-CLIFF_DB:+.1f} dB vs cliff)"
        print(f"    RF{s['rf']:>2}  shelf {s['shelf']:+5.1f}  "
              f"MER {s['best']['mer']:5.2f}  {s['class']:<11}{extra}")
    w = winner["judge"]
    q = ("CABLE-QUALITY" if w["score"] >= 90 else
         "WATCHABLE" if w["score"] >= GOOD_SCORE else
         "GLITCHY" if w["score"] >= 20 else "BROKEN")
    print(f"\n    WINNER: RF{winner['rf']}  rfgain={w['gain']['rfgain']} "
          f"IFGR={w['gain']['ifgr']}  config={w['config']}  "
          f"score {w['score']}/100 -> {q}")
    if q in ("GLITCHY", "BROKEN"):
        print("    (software is maxed — the rest is physical: aim, height, "
              "position, or a bigger antenna)")
    prof = _save_profile(name, args, shelves, surveyed, winner, t_start)
    print(f"    profile saved: {prof}")

    if args.play and winner:
        cmd = [PY, str(HERE / "mer_meter.py"), "--rf", str(winner["rf"]),
               "--rfgain", str(w["gain"]["rfgain"]), "--ifgr", str(w["gain"]["ifgr"]),
               "--antenna", args.antenna]
        print(f"\n  handing off to playback (mer_meter dashboard)...")
        os.execv(PY, cmd)


def _save_profile(name, args, shelves, surveyed, winner, t_start):
    PROFILES.mkdir(exist_ok=True)
    out = PROFILES / f"{name}.json"
    out.write_text(json.dumps({
        "name": name, "antenna": args.antenna, "biast": args.biast,
        "when": time.strftime("%Y-%m-%d %H:%M"),
        "took_s": round(time.time() - t_start),
        "shelves": {str(k): round(v, 1) for k, v in shelves.items()},
        "channels": surveyed,
        "winner": None if winner is None else {
            "rf": winner["rf"], **winner["judge"]},
    }, indent=2))
    return out


if __name__ == "__main__":
    main()
