"""Fast SoapySDR carrier detector.

Opens the SDR exactly once, tunes through a list of frequencies, captures
a brief sample window at each, and reports the metrics needed by the
ATSC 1.0 detection recipe in tv_tuner.run_scan(). Thresholds for the
recipe were tuned with tools/scan_lab/harness.py against a 35-channel
HDHomeRun ground truth set; the winning recipe lives in
tools/scan_lab/winning_recipe.json.

Per-frequency metrics emitted:

  * rms_dbfs            — total power across the captured bandwidth.
  * pilot_snr_db        — pilot bin (channel center − 2.69 MHz) over
                          out-of-band noise floor (median of bins
                          beyond ±3.5 MHz).
  * pilot_sharpness_db  — pilot peak vs the local ±100 kHz neighborhood
                          mean. Distinguishes a narrow CW pilot from a
                          broadband bump. *The single strongest ATSC 1.0
                          discriminator at this site.*
  * vsb_asymmetry_db    — power 0..3 MHz above pilot vs 0..3 MHz below
                          (the data sideband is single-sided in 8-VSB,
                          so real ATSC reads ≥ 3 dB; symmetric signals
                          read ~0 dB).
  * atsc3_db            — flat-spectrum OFDM signature: in-band excess
                          present BUT no narrow pilot AND no VSB
                          asymmetry. Heuristic only (we don't decode
                          3.0).

Mode (`--mode`):
  rms     — RMS only (works for any modulation; no per-band tuning).
  atsc1   — adds ATSC 1.0 pilot detection (sensitive but only useful
            on 6 MHz channels at standard ATSC alignment).
  atsc    — adds both pilot AND atsc3 metrics (default for our scanner).

Run with radioconda's Python (same as tv_live) so the SoapySDR
plugins are loadable.

Input:  JSON list of {"freq_hz": int, "label": str} on stdin
        (or backwards-compat: a plain list of ints).
Output: JSON list with all metrics on stdout. Status to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

try:
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
except ImportError:
    print("[sweep] SoapySDR Python bindings not found "
          "(run with radioconda Python)", file=sys.stderr)
    sys.exit(2)

import numpy as np

# ── ATSC pilot offset (2026-07-29, speed-1) ──────────────────────────────
# A/53 Part 2 §5.4.2 + fn.4: the pilot sits 309,440.559 Hz above the lower
# channel edge. The channel centre is 3.000 MHz above that edge, so the
# pilot is centre − 2,690,559.441 Hz. This file carried −2,690,000 Hz
# (+559 Hz off) and atsc_fpll_tight_impl.cc carried −2,691,000 Hz (−441 Hz
# off) — two different wrong approximations of the same spec constant.
# At the 488 Hz bin width of a 16384-pt FFT on 8 MS/s the fix moves the
# pilot search window by exactly ONE bin, which is inside the ±4-bin
# window, so detection is unchanged (verified on all 35 scan_lab fixtures:
# lab/speed_build/scan_gate_study.py). STVT_PILOT_OFFSET_HZ restores any
# other value, including the legacy −2690000, without a code change.
PILOT_OFFSET_HZ = float(os.environ.get("STVT_PILOT_OFFSET_HZ", "-2690559.441"))

# ── two-stage scan defaults (speed-1 lever 2) ────────────────────────────
# Stage A is a single 16384-point FFT = 2.05 ms of samples, measured on all
# 35 fixtures to reproduce the 200 ms verdict exactly with a BETTER
# worst-case margin (0.76 dB vs 0.62 dB) — see lab/speed_dossier.md §2.2.
# Stage B (the full dwell, today's detector) then runs ONLY where stage A
# saw a pilot, which is HDHomeRun's `if (!signal_present) return 1;`.
FAST_DWELL_SEC = float(os.environ.get("STVT_SCAN_FAST_DWELL", "0.00205"))
# Prescreen bars. These are NOT a detection gate — they only decide who pays
# for the full dwell — so they are set below the LOWEST gate downstream (the
# --thorough "weak" gate at pilot_snr 15 / sharpness 8), not below the strict
# one. Measured on all 35 scan_lab fixtures (lab/speed_build/scan_gate_study.py):
# 10/5 confirms 23 of 35 and reproduces every verdict the single-stage 100 ms
# sweep reaches — hot (TP 14, FP 0, FN 0), weak and atsc3 — while keeping 5 dB
# of pilot-SNR and 3 dB of sharpness slack under the weakest gate so a
# different antenna/market cannot silently lose a channel. Raising them to
# 15/8 buys another 0.4 s and costs one weak-list HINT on one fixture, which
# is why 15/8 is not the default.
PRESCREEN_SNR_DB = float(os.environ.get("STVT_SCAN_PRESCREEN_SNR", "10.0"))
PRESCREEN_SHARP_DB = float(os.environ.get("STVT_SCAN_PRESCREEN_SHARP", "5.0"))


def _fast_enabled() -> bool:
    """Two-stage scan is ON by default; STVT_SCAN_FAST=0 restores the
    single-stage full-dwell sweep byte-for-byte (the fallback knob)."""
    return os.environ.get("STVT_SCAN_FAST", "1") != "0"


def prescreen(metrics: dict,
              snr_db: float = None,
              sharp_db: float = None) -> bool:
    """Stage-A verdict: is there ANY hint of a pilot worth paying the full
    dwell for? Deliberately far more permissive than every gate downstream
    (strict 30/26.25, pilot-rescue 30/18, weak 15/8) so that stage A can
    only ever ADD work, never remove a candidate."""
    s = snr_db if snr_db is not None else PRESCREEN_SNR_DB
    h = sharp_db if sharp_db is not None else PRESCREEN_SHARP_DB
    return (metrics.get("pilot_snr_db", float("-inf")) >= s
            and metrics.get("pilot_sharpness_db", float("-inf")) >= h)


def open_sdr(driver: str, sample_rate: int, antenna: str,
             ifgr: float, rfgain_sel: int):
    """Open SDR with retry: SDRplay sometimes briefly holds the device
    after a previous tv_live exits."""
    last = None
    for attempt, settle in enumerate([0, 3, 6, 10], start=1):
        if settle:
            print(f"[sweep] SDR busy; retry {attempt} after {settle}s",
                  file=sys.stderr)
            time.sleep(settle)
        try:
            # Honor STVT_SOAPY_ARGS (e.g. SoapyRemote over TCP); else
            # auto-detect whatever radio is plugged in (issue #2: a
            # PlutoSDR probed fine while we insisted on driver=sdrplay).
            import sdr_compat
            args = os.environ.get("STVT_SOAPY_ARGS") \
                or sdr_compat.resolve_soapy_args(f"driver={driver}")
            sdr = SoapySDR.Device(args)
        except Exception as e:
            last = e
            if "no available RSP" not in str(e):
                raise
            continue
        sdr.setSampleRate(SOAPY_SDR_RX, 0, sample_rate)
        desc_a = sdr_compat.apply_antenna(sdr, antenna)
        try:
            desc_g = sdr_compat.apply_rx_gain(sdr, args, float(ifgr),
                                              rfgain_sel)
        except Exception as e:
            desc_g = f"(gain not set: {str(e)[:40]})"
        print(f"[sweep] radio {args}: antenna {desc_a}, {desc_g}",
              file=sys.stderr)
        return sdr
    raise RuntimeError(f"SDR open gave up: {last}")


def _analyze(samples: np.ndarray, sample_rate: int, mode: str) -> dict:
    """Compute RMS power, ATSC 1.0 pilot SNR, and ATSC 3.0 flat-spectrum
    score from a complex sample buffer. Returns a dict of metrics."""
    if samples.size == 0:
        return {"rms_dbfs": float("-inf"), "pilot_db": float("-inf"),
                "pilot_snr_db": float("-inf"), "atsc3_db": float("-inf")}

    # Broadband RMS.
    power = float(np.mean(np.abs(samples) ** 2))
    rms_dbfs = 10.0 * np.log10(power + 1e-20)

    base_empty = {
        "rms_dbfs": rms_dbfs,
        "pilot_snr_db": float("-inf"),
        "pilot_sharpness_db": float("-inf"),
        "vsb_asymmetry_db": float("-inf"),
        "in_band_excess_db": float("-inf"),
        "atsc3_db": float("-inf"),
    }
    if mode == "rms":
        return base_empty

    # FFT analysis. For short captures (≤16k samples), single FFT.
    # For longer captures (deep-scan), use Welch's method: chop into
    # disjoint windows of n_fft each, average the magnitude-squared
    # spectra. The pilot tone is CW so its bin power is identical
    # in every segment, while noise variance reduces by sqrt(N_seg).
    # Net gain on every threshold metric: ~10·log10(N_seg) dB.
    n_fft = 1 << 14  # 16384
    if samples.size < 1024:
        return base_empty
    n_segments = max(1, samples.size // n_fft)
    win = np.hanning(n_fft).astype(np.float32)
    psd = np.zeros(n_fft, dtype=np.float64)
    for k in range(n_segments):
        seg = samples[k * n_fft:(k + 1) * n_fft]
        if seg.size < n_fft:
            break
        spec = np.fft.fftshift(np.fft.fft(seg * win, n_fft))
        psd += np.abs(spec) ** 2
    psd /= max(1, n_segments)
    # Frequency axis: bin k corresponds to (k - n_fft/2) * sample_rate / n_fft
    bin_hz = sample_rate / n_fft

    # ATSC 1.0 pilot at centre − 2,690,559.441 Hz (A/53 P2 §5.4.2; see
    # PILOT_OFFSET_HZ at the top of this file).
    pilot_center_bin = n_fft // 2 + int(round(PILOT_OFFSET_HZ / bin_hz))
    # Narrow ±2 kHz pilot bin (a real CW pilot fits in 1-2 bins; widening
    # the window mostly admits noise).
    pilot_win = max(1, int(round(2e3 / bin_hz)))
    pilot_lo = max(0, pilot_center_bin - pilot_win)
    pilot_hi = min(n_fft, pilot_center_bin + pilot_win + 1)
    pilot_peak = float(np.max(psd[pilot_lo:pilot_hi])) if pilot_hi > pilot_lo else 0.0

    # Noise floor: median of bins beyond ±3.5 MHz (out of channel).
    margin_bins = int(round(3.5e6 / bin_hz))
    oob_lo = psd[:max(0, n_fft // 2 - margin_bins)]
    oob_hi = psd[min(n_fft, n_fft // 2 + margin_bins):]
    noise_ref = np.concatenate([oob_lo, oob_hi])
    noise_floor = float(np.median(noise_ref)) if noise_ref.size else \
                  float(np.median(psd))
    if noise_floor <= 0:
        noise_floor = 1e-20

    # Pilot sharpness: ratio of pilot peak to the local neighborhood mean
    # (±100 kHz around the pilot, excluding the pilot bin itself). Real
    # CW carriers concentrate energy in 1-2 bins → ratio 25-40 dB.
    # Broadband noise peaks → ratio 3-8 dB.
    nbhd_win = int(round(100e3 / bin_hz))
    nbhd_lo = max(0, pilot_center_bin - nbhd_win)
    nbhd_hi = min(n_fft, pilot_center_bin + nbhd_win + 1)
    nbhd = psd[nbhd_lo:nbhd_hi].copy()
    # Zero out the pilot bins so they don't contaminate the mean.
    inner_lo = max(0, (pilot_lo - nbhd_lo))
    inner_hi = max(inner_lo, (pilot_hi - nbhd_lo))
    nbhd[inner_lo:inner_hi] = 0
    nbhd_nonzero = nbhd[nbhd > 0]
    nbhd_mean = float(np.mean(nbhd_nonzero)) if nbhd_nonzero.size else noise_floor
    pilot_sharpness_db = 10.0 * np.log10(pilot_peak / nbhd_mean + 1e-20)

    # VSB asymmetry: ATSC's data sideband extends ~5.7 MHz ABOVE the pilot
    # and only ~0.3 MHz below (the vestigial portion). So if we integrate
    # power across equal-width bands above and below the pilot, the lower
    # band is mostly out-of-channel noise (only 0.3 MHz of it carries
    # vestigial energy), while the upper band is fully in-channel data.
    # Real ATSC: +8 to +15 dB. Noise / OFDM: ≈0 dB.
    bins_per_3m = max(1, int(round(3.0e6 / bin_hz)))
    above_lo = pilot_center_bin
    above_hi = min(n_fft, pilot_center_bin + bins_per_3m)
    below_lo = max(0, pilot_center_bin - bins_per_3m)
    below_hi = pilot_center_bin
    above_pow = (float(np.mean(psd[above_lo:above_hi]))
                 if above_hi > above_lo else 0.0)
    below_pow = (float(np.mean(psd[below_lo:below_hi]))
                 if below_hi > below_lo else 1e-20)
    vsb_asymmetry_db = 10.0 * np.log10(above_pow / below_pow + 1e-20)
    # `data_pow` is just the upper-half value, used elsewhere for in-band
    # excess and ATSC 3.0 detection.
    data_pow = above_pow

    pilot_snr_db = 10.0 * np.log10(pilot_peak / noise_floor + 1e-20)
    in_band_excess_db = 10.0 * np.log10(data_pow / noise_floor + 1e-20)

    # ATSC 3.0 / OFDM: in-band excess present BUT no narrow pilot AND no
    # VSB asymmetry (OFDM is symmetric across the channel).
    atsc3_db = in_band_excess_db if (
        in_band_excess_db > 5 and pilot_sharpness_db < 15
        and abs(vsb_asymmetry_db) < 4
    ) else float("-inf")

    return {
        "rms_dbfs": rms_dbfs,
        "pilot_snr_db": pilot_snr_db,
        "pilot_sharpness_db": pilot_sharpness_db,
        "vsb_asymmetry_db": vsb_asymmetry_db,
        "in_band_excess_db": in_band_excess_db,
        "atsc3_db": atsc3_db,
    }


def sweep(freqs_hz: list[int],
          sample_rate: int = 8_000_000,
          dwell_sec: float = 0.10,
          settle_sec: float = 0.04,
          mode: str = "atsc",
          driver: str = "sdrplay",
          antenna: str = "Antenna A",
          ifgr: float = 59.0,
          rfgain_sel: int = 5,
          progress=None,
          fast: bool = None,
          fast_dwell_sec: float = None,
          prescreen_snr_db: float = None,
          prescreen_sharp_db: float = None) -> list[dict]:
    """Tune to each freq, capture samples, return per-freq metrics dict.

    TWO-STAGE (default; `fast=False` or STVT_SCAN_FAST=0 for the legacy
    single-stage path):

      stage A  every frequency pays settle + `fast_dwell_sec` (2.05 ms =
               one 16384-pt FFT, the measured detection floor).
      stage B  ONLY frequencies whose stage-A metrics clear the (very
               permissive) prescreen pay the full `dwell_sec`, captured
               fresh and contiguously exactly as the legacy path does — so
               a confirmed channel's reported metrics are produced by the
               unchanged detector on an unchanged capture.

    Every record keeps every legacy key; `stage` ("fast"|"confirm") and
    `dwell_sec` are added alongside so consumers can see which detector
    spoke without any of them having to care.
    """
    if fast is None:
        fast = _fast_enabled()
    if fast_dwell_sec is None:
        fast_dwell_sec = FAST_DWELL_SEC
    # A stage-A window shorter than the FFT would analyse nothing, and a
    # stage-A window as long as the dwell means there is nothing to save.
    n_fast = int(sample_rate * fast_dwell_sec)
    if n_fast < (1 << 14) or fast_dwell_sec >= dwell_sec or mode == "rms":
        fast = False
    sdr = open_sdr(driver, sample_rate, antenna, ifgr, rfgain_sel)
    try:
        # Stream args from STVT_STREAM_ARGS (e.g. 'remote:prot=tcp' for the
        # SoapyRemote TCP transport); empty for a local device.
        _stream_kw = {}
        for _p in os.environ.get("STVT_STREAM_ARGS", "").split(","):
            if "=" in _p:
                _k, _v = _p.split("=", 1)
                _stream_kw[_k.strip()] = _v.strip()
        rx = (sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [0], _stream_kw)
              if _stream_kw
              else sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32))
        sdr.activateStream(rx)
        try:
            n_samples = int(sample_rate * dwell_sec)
            buf = np.zeros(n_samples, dtype=np.complex64)
            results = []
            n_confirmed = 0
            t_radio0 = time.time()

            def _fill(n_want: int) -> int:
                """Read n_want contiguous samples into buf[0:]. Identical to
                the legacy capture loop — used for BOTH stages so a
                confirmed channel's capture is exactly a legacy capture."""
                got = 0
                deadline = time.time() + 1.0
                while got < n_want and time.time() < deadline:
                    sr = sdr.readStream(rx, [buf[got:n_want]],
                                        n_want - got, timeoutUs=200_000)
                    n = sr.ret if hasattr(sr, "ret") else int(sr)
                    if n > 0:
                        got += n
                    else:
                        break
                return got

            for i, f in enumerate(freqs_hz):
                sdr.setFrequency(SOAPY_SDR_RX, 0, int(f))
                # Drain stale samples queued before the retune.
                drain_buf = np.zeros(int(sample_rate * settle_sec),
                                     dtype=np.complex64)
                t0 = time.time()
                while time.time() - t0 < settle_sec:
                    sdr.readStream(rx, [drain_buf], len(drain_buf),
                                   timeoutUs=int(settle_sec * 1e6))
                stage = "full"
                if fast:
                    # ── STAGE A: 2.05 ms, one FFT ──
                    got = _fill(n_fast)
                    metrics = _analyze(buf[:got], sample_rate, mode)
                    stage = "fast"
                    dwell_used = fast_dwell_sec
                    if prescreen(metrics, prescreen_snr_db,
                                 prescreen_sharp_db):
                        # ── STAGE B: the unchanged full-dwell detector ──
                        got = _fill(n_samples)
                        metrics = _analyze(buf[:got], sample_rate, mode)
                        stage = "confirm"
                        dwell_used = dwell_sec
                        n_confirmed += 1
                else:
                    got = _fill(n_samples)
                    metrics = _analyze(buf[:got], sample_rate, mode)
                    dwell_used = dwell_sec
                rec = {"freq_hz": int(f), "samples": int(got),
                       "stage": stage, "dwell_sec": round(dwell_used, 5),
                       **metrics}
                results.append(rec)
                if progress is not None:
                    progress(i + 1, len(freqs_hz), rec)
            print(f"[sweep] {len(freqs_hz)} frequencies in "
                  f"{time.time() - t_radio0:.2f}s of radio time "
                  f"({'two-stage' if fast else 'single-stage'}"
                  f"{f', {n_confirmed} confirmed' if fast else ''})",
                  file=sys.stderr, flush=True)
            return results
        finally:
            sdr.deactivateStream(rx)
            sdr.closeStream(rx)
    finally:
        del sdr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-rate", type=int, default=8_000_000)
    ap.add_argument("--dwell-sec", type=float, default=0.10)
    ap.add_argument("--settle-sec", type=float, default=0.04)
    ap.add_argument("--ifgr", type=float, default=59.0)
    ap.add_argument("--rfgain-sel", type=int, default=5)
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--mode", choices=["rms", "atsc1", "atsc"],
                    default="atsc",
                    help="Detection metrics. 'atsc' = RMS + ATSC 1.0 "
                         "pilot SNR + ATSC 3.0 OFDM signature.")
    ap.add_argument("--freq", action="append", type=int, default=[],
                     help="Frequency in Hz (repeat). If empty, read JSON "
                          "list from stdin.")
    # Two-stage scan (speed-1 lever 2). Default follows STVT_SCAN_FAST.
    ap.add_argument("--fast", dest="fast", action="store_true", default=None,
                    help="Force the two-stage sweep (2.05 ms stage A, full "
                         "dwell only where a pilot might be).")
    ap.add_argument("--no-fast", dest="fast", action="store_false",
                    help="Force the legacy single-stage full-dwell sweep.")
    ap.add_argument("--fast-dwell-sec", type=float, default=None,
                    help=f"Stage-A capture length (default {FAST_DWELL_SEC}, "
                         f"= one 16384-pt FFT at 8 MS/s).")
    ap.add_argument("--prescreen-snr", type=float, default=None,
                    help="Stage-A pilot SNR bar for paying the full dwell "
                         f"(default {PRESCREEN_SNR_DB} dB).")
    ap.add_argument("--prescreen-sharp", type=float, default=None,
                    help="Stage-A pilot sharpness bar for paying the full "
                         f"dwell (default {PRESCREEN_SHARP_DB} dB).")
    args = ap.parse_args()

    if args.freq:
        freqs = args.freq
    else:
        freqs = json.load(sys.stdin)

    def progress(i, total, rec):
        if args.mode == "rms":
            bar = (f"  [{i:>3}/{total}]  {rec['freq_hz']/1e6:6.2f} MHz  "
                   f"rms={rec['rms_dbfs']:+6.2f} dBFS")
        else:
            bar = (f"  [{i:>3}/{total}]  {rec['freq_hz']/1e6:6.2f} MHz  "
                   f"rms={rec['rms_dbfs']:+6.1f}  "
                   f"pilot_snr={rec['pilot_snr_db']:+6.1f} dB  "
                   f"atsc3={rec['atsc3_db']:+6.1f} dB"
                   # appended, never inserted: existing log readers see the
                   # historical line unchanged up to here.
                   f"  [{rec.get('stage', 'full')}]")
        print(bar, file=sys.stderr, flush=True)

    results = sweep(
        freqs,
        sample_rate=args.sample_rate,
        dwell_sec=args.dwell_sec,
        settle_sec=args.settle_sec,
        mode=args.mode,
        antenna=args.antenna,
        ifgr=args.ifgr,
        rfgain_sel=args.rfgain_sel,
        progress=progress,
        fast=args.fast,
        fast_dwell_sec=args.fast_dwell_sec,
        prescreen_snr_db=args.prescreen_snr,
        prescreen_sharp_db=args.prescreen_sharp,
    )
    json.dump(results, sys.stdout)


if __name__ == "__main__":
    main()
