"""Capture wideband I/Q fixtures for the scanner test bench.

Runs once against your live antenna + SDR. Tunes to each RF channel in
the configured region, records 200 ms of complex float32 I/Q at the
default scanner sample rate, and saves each capture to
  fixtures/<rf>.cf32

The harness then replays these captures against detection algorithms,
so the agent can test new detection logic without touching the SDR.
A 200 ms × 35-channel capture is ~430 MB total.

Run with radioconda Python:
  & "$env:USERPROFILE\\radioconda\\python.exe" \\
      tools\\scan_lab\\capture_fixtures.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
except ImportError:
    print("[capture] SoapySDR Python bindings not found "
          "(run with radioconda Python)", file=sys.stderr)
    sys.exit(2)

import numpy as np

HERE = Path(__file__).resolve().parent
FIXTURE_DIR = HERE / "fixtures"


def rf_to_freq_mhz(rf: int) -> float:
    if 2 <= rf <= 4:
        return 57.0 + (rf - 2) * 6.0
    if 5 <= rf <= 6:
        return 79.0 + (rf - 5) * 6.0
    if 7 <= rf <= 13:
        return 177.0 + (rf - 7) * 6.0
    if 14 <= rf <= 50:
        return 473.0 + (rf - 14) * 6.0
    return 0.0


def open_sdr(sample_rate: int = 8_000_000):
    last = None
    for attempt, settle in enumerate([0, 3, 6, 10], start=1):
        if settle:
            print(f"[capture] SDR busy; retry {attempt} after {settle}s",
                  file=sys.stderr)
            time.sleep(settle)
        try:
            sdr = SoapySDR.Device("driver=sdrplay")
        except Exception as e:
            last = e
            if "no available RSP" not in str(e):
                raise
            continue
        sdr.setSampleRate(SOAPY_SDR_RX, 0, sample_rate)
        try:
            sdr.setAntenna(SOAPY_SDR_RX, 0, "Antenna A")
        except Exception:
            pass
        try:
            sdr.setGainMode(SOAPY_SDR_RX, 0, False)
        except Exception:
            pass
        try:
            sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 59.0)
        except Exception:
            try:
                sdr.setGain(SOAPY_SDR_RX, 0, 59.0)
            except Exception:
                pass
        try:
            sdr.writeSetting("rfgain_sel", "5")
        except Exception:
            pass
        return sdr
    raise RuntimeError(f"SDR open gave up: {last}")


def capture_one(sdr, rx, freq_hz: int, sample_rate: int,
                 dwell_sec: float, settle_sec: float) -> np.ndarray:
    sdr.setFrequency(SOAPY_SDR_RX, 0, int(freq_hz))
    drain_buf = np.zeros(int(sample_rate * settle_sec), dtype=np.complex64)
    t0 = time.time()
    while time.time() - t0 < settle_sec:
        sdr.readStream(rx, [drain_buf], len(drain_buf),
                       timeoutUs=int(settle_sec * 1e6))
    n_target = int(sample_rate * dwell_sec)
    out = np.zeros(n_target, dtype=np.complex64)
    got = 0
    deadline = time.time() + dwell_sec * 3 + 1.0
    while got < n_target and time.time() < deadline:
        sr = sdr.readStream(rx, [out[got:]], n_target - got,
                             timeoutUs=200_000)
        n = sr.ret if hasattr(sr, "ret") else int(sr)
        if n > 0:
            got += n
        else:
            break
    return out[:got]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="na",
                     help="Region to capture: na | kr (default na)")
    ap.add_argument("--dwell-sec", type=float, default=0.2,
                     help="Capture duration per channel (default 200 ms)")
    ap.add_argument("--sample-rate", type=int, default=8_000_000)
    ap.add_argument("--settle-sec", type=float, default=0.05)
    ap.add_argument("--rfs", type=str, default=None,
                     help="Comma-separated RF list (overrides --region)")
    args = ap.parse_args()

    if args.rfs:
        rfs = [int(x) for x in args.rfs.split(",") if x.strip()]
    elif args.region == "kr":
        rfs = list(range(14, 51))
    else:
        rfs = list(range(2, 7)) + list(range(7, 14)) + list(range(14, 37))

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "sample_rate_hz": args.sample_rate,
        "dwell_sec": args.dwell_sec,
        "region": args.region,
        "captures": {},
    }
    n_target = int(args.sample_rate * args.dwell_sec)
    total_bytes = n_target * 8 * len(rfs)
    print(f"[capture] {len(rfs)} channels × {args.dwell_sec}s @ "
          f"{args.sample_rate/1e6:.1f} MS/s = {total_bytes/1e6:.0f} MB total")
    sdr = open_sdr(args.sample_rate)
    try:
        rx = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        sdr.activateStream(rx)
        try:
            for i, rf in enumerate(rfs, 1):
                freq_mhz = rf_to_freq_mhz(rf)
                if freq_mhz <= 0:
                    print(f"  [{i:>3}/{len(rfs)}] RF {rf}: unknown freq, skip")
                    continue
                freq_hz = int(freq_mhz * 1_000_000)
                print(f"  [{i:>3}/{len(rfs)}] RF {rf:>2} "
                      f"({freq_mhz:5.1f} MHz)... ", end="", flush=True)
                samples = capture_one(sdr, rx, freq_hz, args.sample_rate,
                                       args.dwell_sec, args.settle_sec)
                path = FIXTURE_DIR / f"rf{rf:02d}.cf32"
                samples.astype(np.complex64).tofile(path)
                manifest["captures"][str(rf)] = {
                    "rf": rf,
                    "freq_hz": freq_hz,
                    "freq_mhz": freq_mhz,
                    "file": path.name,
                    "samples": int(samples.size),
                }
                print(f"{samples.size:>7} samples → {path.name}")
        finally:
            sdr.deactivateStream(rx)
            sdr.closeStream(rx)
    finally:
        del sdr

    manifest_path = FIXTURE_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[capture] manifest written to {manifest_path}")
    print(f"[capture] done — {len(manifest['captures'])} fixtures saved.")


if __name__ == "__main__":
    main()
