#!/usr/bin/env python3
"""lna_probe.py — which port has signal, and is the LNA actually powered?

Measures raw IQ RMS plus the in-band ATSC "shelf" (signal above the guard
region) on a known channel, once per antenna port, with bias-T off and then on.

Read it like this:
  * shelf > ~4 dB          -> a real ATSC carrier is present on that port
  * shelf jumps 0 -> 1     -> that LNA is bias-T powered; leave bias-T on
  * shelf flat across both -> either no LNA, or it has its own supply

Bias-T note: on the RSPdx the bias-T output exists on Antenna B only, so
toggling it while another port is selected is a no-op in hardware. Pass
--no-biast to skip the powered half entirely if you would rather not send
DC toward anything at all.
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np

import SoapySDR
SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_FATAL)   # driver-load noise, not ours
from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sdr_compat                                     # noqa: E402
from tv_live import rf_to_freq_hz                     # noqa: E402

RATE, FFT = 8_000_000, 4096
DWELL_S = 1.0


def measure(sdr, antenna, center_hz):
    """Average PSD on one port; return in-band shelf, RMS and peak."""
    sdr.setAntenna(SOAPY_SDR_RX, 0, antenna)
    sdr.setFrequency(SOAPY_SDR_RX, 0, center_hz)
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    sdr.activateStream(st)
    buf = np.empty(FFT, dtype=np.complex64)
    win = np.hanning(FFT).astype(np.float32)

    # discard the first burst: the front end is still settling after a retune
    warm = np.empty(65536, dtype=np.complex64)
    try:
        sdr.readStream(st, [warm], 65536, timeoutUs=int(0.5e6))
    except Exception:
        pass

    acc = np.zeros(FFT)
    n = 0
    rms = 0.0
    peak = 0.0
    t0 = time.time()
    while time.time() - t0 < DWELL_S:
        sr = sdr.readStream(st, [buf], FFT, timeoutUs=int(0.4e6))
        if sr.ret < FFT:
            continue
        acc += np.abs(np.fft.fftshift(np.fft.fft(buf * win))) ** 2
        n += 1
        rms += float(np.mean(np.abs(buf) ** 2))
        peak = max(peak, float(np.max(np.abs(buf))))
    sdr.deactivateStream(st)
    sdr.closeStream(st)
    if n == 0:
        return None

    psd = acc / n
    bin_hz = RATE / FFT
    dc = FFT // 2
    dc_half = int(100_000 / bin_hz)          # notch the tuner's own DC spur
    in_lo, in_hi = dc - int(3e6 / bin_hz), dc + int(3e6 / bin_hz)
    mask = np.ones(FFT, dtype=bool)
    mask[dc - dc_half:dc + dc_half] = False
    inband = psd[in_lo:in_hi][mask[in_lo:in_hi]]
    outband = np.concatenate([psd[:in_lo], psd[in_hi:]])
    shelf = 10 * np.log10(np.mean(inband) / (np.mean(outband) + 1e-20) + 1e-20)
    return dict(shelf=shelf, rms=(rms / n) ** 0.5, peak=peak)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rf", type=int, default=34,
                    help="ATSC channel to probe (default 34). Pick one that is "
                         "strong where you live — an empty channel proves nothing.")
    ap.add_argument("--driver", default="sdrplay", help="SoapySDR driver (default sdrplay)")
    ap.add_argument("--ifgr", type=float, default=40.0, help="IF gain reduction dB (default 40)")
    ap.add_argument("--rfgain-sel", default="3", help="RSP RF gain step (default 3)")
    ap.add_argument("--no-biast", action="store_true",
                    help="do not toggle bias-T; measure unpowered only")
    args = ap.parse_args()

    center = rf_to_freq_hz(args.rf)          # never hand-compute a channel centre
    sdr = SoapySDR.Device(sdr_compat.resolve_soapy_args(f"driver={args.driver}"))
    sdr.setSampleRate(SOAPY_SDR_RX, 0, RATE)
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception:
        pass
    try:
        sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", args.ifgr)
    except Exception:
        pass
    try:
        sdr.writeSetting("rfgain_sel", args.rfgain_sel)
    except Exception:
        pass

    ants = list(sdr.listAntennas(SOAPY_SDR_RX, 0))
    print(f"  ports: {ants}   (measuring RF{args.rf} / {center/1e6:.3f} MHz)\n")
    print(f"  {'port':<12}{'biasT':>6}{'rawRMS':>10}{'peak':>8}{'shelf dB':>10}")
    states = ("0",) if args.no_biast else ("0", "1")
    for ant in ants:
        for bt in states:
            try:
                sdr.writeSetting("biasT_ctrl", bt)
            except Exception:
                pass
            time.sleep(0.3)
            m = measure(sdr, ant, center)
            if m is None:
                print(f"  {ant:<12}{bt:>6}{'  (no samples)':>28}")
            else:
                flag = "  <-- signal!" if m["shelf"] > 4 else ""
                print(f"  {ant:<12}{bt:>6}{m['rms']:>10.4f}{m['peak']:>8.3f}"
                      f"{m['shelf']:>+10.1f}{flag}")
    try:
        sdr.writeSetting("biasT_ctrl", "0")      # never leave DC on the coax
    except Exception:
        pass

    print("\n  shelf > ~4 dB = a real ATSC carrier is present on that port.")
    print("  If a port jumps from biasT 0 -> 1, that LNA is bias-T powered (keep it ON).")
    print("  All ports flat and low? Try --rf with a channel you know is strong here.")


if __name__ == "__main__":
    main()
