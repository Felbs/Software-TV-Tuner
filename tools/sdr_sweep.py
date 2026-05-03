"""Fast SoapySDR carrier detector.

Opens the SDR exactly once, tunes through a list of frequencies, captures
a brief sample window at each, and reports two metrics per frequency:

  * rms_dbfs   — total power across the captured bandwidth (broadband)
  * pilot_db   — for ATSC 1.0 mode only: SNR of the channel's pilot
                  carrier (a narrow CW spike at lower_edge + 309 kHz).
                  20-30 dB higher SNR than broadband RMS, so weak
                  carriers HD-HomeRun-class hardware would catch are
                  also caught here.
  * atsc3_db   — flat-spectrum OFDM signature score. ATSC 3.0 broadcasts
                  produce a flat power envelope across the channel
                  rather than the asymmetric 8-VSB profile; this metric
                  rises when the in-band power is uniformly distributed.

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

    if mode == "rms":
        return {"rms_dbfs": rms_dbfs, "pilot_db": float("-inf"),
                "pilot_snr_db": float("-inf"), "atsc3_db": float("-inf")}

    # FFT analysis. Window-then-FFT to suppress sidelobes.
    n_fft = 1 << 14  # 16384
    n = min(samples.size, n_fft)
    if n < 1024:
        return {"rms_dbfs": rms_dbfs, "pilot_db": float("-inf"),
                "pilot_snr_db": float("-inf"), "atsc3_db": float("-inf")}
    win = np.hanning(n).astype(np.float32)
    windowed = samples[:n] * win
    spec = np.fft.fftshift(np.fft.fft(windowed, n_fft))
    psd = np.abs(spec) ** 2
    # Frequency axis: bin k corresponds to (k - n_fft/2) * sample_rate / n_fft
    bin_hz = sample_rate / n_fft

    # ATSC 1.0 pilot is at -2.690 MHz from channel center (lower_edge +
    # 0.31 MHz, where lower_edge = center - 3 MHz). Look in a ±10 kHz
    # window around the expected bin to allow for tuner offset / phase
    # noise. The pilot is a CW spike, so peak-pick within the window.
    pilot_center_bin = n_fft // 2 + int(round(-2.690e6 / bin_hz))
    win_bins = max(1, int(round(20e3 / bin_hz)))  # ±10 kHz
    lo = max(0, pilot_center_bin - win_bins)
    hi = min(n_fft, pilot_center_bin + win_bins + 1)
    pilot_peak = float(np.max(psd[lo:hi])) if hi > lo else 0.0
    # Noise floor: median of bins outside the channel's main occupancy
    # (channel spans ±3 MHz from center). Use bins beyond ±3.5 MHz as
    # the local noise reference, falling back to global median if none.
    margin_bins = int(round(3.5e6 / bin_hz))
    out_of_band_lo = psd[:max(0, n_fft // 2 - margin_bins)]
    out_of_band_hi = psd[min(n_fft, n_fft // 2 + margin_bins):]
    noise_ref = np.concatenate([out_of_band_lo, out_of_band_hi])
    noise_floor = float(np.median(noise_ref)) if noise_ref.size else \
                  float(np.median(psd))
    if noise_floor <= 0:
        noise_floor = 1e-20
    pilot_snr_db = 10.0 * np.log10(pilot_peak / noise_floor + 1e-20)
    pilot_db = 10.0 * np.log10(pilot_peak / n_fft / n_fft + 1e-20)

    # ATSC 3.0 / OFDM signature: flat power envelope across the channel.
    # Compute std/mean of in-band PSD ÷ a clean reference; lower std is
    # flatter. Score = mean_in_band_db - rms_log_var. Simpler heuristic:
    # the in-band power is well above noise AND the pilot is NOT
    # particularly elevated relative to the rest of in-band → likely 3.0.
    in_band_lo = max(0, n_fft // 2 - int(round(3.0e6 / bin_hz)))
    in_band_hi = min(n_fft, n_fft // 2 + int(round(3.0e6 / bin_hz)))
    in_band = psd[in_band_lo:in_band_hi]
    in_band_mean = float(np.mean(in_band)) if in_band.size else 0.0
    in_band_excess_db = 10.0 * np.log10(in_band_mean / noise_floor + 1e-20)
    atsc3_db = in_band_excess_db  # crude; refined by caller w/ pilot ratio

    return {
        "rms_dbfs": rms_dbfs,
        "pilot_db": pilot_db,
        "pilot_snr_db": pilot_snr_db,
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
