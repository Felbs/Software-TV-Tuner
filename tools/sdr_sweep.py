"""Fast SoapySDR power sweep.

Opens the SDR exactly once, tunes through a list of frequencies, captures
a brief sample window at each, and writes per-frequency RMS power (dBFS)
as JSON to stdout. Vastly faster than spawning a full GNU Radio flowgraph
per frequency — used by magic_tv.py's two-phase scanner to skip dead
channels before running the slow per-channel lock test.

This script must be invoked with radioconda's Python (same as tv_live)
so the SoapySDR plugins are loadable.

Input:  newline-separated JSON list of integer Hz frequencies on stdin,
        or repeated --freq Hz arguments.
Output: JSON list of {"freq_hz": int, "rms_dbfs": float, "samples": int}
        on stdout. Status messages go to stderr.
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


def sweep(freqs_hz: list[int],
          sample_rate: int = 8_000_000,
          dwell_sec: float = 0.15,
          settle_sec: float = 0.05,
          driver: str = "sdrplay",
          antenna: str = "Antenna A",
          ifgr: float = 59.0,
          rfgain_sel: int = 5,
          progress=None) -> list[dict]:
    """Tune to each freq, capture samples, return per-freq RMS power."""
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
                # Drain any stale samples queued before retune.
                drain_buf = np.zeros(int(sample_rate * settle_sec),
                                     dtype=np.complex64)
                t0 = time.time()
                while time.time() - t0 < settle_sec:
                    sdr.readStream(rx, [drain_buf], len(drain_buf),
                                   timeoutUs=int(settle_sec * 1e6))
                # Now grab a fresh window for the actual measurement.
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
                if got > 0:
                    samp = buf[:got]
                    power = float(np.mean(np.abs(samp) ** 2))
                    rms_dbfs = 10.0 * np.log10(power + 1e-20)
                else:
                    rms_dbfs = float("-inf")
                rec = {"freq_hz": int(f), "rms_dbfs": rms_dbfs,
                       "samples": int(got)}
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
    ap.add_argument("--dwell-sec", type=float, default=0.15)
    ap.add_argument("--settle-sec", type=float, default=0.05)
    ap.add_argument("--ifgr", type=float, default=59.0)
    ap.add_argument("--rfgain-sel", type=int, default=5)
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--freq", action="append", type=int, default=[],
                     help="Frequency in Hz (repeat). If empty, read JSON "
                          "list from stdin.")
    args = ap.parse_args()

    if args.freq:
        freqs = args.freq
    else:
        freqs = json.load(sys.stdin)

    def progress(i, total, rec):
        bar = (f"  [{i:>3}/{total}]  {rec['freq_hz']/1e6:6.2f} MHz  "
               f"{rec['rms_dbfs']:+6.2f} dBFS")
        print(bar, file=sys.stderr, flush=True)

    results = sweep(
        freqs,
        sample_rate=args.sample_rate,
        dwell_sec=args.dwell_sec,
        settle_sec=args.settle_sec,
        antenna=args.antenna,
        ifgr=args.ifgr,
        rfgain_sel=args.rfgain_sel,
        progress=progress,
    )
    json.dump(results, sys.stdout)


if __name__ == "__main__":
    main()
