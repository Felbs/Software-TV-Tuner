"""compare_antennas.py — head-to-head antenna characterization.

Measures two antenna ports under identical settings and reports a side-by-side
table so we can see EXACTLY where a weak/passive antenna loses to a good one:
  • raw IQ RMS / peak       (how much total energy each captures)
  • FM-band level (98 MHz)  (overload risk on big antennas)
  • per-TV-channel in-band SHELF dB  (signal-above-guard = decodability proxy)
  • the GAP (dB) between the two on each channel

Run with the chain stopped (it owns the SDR). Prints a comparison + a verdict
on whether the gap is "amplifiable" (weak-but-clean -> an LNA helps) or
"floor-limited" (signal in the noise -> no gain helps).

Usage:
    python compare_antennas.py --a "Antenna A" --b "Antenna B"
"""
import argparse
import time

import numpy as np
import SoapySDR
SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX

CHANNELS = {21: 515.0, 31: 575.0, 34: 593.0, 36: 605.0}
FM_MHZ = 98.0
RATE, FFT = 8_000_000, 4096


def make_sdr(ifgr=45, rfsel=3):
    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, RATE)
    try: sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception: pass
    sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", float(ifgr))
    try: sdr.writeSetting("rfgain_sel", str(rfsel))
    except Exception: pass
    return sdr


def measure(sdr, antenna, center_mhz):
    sdr.setAntenna(SOAPY_SDR_RX, 0, antenna)
    sdr.setFrequency(SOAPY_SDR_RX, 0, center_mhz * 1e6)
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32); sdr.activateStream(st)
    buf = np.empty(FFT, dtype=np.complex64)
    win = np.hanning(FFT).astype(np.float32)
    w = np.empty(65536, dtype=np.complex64)
    try: sdr.readStream(st, [w], 65536, timeoutUs=int(0.5e6))
    except Exception: pass
    acc = np.zeros(FFT); n = 0; rms = 0.0; peak = 0.0; t0 = time.time()
    while time.time() - t0 < 1.3:
        sr = sdr.readStream(st, [buf], FFT, timeoutUs=int(0.4e6))
        if sr.ret < FFT: continue
        acc += np.abs(np.fft.fftshift(np.fft.fft(buf * win))) ** 2; n += 1
        rms += float(np.mean(np.abs(buf) ** 2)); peak = max(peak, float(np.max(np.abs(buf))))
    sdr.deactivateStream(st); sdr.closeStream(st)
    if n == 0:
        return None
    psd = acc / n
    bin_hz = RATE / FFT
    dc = FFT // 2
    dc_half = int(100_000 / bin_hz)
    in_lo, in_hi = dc - int(3e6 / bin_hz), dc + int(3e6 / bin_hz)
    mask = np.ones(FFT, dtype=bool); mask[dc - dc_half:dc + dc_half] = False
    inband = psd[in_lo:in_hi][mask[in_lo:in_hi]]
    outband = np.concatenate([psd[:in_lo], psd[in_hi:]])
    shelf = 10 * np.log10(np.mean(inband) / (np.mean(outband) + 1e-20) + 1e-20)
    rms_lin = (rms / n) ** 0.5
    return dict(shelf=shelf, rms=rms_lin, peak=peak,
                inband_db=10 * np.log10(np.mean(inband) + 1e-20),
                floor_db=10 * np.log10(np.mean(outband) + 1e-20))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default="Antenna A")
    ap.add_argument("--b", default="Antenna B")
    args = ap.parse_args()
    sdr = make_sdr()

    print(f"\n{'':14}{'FM(98)':>9}{'rawRMS':>9}{'peak':>7}")
    rows = {}
    for label, ant in (("A " + args.a, args.a), ("B " + args.b, args.b)):
        fm = measure(sdr, ant, FM_MHZ)
        rows[ant] = fm
        print(f"  {label:<12}{fm['inband_db']:>9.1f}{fm['rms']:>9.4f}{fm['peak']:>7.3f}")

    print(f"\n  {'channel':<9}{'A shelf':>9}{'B shelf':>9}{'GAP(A-B)':>10}")
    gaps = []
    for rf, mhz in CHANNELS.items():
        ma = measure(sdr, args.a, mhz)
        mb = measure(sdr, args.b, mhz)
        gap = ma["shelf"] - mb["shelf"]
        gaps.append(gap)
        print(f"  RF{rf:<7}{ma['shelf']:>+9.1f}{mb['shelf']:>+9.1f}{gap:>+10.1f}")

    avg_gap = sum(gaps) / len(gaps)
    a_fm = rows[args.a]["inband_db"]; b_fm = rows[args.b]["inband_db"]
    print("\n" + "=" * 56)
    print("  ANALYSIS")
    print("=" * 56)
    print(f"  avg TV-signal gap A over B: {avg_gap:+.1f} dB")
    print(f"  FM pickup: A {a_fm:.1f} dB vs B {b_fm:.1f} dB  (A grabs {a_fm-b_fm:+.1f} dB more FM)")
    print("-" * 56)
    # verdict: is B's signal clean-but-weak (LNA helps) or in the noise floor?
    b_best = max(measure(sdr, args.b, m)["shelf"] for m in CHANNELS.values())
    if b_best >= 6:
        verdict = (f"B shows a real carrier (best shelf {b_best:+.1f} dB) but weaker. "
                   "If it locks-but-won't-decode, it's SNR/multipath limited — an "
                   "LNA at the antenna may push it over; gain alone won't.")
    elif b_best >= 2:
        verdict = (f"B is FAINT (best shelf {b_best:+.1f} dB) — near the noise floor. "
                   "An LNA amplifies noise too; only a bigger/higher antenna helps.")
    else:
        verdict = (f"B shows almost no carrier (best {b_best:+.1f} dB) — it's not "
                   "capturing the signal at this spot. Physical move needed.")
    print(f"  VERDICT: {verdict}")
    print("=" * 56)


if __name__ == "__main__":
    main()
