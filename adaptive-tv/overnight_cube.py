"""overnight_cube.py — the Antenna x Channel x Time cube.

Both antennas are wired (ANT-B rabbit ears, ANT-A discone) and the RSPdx
switches ports in SOFTWARE — so tonight the tuner itself does the antenna
swapping no human ever wants to do. All night, every ~20 min:

    for each antenna port:
        for each RF channel in the roster:
            28 s chain sample -> median MER, seq-headers, verdict

This produces the empirical map "which antenna, which channel, which hour"
— the data spine of the ultimate goal (decode TV on ANY antenna: measure
the antenna, don't trust it). Every 3rd cycle it re-trims gain on the most
promising below-cliff pair (recalibrate-forever law, in miniature).

Sentry duty is folded in: RF7/RF9 at MER >= 15.5 on either antenna
triggers a voice announce (the dawn tropo window is 4-8 AM).

Runs until --until (default 07:15). Log: cube_log.jsonl (one JSON/sample).
"""
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PY = r"C:\Users\user\radioconda\python.exe"
TV_LIVE = Path(r"Z:\src\magic-tv-decoder\tools\tv_live.py")
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
SDRPLAY_DLL = r"C:\Program Files\SDRplay\API\x64"
HERE = Path(__file__).parent
LOG_JL = HERE / "cube_log.jsonl"
STATE_F = HERE / "cube_state.json"
CHAINLOG = HERE / "cube_chain.log"

CLIFF = 15.2
# 2026-07-07 three-antenna era: Philips on A (UHF star), rabbit ears on
# B (all-round), discone on C (sub-200 MHz port: FM oracle + VHF-hi
# retest — RF7/9 are inside C's range; the old 0/35 verdict predates
# every current recipe). Each antenna samples only channels it can
# physically serve.
# THE THREE-ANTENNA ERA (2026-07-07, found connector): Philips on B
# (reigning TV champion — WETA conqueror), rabbit ears on A (Baltimore
# specialist, position un-aimed since the attic move), discone on C
# (FM oracle; VHF-hi only for TV).
ANTS = [("Antenna B", "philips", [7, 9, 15, 21, 34, 36]),
        ("Antenna A", "rabbit", [7, 9, 15, 21, 34, 36]),
        ("Antenna C", "discone", [7, 9])]
ROSTER = [7, 9, 15, 21, 34, 36]
# (rfgain_sel, ifgr) starting points; state file evolves these per (rf, ant)
GAINS = {7: (5, 32), 9: (5, 32), 15: (1, 32), 21: (2, 32),
         34: (2, 32), 36: (3, 40)}

RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
RE_MX = re.compile(r"mean\|x\|=([\d.]+)")
RE_RS = re.compile(r"\[rs_erasure t=.*?pkts=(\d+) ec=(\d+) "
                   r"era_dec=\d+ era_ok=\d+ miscorr=\d+ bad=(\d+) "
                   r"\(last5s: pkts=(\d+) era_dec=\d+ era_ok=\d+ bad=(\d+)"
                   r"(?: sync=\d+)?\)")
RE_VIT = re.compile(r"vit_metric=([\d.]+) vit_max=([\d.]+)")

SAMPLE_SECS = 28
SETTLE_SECS = 9          # ignore telemetry before the EQ settles
CYCLE_MIN = 20


def mer_db(err):
    return 20.0 * math.log10(5.0 / err) if err > 0 else 20.0


def chain_env(rf, antenna, rfg, ifgr):
    e = os.environ.copy()
    e["PATH"] = SDRPLAY_DLL + os.pathsep + e.get("PATH", "")
    e.update({
        "STVT_ANTENNA": antenna, "STVT_IFGR": str(ifgr),
        "STVT_RFGAIN_SEL": str(rfg), "STVT_EQ": "long",
        "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
        "STVT_DABNOTCH": "0" if rf < 14 else "1",   # VHF law: notch OFF
        # E4 (2026-07-06): erasure RS with 0 erasures = stock behavior +
        # full FEC telemetry (pkts/corrected/bad + viterbi metric) — the
        # previously-dark 98% of the disease map, now narrated.
        "STVT_RS": "erasure", "STVT_RS_ERASURES": "0",
        "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8",
        "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
        "STVT_EQ_TELEM": "1", "STVT_FPLL_FOLD": "1",
        # Glitch Specimen Recorder (2026-07-06): 8 s IQ ring in every
        # sample; specimen_watch.py pulls the trigger on RS storms.
        "STVT_IQ_RING": "8",
    })
    return e


def sample(rf, antenna, rfg, ifgr, secs=SAMPLE_SECS):
    """One chain run -> dict of MER stats + header count."""
    if LIVE.exists():
        try:
            LIVE.unlink()
        except OSError:
            pass
    lf = open(CHAINLOG, "w")
    ch = subprocess.Popen([PY, "-u", str(TV_LIVE), "--rf", str(rf)],
                          env=chain_env(rf, antenna, rfg, ifgr),
                          stdout=lf, stderr=subprocess.STDOUT)
    t0 = time.time()
    mers = []
    mx = 0.0
    off = 0
    rs_last = vit_last = None
    deaf_fired = False
    while time.time() - t0 < secs and ch.poll() is None:
        time.sleep(1.0)
        try:
            with open(CHAINLOG, "r", errors="ignore") as f:
                f.seek(off)
                chunk = f.read()
                off = f.tell()
        except OSError:
            chunk = ""
        if time.time() - t0 >= SETTLE_SECS:
            mers += [mer_db(float(m)) for m in RE_FS.findall(chunk)]
        m = RE_MX.findall(chunk)
        if m:
            mx = float(m[-1])
        r = RE_RS.findall(chunk)
        if r:
            rs_last = r[-1]
        v = RE_VIT.findall(chunk)
        if v:
            vit_last = v[-1]
        # DEAF trigger (2026-07-06, RF15 @ MER 17.41 with 99.9% RS fail):
        # pristine symbols + total FEC death = the eq->RS alignment bug
        # caught alive. Dump the ring NOW, while this chain still runs.
        if (not deaf_fired and time.time() - t0 > 20 and len(mers) > 30
                and rs_last and int(rs_last[3]) > 20000
                and int(rs_last[4]) / max(1, int(rs_last[3])) > 0.98):
            ms = sorted(mers)
            if ms[len(ms) // 2] >= 16.0:
                trig = Path(r"Z:\src\magic-tv-decoder\tools\data"
                            r"\specimens\TRIGGER")
                trig.parent.mkdir(parents=True, exist_ok=True)
                trig.write_text(
                    f"DEAF rf{rf} {antenna} mer_med={ms[len(ms)//2]:.2f} "
                    f"rs5_bad={rs_last[4]}/{rs_last[3]}")
                deaf_fired = True
    ch.terminate()
    try:
        ch.wait(timeout=6)
    except Exception:
        ch.kill()
    lf.close()
    hdr = 0
    if LIVE.exists():
        try:
            hdr = LIVE.read_bytes().count(b"\x00\x00\x01\xb3")
        except OSError:
            pass
    mers.sort()
    n = len(mers)
    out = {
        "fs_n": n,
        "mer_med": round(mers[n // 2], 2) if n else None,
        "mer_p90": round(mers[int(n * 0.9)], 2) if n else None,
        "mean_x": round(mx, 4),
        "hdr": hdr,
    }
    if rs_last:
        out.update({"rs_pkts": int(rs_last[0]), "rs_ec": int(rs_last[1]),
                    "rs_bad": int(rs_last[2]),
                    "rs5_pkts": int(rs_last[3]), "rs5_bad": int(rs_last[4])})
    if vit_last:
        out.update({"vit": float(vit_last[0]), "vit_max": float(vit_last[1])})
    return out


def verdict(s):
    if s["fs_n"] == 0:
        return "NO-FS" if s["mean_x"] < 0.02 else "PILOT-ONLY"
    if s["hdr"] > 3 and (s["mer_med"] or 0) >= CLIFF - 0.5:
        return "DECODE"
    if (s["mer_med"] or 0) >= CLIFF:
        return "AT-CLIFF"
    if (s["mer_med"] or 0) >= CLIFF - 3:
        return "CLOSE"
    return "FLOOR"


def announce(text):
    ps = ("Add-Type -AssemblyName System.Speech; "
          "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$s.Rate=-1; $s.Speak('" + text.replace("'", "") + "')")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps])
    except Exception:
        pass


def log(obj):
    obj["t"] = datetime.now().strftime("%H:%M:%S")
    with open(LOG_JL, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")
    print(obj["t"], obj.get("rf"), obj.get("ant"), obj.get("verdict", ""),
          "MER", obj.get("mer_med"), "hdr", obj.get("hdr"), flush=True)


def load_state():
    try:
        return json.loads(STATE_F.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(st):
    STATE_F.write_text(json.dumps(st, indent=1))


def main():
    until_s = sys.argv[1] if len(sys.argv) > 1 else "07:15"
    hh, mm = map(int, until_s.split(":"))
    now = datetime.now()
    until = now.replace(hour=hh, minute=mm, second=0)
    if until <= now:
        until += timedelta(days=1)
    state = load_state()          # {"rf7|rabbit": [rfg, ifgr], ...}
    print(f"cube: running until {until}, roster {ROSTER}, "
          f"ants {[a[1] for a in ANTS]}", flush=True)
    log({"event": "start", "until": str(until)})
    cyc = 0
    nofs_streak = 0
    while datetime.now() < until:
        cyc += 1
        cycle_t0 = time.time()
        best_close = None         # most promising below-cliff pair
        for antenna, ant, rfs in ANTS:
            for rf in rfs:
                if datetime.now() >= until:
                    break
                key = f"rf{rf}|{ant}"
                rfg, ifgr = state.get(key, GAINS[rf])
                s = sample(rf, antenna, rfg, ifgr)
                s.update({"cyc": cyc, "rf": rf, "ant": ant,
                          "rfg": rfg, "ifgr": ifgr})
                s["verdict"] = verdict(s)
                log(s)
                # SDR wedge guard: every sample dead in a whole pass = wedged
                nofs_streak = nofs_streak + 1 if s["verdict"] == "NO-FS" else 0
                if nofs_streak >= len(ROSTER) * 2:
                    log({"event": "SDR-SUSPECT",
                         "note": "all-dead pass; sleeping 8 min"})
                    announce("S D R may be wedged")
                    nofs_streak = 0
                    time.sleep(480)
                m = s["mer_med"] or -99
                if s["verdict"] in ("CLOSE", "AT-CLIFF") and (
                        best_close is None or m > best_close[0]):
                    best_close = (m, rf, antenna, ant, rfg, ifgr)
                # sentry contract: dawn tropo on the VHF pair
                if rf in (7, 9) and m >= 15.5:
                    announce(f"Channel {rf} is in on the {ant} antenna, "
                             f"M E R {m:.1f}")
                    log({"event": "TROPO-CATCH", "rf": rf, "ant": ant,
                         "mer": m})
        # recalibrate-forever: every 3rd cycle, trim the hottest prospect
        if cyc % 3 == 0 and best_close:
            m0, rf, antenna, ant, rfg, ifgr = best_close
            key = f"rf{rf}|{ant}"
            best = (m0, rfg, ifgr)
            for d in (-6, +6):
                ii = max(20, min(59, ifgr + d))
                s = sample(rf, antenna, rfg, ii, secs=20)
                mm_ = s["mer_med"] or -99
                log({"event": "trim", "rf": rf, "ant": ant, "ifgr": ii,
                     "mer_med": s["mer_med"]})
                if mm_ > best[0]:
                    best = (mm_, rfg, ii)
            if best[2] != ifgr:
                state[key] = [best[1], best[2]]
                save_state(state)
                log({"event": "trim-adopt", "key": key, "gains": state[key],
                     "mer": best[0]})
        # sleep out the remainder of the 20-min slot
        rest = CYCLE_MIN * 60 - (time.time() - cycle_t0)
        if rest > 0 and datetime.now() < until:
            time.sleep(min(rest, (until - datetime.now()).total_seconds()))
    log({"event": "end", "cycles": cyc})
    print("cube complete", flush=True)


if __name__ == "__main__":
    main()
