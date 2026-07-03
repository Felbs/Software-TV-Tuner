"""auto_tv.py — universal ATSC orchestrator.

One command that figures out your whole setup:

  1. DETECT   which SDR is plugged in + what knobs it has (sdr_caps)
  2. PICK     the best TV antenna port for that device
  3. SCAN     which ATSC RF channels have signal (tv_tuner --scan), with a
              VHF-hi force-probe fallback since the cold scanner is conservative
  4. TUNE     auto-A/B the chain config for the chosen channel (auto_tune)
  5. PLAY     launch the chain with the winning config + marginal-signal player

Driver-aware: only probes hardware knobs the detected SDR actually exposes
(e.g. SDRplay's RF notch). On an RTL-SDR it skips the notch step automatically.

Usage:
    python auto_tv.py                      # full auto: detect->scan->tune->play best channel
    python auto_tv.py --rf 7               # skip scan, tune+play RF7 directly
    python auto_tv.py --rf 7 --quick       # skip auto-tune, use cached/default config
    python auto_tv.py --scan-only          # just detect + scan, print channel list
    python auto_tv.py --no-play            # detect->scan->tune, stop before playback
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE        = Path(__file__).resolve().parent
PY          = os.path.join(os.environ["USERPROFILE"], "radioconda", "python.exe")
SDRPLAY_DLL = r"C:\Program Files\SDRplay\API\x64"
TV_TUNER    = Path(r"Z:\src\magic-tv-decoder\tools\tv_tuner.py")
TV_LIVE     = Path(r"Z:\src\magic-tv-decoder\tools\tv_live.py")
SCAN_JSON   = Path(os.path.expanduser("~")) / ".tv_tuner" / "scan.json"

sys.path.insert(0, str(HERE))
from sdr_caps import detect_first_sdr

import numpy as np
import SoapySDR
SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX

# VHF-hi channels are the discone/wideband-antenna sweet spot (per memory
# discone_vhf_hi_atsc_works). Force-probe these if the cold scan finds nothing.
VHF_HI_RF = [7, 8, 9, 10, 11, 12, 13]
ATSC_RF_CENTERS = {2: 57.0, 3: 63.0, 4: 69.0, 5: 79.0, 6: 85.0,
                   7: 177.0, 8: 183.0, 9: 189.0, 10: 195.0, 11: 201.0, 12: 207.0, 13: 213.0,
                   **{ch: 473.0 + (ch - 14) * 6.0 for ch in range(14, 37)}}


def env_with_sdr():
    env = os.environ.copy()
    env["PATH"] = SDRPLAY_DLL + os.pathsep + env.get("PATH", "")
    return env


# ── phase 1: detect ────────────────────────────────────────────────
def phase_detect():
    print("=" * 70)
    print("PHASE 1 — detect SDR")
    print("=" * 70)
    caps = detect_first_sdr()
    if caps is None:
        print("  NO SDR found. Check USB / driver / PATH.")
        sys.exit(1)
    print(caps.report())
    return caps


# ── phase 1b: auto-pick the antenna port with the most signal ──────
def pick_best_antenna(caps, probe_rf: int = 7) -> str:
    """Measure in-band signal-to-floor on each antenna port at a reference
    VHF-hi frequency, pick the strongest. This is the 'works with any antenna
    on any port' smarts — instead of guessing, we measure where the signal is."""
    if len(caps.antennas) <= 1:
        return caps.antennas[0] if caps.antennas else "RX"
    print("\n" + "=" * 70)
    print("PHASE 1b — auto-pick antenna port (measure signal on each)")
    print("=" * 70)
    center = ATSC_RF_CENTERS.get(probe_rf, 177.0) * 1e6
    rate = 8_000_000
    fft = 4096
    sdr = SoapySDR.Device(caps._make_args())
    sdr.setSampleRate(SOAPY_SDR_RX, 0, rate)
    sdr.setFrequency(SOAPY_SDR_RX, 0, center)
    caps.gain_setter(sdr, 0.6)   # moderate gain via normalized interface
    win = np.hanning(fft).astype(np.float32)
    buf = np.empty(fft, dtype=np.complex64)
    results = {}
    for ant in caps.antennas:
        try:
            sdr.setAntenna(SOAPY_SDR_RX, 0, ant)
        except Exception:
            continue
        stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        sdr.activateStream(stream)
        warm = np.empty(65536, dtype=np.complex64)
        try: sdr.readStream(stream, [warm], 65536, timeoutUs=int(0.5e6))
        except Exception: pass
        acc = np.zeros(fft, dtype=np.float64); n = 0
        t0 = time.time()
        while time.time() - t0 < 2.0:
            sr = sdr.readStream(stream, [buf], fft, timeoutUs=int(0.5e6))
            if sr.ret < fft: continue
            acc += np.abs(np.fft.fftshift(np.fft.fft(buf * win)))**2; n += 1
        sdr.deactivateStream(stream); sdr.closeStream(stream)
        if n == 0:
            results[ant] = -200.0; continue
        psd = acc / n   # linear power per bin
        # ATSC signature = a 6 MHz-wide power SHELF above the guard bands.
        # central 6 MHz of the 8 MHz capture = 75% of bins; wings = the rest.
        # An empty antenna shows flat noise (shelf ~0 dB); a port with ATSC
        # shows the channel raised a few dB above the out-of-band guard.
        # Mask DC ±100 kHz (SDR spur lives there on every port — don't count it).
        bin_hz = rate / fft
        dc = fft // 2
        dc_half = int(100_000 / bin_hz)
        in_lo, in_hi = dc - int(3e6 / bin_hz), dc + int(3e6 / bin_hz)
        mask = np.ones(fft, dtype=bool)
        mask[dc - dc_half: dc + dc_half] = False   # exclude DC spur
        inband = psd[in_lo:in_hi][mask[in_lo:in_hi]]
        outband = np.concatenate([psd[:in_lo], psd[in_hi:]])
        shelf_db = 10 * np.log10(np.mean(inband) / (np.mean(outband) + 1e-20) + 1e-20)
        results[ant] = shelf_db
        print(f"  {ant:<12} in-band shelf {shelf_db:>+5.1f} dB")
    best = max(results, key=results.get)
    print(f"  -> strongest ATSC signal on: {best} ({results[best]:+.1f} dB shelf)")
    return best


# ── phase 3: scan ──────────────────────────────────────────────────
def phase_scan(antenna: str) -> list[dict]:
    print("\n" + "=" * 70)
    print("PHASE 2 — scan for ATSC channels")
    print("=" * 70)
    cmd = [PY, str(TV_TUNER), "--scan", "--thorough"]
    env = env_with_sdr()
    env["STVT_ANTENNA"] = antenna
    print(f"  running scan on {antenna} (this takes ~30-60s)...")
    try:
        subprocess.run(cmd, env=env, input="1\n", text=True,
                       timeout=600, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        print("  scan timed out")
    # Parse scan.json
    locked = []
    if SCAN_JSON.exists():
        d = json.loads(SCAN_JSON.read_text())
        for c in d.get("channels", []):
            if c.get("lock"):
                locked.append(c)
        # Sort all channels by pilot SNR for the force-probe fallback ranking
        all_chans = sorted(d.get("channels", []),
                           key=lambda c: -(c.get("pilot_snr_db") or 0))
        print(f"  scan complete: {len(locked)} channels locked, "
              f"noise floor {d.get('noise_floor_dbfs', 0):.1f} dBFS")
        if not locked:
            print("  no cold locks — top pilot tones (force-probe candidates):")
            for c in all_chans[:6]:
                print(f"    RF{c['rf']:>2}  {c['freq_mhz']:>6.1f} MHz  "
                      f"pilot_snr={c['pilot_snr_db']:>5.1f}  sharp={c['pilot_sharpness_db']:>5.1f}")
            return all_chans   # caller will force-probe VHF-hi
    return locked


# ── phase 3b: force-probe (cold scan is conservative) ──────────────
def force_probe_vhf_hi(antenna: str, candidates: list[dict], caps) -> int | None:
    """Live-test the strongest VHF-hi candidates by running the actual chain for
    ~40s and checking if live.ts grows (= real decode). Returns winning RF or None."""
    print("\n" + "=" * 70)
    print("PHASE 2b — force-probe VHF-hi (cold scan was conservative)")
    print("=" * 70)
    # Rank VHF-hi candidates by pilot SNR
    vhf = [c for c in candidates if c["rf"] in VHF_HI_RF]
    vhf.sort(key=lambda c: -(c.get("pilot_snr_db") or 0))
    if not vhf:
        # No scan data — just try the standard VHF-hi channels in order
        vhf = [{"rf": rf, "freq_mhz": ATSC_RF_CENTERS[rf]} for rf in VHF_HI_RF]
    notch = "1" if caps.has_rf_notch else "0"
    live_ts = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
    for c in vhf[:3]:   # try top 3
        rf = c["rf"]
        print(f"  probing RF{rf} ({c['freq_mhz']:.0f} MHz)...", flush=True)
        if live_ts.exists():
            try: live_ts.unlink()
            except Exception: pass
        env = env_with_sdr()
        env.update({"STVT_ANTENNA": antenna, "STVT_IFGR": "30", "STVT_RFGAIN_SEL": "4",
                    "STVT_RFNOTCH": notch, "STVT_VITERBI": "soft", "STVT_EQ": "long"})
        proc = subprocess.Popen([PY, "-u", str(TV_LIVE), "--rf", str(rf)],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Wait up to 45s for live.ts to grow past 5 MB (= real decode)
        decoded = False
        t0 = time.time()
        while time.time() - t0 < 45:
            time.sleep(3)
            if live_ts.exists() and live_ts.stat().st_size > 5 * 1024 * 1024:
                decoded = True
                break
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
        if decoded:
            print(f"    RF{rf} DECODES (live.ts grew to {live_ts.stat().st_size//(1024*1024)} MB)")
            return rf
        else:
            print(f"    RF{rf} no decode")
        time.sleep(2)
    return None


# ── phase 4: auto-tune ─────────────────────────────────────────────
def phase_tune(rf: int, antenna: str) -> dict:
    print("\n" + "=" * 70)
    print(f"PHASE 3 — auto-tune chain config for RF{rf}")
    print("=" * 70)
    # First make a profile (sweep characterization)
    subprocess.run([PY, str(HERE / "antenna_profile.py"), "--rf", str(rf),
                    "--antenna", antenna], env=env_with_sdr(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    # Then auto-tune (A/B test configs)
    print(f"  A/B testing 5 chain configs (~4 min)...")
    subprocess.run([PY, str(HERE / "auto_tune.py"), "--rf", str(rf),
                    "--antenna", antenna], env=env_with_sdr(),
                   timeout=600)
    # Read the winner back
    prof = HERE / "profiles" / f"RF{rf}_{antenna.replace(' ', '')}.json"
    if prof.exists():
        p = json.loads(prof.read_text())
        winner = p.get("auto_tune_winner", {})
        print(f"\n  winner: {winner.get('name', '?')}  env={winner.get('env', {})}")
        return winner.get("env", {"STVT_EQ": "long"})
    return {"STVT_EQ": "long"}


def pick_robust_program(live_ts: Path) -> int:
    """Snapshot the TS, map program_number -> resolution, pick the LOWEST-res
    program. On a marginal signal the small-I-frame SD subchannels survive
    packet loss far better than the big-I-frame HD ones. Returns program #."""
    snap = Path(os.environ["TEMP"]) / "auto_tv_progprobe.ts"
    try:
        import shutil
        # copy ~30 MB so ffprobe sees all PMTs
        with open(live_ts, "rb") as src, open(snap, "wb") as dst:
            dst.write(src.read(30 * 1024 * 1024))
        out = subprocess.run(
            ["ffprobe", "-hide_banner", "-f", "mpegts", "-probesize", "20000000",
             "-show_programs", "-of", "csv=p=0:nk=1", str(snap)],
            capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return 2   # safe SD default
    # CSV video rows: program_id is field[0], width/height appear mid-row.
    best_prog, best_px = None, 1 << 60
    for line in out.splitlines():
        f = line.split(",")
        if len(f) < 16 or "mpeg2video" not in line:
            continue
        try:
            prog = int(f[0])
            # width/height are the two consecutive ints after the pixfmt marker
            w = next(int(x) for x in f if x.isdigit() and int(x) in (704, 720, 1280, 1920))
            # crude: find height matching common values near width
            hs = [int(x) for x in f if x.isdigit() and int(x) in (480, 720, 1080)]
            h = hs[0] if hs else 480
            px = w * h
            if px < best_px:
                best_px, best_prog = px, prog
        except Exception:
            continue
    return best_prog if best_prog is not None else 2


# ── chain config + real-decode validation ─────────────────────────
# Proven decode defaults, mirrored from tv_tuner.py CHAIN_DEFAULTS. These
# are the config that the scanner uses to actually DECODE (not just lock).
# The old play path hardcoded IFGR=30 (a high-gain, marginal-signal setting)
# which OVERDRIVES a strong antenna into clipping: the carrier locks but the
# 8-VSB slices are wrong, RS can't correct, and live.ts fills with full-rate
# GARBAGE (no MPEG headers, random PIDs). IFGR is INVERTED: higher = less gain.
CHAIN_DEFAULTS = {
    "STVT_EQ": "long", "STVT_RS": "stock", "STVT_VITERBI": "soft",
    "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1",
    "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
}
# Gain ladder for "any antenna", strongest→weakest. IFGR inverted: 45 = a
# strong rooftop/attic antenna (less gain so it doesn't clip); 25 = a weak
# discone on the wrong port (max gain). Calibration walks this until the chain
# produces REAL decode, so one code path covers every antenna automatically.
GAIN_LADDER = [("45", "3"), ("50", "3"), ("40", "3"),
               ("55", "2"), ("35", "4"), ("30", "4"), ("25", "5"),
               # low-RF-gain rungs for FRONT-END OVERLOAD (e.g. a big antenna
               # grabbing strong FM): cut rfgain_sel for headroom, mid IFGR.
               ("42", "2"), ("45", "1"), ("48", "1")]


def _read_tail(path: Path, mb: int) -> bytes:
    """Read the last `mb` MB of a growing file using shared access (the chain
    holds it open for writing)."""
    import io
    with open(path, "rb", buffering=0) as f:
        f.seek(0, io.SEEK_END)
        size = f.tell()
        start = max(0, size - mb * 1024 * 1024)
        start = (start // 188) * 188
        f.seek(start)
        return f.read()


def count_video_starts(buf: bytes) -> int:
    """Count MPEG-2 sequence-header start codes (00 00 01 B3) in a byte buffer.
    REAL decoded video has many; full-rate garbage has ~zero. This is the only
    reliable 'is it actually decoding?' test — file GROWTH alone is fooled by
    garbage, which streams at full ATSC rate just like clean TV."""
    n = 0
    i = buf.find(b"\x00\x00\x01\xb3")
    while i != -1:
        n += 1
        i = buf.find(b"\x00\x00\x01\xb3", i + 4)
    return n


def has_real_decode(live_ts: Path, min_headers: int = 3) -> bool:
    """True iff the tail of live.ts contains real MPEG-2 video (not garbage).
    min_headers=3 matches SEQ_MIN_DECODE so a 'marginal' calibration verdict
    and this lock check agree (a faint-but-real signal isn't rejected here)."""
    try:
        return count_video_starts(_read_tail(live_ts, 8)) >= min_headers
    except Exception:
        return False


# Decode-quality thresholds (MPEG-2 seq-headers per 8 MB of live.ts tail).
# A clean HD multiplex shows ~12-50; a marginal/lossy signal shows a few;
# garbage shows 0. STRONG_ENOUGH lets a good antenna early-exit fast.
SEQ_MIN_DECODE  = 3     # below this = no usable decode
SEQ_STRONG_GOOD = 12    # at/above this = clearly clean; stop calibrating


def measure_decode_score(rf: int, antenna: str, ifgr: str, rfsel: str,
                         base: dict, secs: int = 30) -> int:
    """Run the chain at one gain setting and return a decode-QUALITY score =
    MPEG-2 sequence-headers in the last 8 MB of live.ts. 0 = garbage/no lock.
    Higher = cleaner decode. This is the per-gain fitness for calibration."""
    live_ts = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
    if live_ts.exists():
        try: live_ts.unlink()
        except Exception: pass
    env = env_with_sdr(); env.update(base)
    env["STVT_IFGR"] = ifgr; env["STVT_RFGAIN_SEL"] = rfsel
    proc = subprocess.Popen([PY, "-u", str(TV_LIVE), "--rf", str(rf)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    score = 0
    try:
        t0 = time.time()
        while time.time() - t0 < secs:
            time.sleep(3)
            if live_ts.exists() and live_ts.stat().st_size > 12 * 1024 * 1024:
                s = count_video_starts(_read_tail(live_ts, 8))
                score = max(score, s)
                if score >= SEQ_STRONG_GOOD:   # clearly clean — no need to wait
                    break
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
    return score


def calibrate_chain(rf: int, antenna: str, caps):
    """Walk the gain ladder and pick the gain with the BEST decode quality on
    this antenna (not just the first that passes — a marginal indoor antenna
    needs the PEAK, not the first hit). Early-exits the moment a gain is clearly
    clean so a strong antenna stays fast. Returns (env, quality) where quality
    is 'full' | 'marginal' | 'none' — the player mode keys off it."""
    print("\n" + "=" * 70)
    print(f"PHASE 3 — calibrate gain for RF{rf} on {antenna} (any-antenna auto)")
    print("=" * 70)
    base = dict(CHAIN_DEFAULTS)
    base["STVT_ANTENNA"] = antenna
    # Enable BOTH hardware notches by default: RF notch rejects strong FM/AM
    # (a big TV antenna grabs huge FM that overloads the front end), DAB notch
    # rejects strong 174-240 MHz VHF. Both are harmless when nothing's there and
    # are the cure for FM-overload (FPLL max|x| railing at pi/2, 0 decode).
    base["STVT_RFNOTCH"] = "1" if caps.has_rf_notch else "0"
    base["STVT_DABNOTCH"] = "1" if caps.has_dab_notch else "0"
    best_ifgr, best_rfsel, best_score = "45", "3", -1
    for ifgr, rfsel in GAIN_LADDER:
        print(f"  trying IFGR={ifgr} RFGAIN_SEL={rfsel} ...", end="", flush=True)
        score = measure_decode_score(rf, antenna, ifgr, rfsel, base)
        if score == 0:
            print("  garbage / no lock")
        elif score < SEQ_MIN_DECODE:
            print(f"  faint ({score} hdrs/8MB)")
        else:
            print(f"  decode ({score} hdrs/8MB)")
        if score > best_score:
            best_ifgr, best_rfsel, best_score = ifgr, rfsel, score
        if score >= SEQ_STRONG_GOOD:
            print(f"  -> clean decode at IFGR={ifgr}; stopping early.")
            break
        time.sleep(1)
    env_out = dict(base)
    env_out["STVT_IFGR"] = best_ifgr
    env_out["STVT_RFGAIN_SEL"] = best_rfsel
    # EQ ESCALATION: if the default 'long' EQ gave a PARTIAL (but real) decode,
    # the signal is borderline/multipath — try the decision-directed equalizers
    # at the winning gain to push it over the cliff. Only run when there's
    # something to improve (0 < score < clean): a stone-dead carrier won't be
    # rescued by a fancier EQ, so don't waste chain restarts on it.
    if 0 < best_score < SEQ_STRONG_GOOD:
        print(f"  borderline decode ({best_score} hdrs) — escalating equalizer for multipath...")
        for eq in ("pilot_dd", "multifs_dd", "pilot_dd_soft", "cma"):
            base_eq = dict(base); base_eq["STVT_EQ"] = eq
            print(f"    EQ={eq} ...", end="", flush=True)
            s = measure_decode_score(rf, antenna, best_ifgr, best_rfsel, base_eq)
            print(f" {s} hdrs/8MB")
            if s > best_score:
                best_score = s
                env_out["STVT_EQ"] = eq
            if best_score >= SEQ_STRONG_GOOD:
                break
            time.sleep(1)
    if best_score >= SEQ_STRONG_GOOD:
        quality = "full"
    elif best_score >= SEQ_MIN_DECODE:
        quality = "marginal"
    else:
        quality = "none"
    print(f"  WINNER: IFGR={best_ifgr} RFGAIN_SEL={best_rfsel} EQ={env_out['STVT_EQ']}  "
          f"score={best_score} hdrs/8MB  quality={quality.upper()}")
    return env_out, quality


# ── phase 5: play ──────────────────────────────────────────────────
def phase_play(rf: int, antenna: str, winner_env: dict, caps, quality: str = "full"):
    print("\n" + "=" * 70)
    print(f"PHASE 4 — play RF{rf}  (signal quality: {quality.upper()})")
    print("=" * 70)
    env = env_with_sdr()
    # winner_env already carries CHAIN_DEFAULTS + the calibrated gain that
    # produced REAL decode on this antenna (see calibrate_chain). No hardcoded
    # gain here — that was the "garbage on a strong antenna" bug.
    env.update(winner_env)
    # MARGINAL signals (weak/indoor antenna): switch the chain to erasure RS at
    # max budget to recover more packets, and run the player in concealment mode.
    # STRONG signals: keep stock RS + clean (--strong) player. This is the
    # adaptive part — same code, different recipe per measured signal quality.
    marginal = quality == "marginal"
    if marginal:
        env["STVT_RS"] = "erasure"
        env["STVT_RS_ERASURES"] = "20"
    notch = env.get("STVT_RFNOTCH", "1" if caps.has_rf_notch else "0")
    live_ts = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
    if live_ts.exists():
        try: live_ts.unlink()
        except Exception: pass
    print(f"  starting chain (IFGR={env.get('STVT_IFGR')}, RS={env.get('STVT_RS')}, "
          f"EQ={env.get('STVT_EQ')}, RFNOTCH={notch})...")
    chain = subprocess.Popen([PY, "-u", str(TV_LIVE), "--rf", str(rf)],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Wait for REAL decode, not just file growth — garbage grows the file at
    # full ATSC rate too. has_real_decode() checks for MPEG-2 sequence headers.
    t0 = time.time()
    locked = False
    while time.time() - t0 < 55:
        time.sleep(3)
        if live_ts.exists() and live_ts.stat().st_size > 12 * 1024 * 1024:
            if has_real_decode(live_ts):
                locked = True
                break
    if not locked:
        sz = live_ts.stat().st_size // (1024*1024) if live_ts.exists() else 0
        print(f"  CHAIN DID NOT LOCK (live.ts only {sz} MB after 55s).")
        print(f"  RF{rf} has no decodable signal on {antenna}. Not launching player.")
        if chain.poll() is None:
            chain.terminate()
            try: chain.wait(timeout=5)
            except Exception: chain.kill()
        return
    print(f"  chain locked ({live_ts.stat().st_size//(1024*1024)} MB).")
    prog = pick_robust_program(live_ts)
    # Clean signal -> --strong (no anti-freeze hacks that green-frame a strong
    # signal). Marginal -> concealment mode (datamosh through loss, don't freeze).
    play_args = [PY, str(HERE / "play_marginal.py"), str(prog), "--tail-mb", "15"]
    if not marginal:
        play_args.append("--strong")
    mode = "marginal/concealment" if marginal else "strong/clean"
    print(f"  launching player on program {prog} ({mode} mode)...")
    player = subprocess.Popen(play_args)
    print("\n  TV is playing. Ctrl-C here to stop everything.")
    try:
        player.wait()
    except KeyboardInterrupt:
        print("\n  stopping...")
    finally:
        for p in (player, chain):
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=5)
                except Exception: p.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=None, help="skip scan, use this RF channel")
    ap.add_argument("--antenna", default=None,
                    help="force a specific antenna port (e.g. 'Antenna C'). Default: auto-detect strongest.")
    ap.add_argument("--quick", action="store_true", help="skip calibration, use cached/default config")
    ap.add_argument("--deep", action="store_true",
                    help="after gain calibration, also A/B-test chain configs (slow; marginal signals)")
    ap.add_argument("--scan-only", action="store_true", help="detect + scan, then stop")
    ap.add_argument("--no-play", action="store_true", help="stop before playback")
    args = ap.parse_args()

    caps = phase_detect()
    # Antenna selection: explicit override, else auto-pick by measuring signal
    if args.antenna:
        antenna = args.antenna
        print(f"\n  -> using forced antenna: {antenna}")
    else:
        antenna = pick_best_antenna(caps, probe_rf=args.rf or 7)

    if args.rf is not None:
        rf = args.rf
        print(f"\n  using requested RF{rf}, skipping scan")
    else:
        candidates = phase_scan(antenna)
        locked = [c for c in candidates if c.get("lock")]
        if args.scan_only:
            print(f"\n[scan-only] {len(locked)} locked channels. Done.")
            return
        if locked:
            rf = max(locked, key=lambda c: c.get("pilot_snr_db", 0))["rf"]
            print(f"\n  best locked channel: RF{rf}")
        else:
            rf = force_probe_vhf_hi(antenna, candidates, caps)
            if rf is None:
                print("\n  no decodable channel found on this antenna. "
                      "Try a TV antenna on the full-spectrum port.")
                return

    # calibrate: walk the gain ladder until REAL decode (works on any antenna).
    # --deep additionally A/B-tests chain configs (slower, marginal signals only).
    if args.quick:
        prof = HERE / "profiles" / f"RF{rf}_{antenna.replace(' ', '')}.json"
        winner_env = dict(CHAIN_DEFAULTS)
        winner_env.update({"STVT_ANTENNA": antenna, "STVT_IFGR": "45", "STVT_RFGAIN_SEL": "3",
                           "STVT_RFNOTCH": "1" if caps.has_rf_notch else "0"})
        if prof.exists():
            p = json.loads(prof.read_text())
            cached = p.get("auto_tune_winner", {}).get("env")
            if cached:
                winner_env.update(cached)
            print(f"\n  [quick] using cached/default config: {winner_env}")
        quality = "full"   # quick mode assumes a known-good antenna
    else:
        winner_env, quality = calibrate_chain(rf, antenna, caps)
        if quality == "none":
            print(f"\n  no gain produced clean decode on RF{rf} / {antenna}. "
                  "Signal too weak — try aiming, a different RF channel, or another port.")
            return
        if args.deep:
            winner_env.update(phase_tune(rf, antenna))

    if args.no_play:
        print(f"\n[no-play] tuned RF{rf}, quality={quality}, winner env: {winner_env}. Done.")
        return

    phase_play(rf, antenna, winner_env, caps, quality)


if __name__ == "__main__":
    main()
