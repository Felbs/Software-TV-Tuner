"""Audio signal sonar — walk the SDR around the house and FOLLOW THE PITCH.

Measures the ATSC 6 MHz in-band power shelf several times a second and emits
a beep whose PITCH rises with signal strength (like a metal detector). You
don't need to watch the screen — just listen: higher/faster pitch = stronger
signal. A fast double-beep means you've hit a likely-decodable spot.

This is the tool for a roaming survey on a long USB cable: plug the SDR in,
walk it around, and let your ears find the peak. When the pitch peaks, stop,
come back to the PC, and re-run the decode test there.

Usage:
    python signal_sonar.py                       # RF36 WTTG, Antenna B
    python signal_sonar.py --rf 31 --antenna "Antenna B"
    python signal_sonar.py --ifgr 35
"""
import argparse
import sys
import time

import numpy as np

try:
    import winsound
    HAVE_SOUND = True
except Exception:
    HAVE_SOUND = False

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import SoapySDR
SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX

ATSC_RF_CENTERS = {**{ch: c for ch, c in zip(range(2, 7), (57, 63, 69, 79, 85))},
                   **{ch: c for ch, c in zip(range(7, 14), (177, 183, 189, 195, 201, 207, 213))},
                   **{ch: 473.0 + (ch - 14) * 6.0 for ch in range(14, 37)}}

# Signal-shelf (dB) -> beep pitch (Hz). Higher signal = higher pitch.
SHELF_LO, SHELF_HI = 0.0, 18.0      # expected shelf range
FREQ_LO, FREQ_HI = 350, 2200        # Hz
LIKELY_DECODE_DB = 12.5             # at/above ≈ attic level that decodes


def shelf_to_freq(db: float) -> int:
    f = (db - SHELF_LO) / (SHELF_HI - SHELF_LO)
    f = max(0.0, min(1.0, f))
    return int(FREQ_LO + f * (FREQ_HI - FREQ_LO))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=36)
    ap.add_argument("--antenna", default="Antenna B")
    ap.add_argument("--ifgr", type=int, default=35)
    ap.add_argument("--rfgain", type=int, default=4)
    args = ap.parse_args()

    center = ATSC_RF_CENTERS.get(args.rf, 605.0) * 1e6
    rate, fft = 8_000_000, 4096
    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setAntenna(SOAPY_SDR_RX, 0, args.antenna)
    sdr.setSampleRate(SOAPY_SDR_RX, 0, rate)
    sdr.setFrequency(SOAPY_SDR_RX, 0, center)
    try: sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception: pass
    sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", float(args.ifgr))
    try: sdr.writeSetting("rfgain_sel", str(args.rfgain))
    except Exception: pass
    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    sdr.activateStream(stream)

    buf = np.empty(fft, dtype=np.complex64)
    win = np.hanning(fft).astype(np.float32)
    bin_hz = rate / fft
    dc = fft // 2
    dc_half = int(100_000 / bin_hz)
    in_lo, in_hi = dc - int(3e6 / bin_hz), dc + int(3e6 / bin_hz)
    warm = np.empty(65536, dtype=np.complex64)
    try: sdr.readStream(stream, [warm], 65536, timeoutUs=int(0.5e6))
    except Exception: pass

    print(f"SONAR on RF{args.rf} ({center/1e6:.0f} MHz) {args.antenna} — "
          f"follow the PITCH (higher = stronger). Ctrl-C to stop.")
    if not HAVE_SOUND:
        print("  (no winsound — printing dB only)")
    peak = -200.0
    try:
        while True:
            acc = np.zeros(fft); n = 0
            t0 = time.time()
            while time.time() - t0 < 0.25:
                sr = sdr.readStream(stream, [buf], fft, timeoutUs=int(0.3e6))
                if sr.ret < fft: continue
                acc += np.abs(np.fft.fftshift(np.fft.fft(buf * win))) ** 2; n += 1
            if n == 0:
                continue
            psd = acc / n
            mask = np.ones(fft, dtype=bool)
            mask[dc - dc_half:dc + dc_half] = False
            inband = psd[in_lo:in_hi][mask[in_lo:in_hi]]
            outband = np.concatenate([psd[:in_lo], psd[in_hi:]])
            shelf = 10 * np.log10(np.mean(inband) / (np.mean(outband) + 1e-20) + 1e-20)
            peak = max(peak, shelf)
            bar = "#" * int(max(0, min(40, shelf * 2)))
            flag = "  <-- LIKELY DECODES!" if shelf >= LIKELY_DECODE_DB else ""
            print(f"\r{shelf:5.1f} dB  peak {peak:5.1f}  {bar:<40}{flag}   ", end="", flush=True)
            if HAVE_SOUND:
                freq = shelf_to_freq(shelf)
                winsound.Beep(freq, 130)
                if shelf >= LIKELY_DECODE_DB:        # double-ping a hot spot
                    winsound.Beep(min(2500, freq + 300), 90)
    except KeyboardInterrupt:
        pass
    finally:
        print()
        sdr.deactivateStream(stream); sdr.closeStream(stream)


if __name__ == "__main__":
    main()
