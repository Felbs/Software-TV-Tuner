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
            sdr = SoapySDR.Device(f"driver={driver}")
        except Exception as e:
            last = e
            if "no available RSP" not in str(e):
                raise
            continue
        sdr.setSampleRate(SOAPY_SDR_RX, 0, sample_rate)
        try:
            sdr.setAntenna(SOAPY_SDR_RX, 0, antenna)
        except Exception:
            pass
        try:
            sdr.setGainMode(SOAPY_SDR_RX, 0, False)
        except Exception:
            pass
        try:
            sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", float(ifgr))
        except Exception:
            try:
                sdr.setGain(SOAPY_SDR_RX, 0, float(ifgr))
            except Exception:
                pass
        try:
            sdr.writeSetting("rfgain_sel", str(rfgain_sel))
        except Exception:
            pass
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

    # ATSC 1.0 pilot at -2.690 MHz from channel center.
    pilot_center_bin = n_fft // 2 + int(round(-2.690e6 / bin_hz))
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
          progress=None) -> list[dict]:
    """Tune to each freq, capture samples, return per-freq metrics dict."""
    sdr = open_sdr(driver, sample_rate, antenna, ifgr, rfgain_sel)
    try:
        rx = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        sdr.activateStream(rx)
        try:
            n_samples = int(sample_rate * dwell_sec)
            buf = np.zeros(n_samples, dtype=np.complex64)
            results = []
            for i, f in enumerate(freqs_hz):
                sdr.setFrequency(SOAPY_SDR_RX, 0, int(f))
                # Drain stale samples queued before the retune.
                drain_buf = np.zeros(int(sample_rate * settle_sec),
                                     dtype=np.complex64)
                t0 = time.time()
                while time.time() - t0 < settle_sec:
                    sdr.readStream(rx, [drain_buf], len(drain_buf),
                                   timeoutUs=int(settle_sec * 1e6))
                got = 0
                deadline = time.time() + 1.0
                while got < n_samples and time.time() < deadline:
                    sr = sdr.readStream(rx, [buf[got:]],
                                         n_samples - got,
                                         timeoutUs=200_000)
                    n = sr.ret if hasattr(sr, "ret") else int(sr)
                    if n > 0:
                        got += n
                    else:
                        break
                metrics = _analyze(buf[:got], sample_rate, mode)
                rec = {"freq_hz": int(f), "samples": int(got), **metrics}
                results.append(rec)
                if progress is not None:
                    progress(i + 1, len(freqs_hz), rec)
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
                   f"atsc3={rec['atsc3_db']:+6.1f} dB")
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
    )
    json.dump(results, sys.stdout)


if __name__ == "__main__":
    main()
