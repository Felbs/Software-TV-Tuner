"""quality_finder.py — aim-by-ear for MULTIPATH, not strength.

When the signal is already strong (shelf pegged) but the picture is glitchy, the
enemy is multipath: echoes carve frequency-selective NOTCHES into the otherwise
flat 6 MHz ATSC channel. This finder measures in-band spectral RIPPLE (p90-p10 of
the in-band PSD in dB) and maps a FLAT channel to a HIGH pitch. Rotate/slide the
antenna for the HIGHEST steady pitch = flattest channel = least multipath.

Continuous tone (Bluetooth-safe, same as tone_finder.py). Console shows ripple +
shelf. Ctrl-C to stop.

Usage:
    python quality_finder.py --rf 31 --antenna "Antenna A" --ifgr 40 --rfgain 3
"""
import argparse, sys, time
import numpy as np
import sounddevice as sd
import SoapySDR
SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX

RATE, FFT = 8_000_000, 4096
SR_AUDIO = 44100

# Ripple dB -> tone Hz (INVERTED: flat/low-ripple = high pitch = good).
FLAT_DB, ROUGH_DB = 4.0, 16.0      # <=4 dB ripple ~ clean; >=16 dB ~ bad multipath
LO_HZ, HI_HZ = 220.0, 1320.0
AVG = 12                            # FFTs averaged per update (stabilizes ripple)


class Tone:
    def __init__(self):
        self._target = LO_HZ; self._cur = LO_HZ; self._phase = 0.0; self._amp = 0.18
        self.stream = sd.OutputStream(samplerate=SR_AUDIO, channels=1,
                                      blocksize=1024, callback=self._cb)

    def set_freq(self, hz): self._target = float(hz)

    def _cb(self, outdata, frames, t, status):
        out = np.empty(frames, dtype=np.float32)
        for i in range(frames):
            self._cur += (self._target - self._cur) * 0.0008
            self._phase += 2 * np.pi * self._cur / SR_AUDIO
            if self._phase > 2 * np.pi: self._phase -= 2 * np.pi
            out[i] = self._amp * np.sin(self._phase)
        outdata[:, 0] = out

    def __enter__(self): self.stream.start(); return self
    def __exit__(self, *a): self.stream.stop(); self.stream.close()


def make_sdr(antenna, rf_mhz, ifgr, rfgain):
    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, RATE)
    try: sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception: pass
    sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", float(ifgr))
    try: sdr.writeSetting("rfgain_sel", str(rfgain))
    except Exception: pass
    try: sdr.writeSetting("rfnotch_ctrl", "1")
    except Exception: pass
    sdr.setAntenna(SOAPY_SDR_RX, 0, antenna)
    sdr.setFrequency(SOAPY_SDR_RX, 0, rf_mhz * 1e6)
    return sdr


def measure(sdr, st, buf, win):
    """Return (ripple_dB, shelf_dB) for the in-band 6 MHz channel."""
    acc = np.zeros(FFT); n = 0
    for _ in range(AVG):
        sr = sdr.readStream(st, [buf], FFT, timeoutUs=int(0.4e6))
        if sr.ret < FFT: continue
        acc += np.abs(np.fft.fftshift(np.fft.fft(buf * win))) ** 2; n += 1
    if n == 0: return None
    psd = acc / n
    bin_hz = RATE / FFT
    dc = FFT // 2
    dh = int(100_000 / bin_hz)                 # mask LO/DC spike
    half = int(2.7e6 / bin_hz)                 # +/- 2.7 MHz in-band
    lo, hi = dc - half, dc + half
    inband_full = psd[lo:hi]
    mask = np.ones(inband_full.size, bool)
    mask[half - dh:half + dh] = False          # drop DC bins
    inband = inband_full[mask]
    inband_db = 10 * np.log10(inband + 1e-20)
    # ripple = spread of in-band levels (percentiles reject narrow pilot/DC)
    ripple = float(np.percentile(inband_db, 90) - np.percentile(inband_db, 10))
    # shelf for reference
    outb = np.concatenate([psd[:lo], psd[hi:]])
    shelf = 10 * np.log10(np.mean(inband) / (np.mean(outb) + 1e-20) + 1e-20)
    return ripple, shelf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=31)
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--ifgr", type=int, default=40)
    ap.add_argument("--rfgain", type=int, default=3)
    args = ap.parse_args()

    CH = {14:473,15:479,17:491,18:497,19:503,20:509,21:515,26:545,30:569,
          31:575,33:587,34:593,35:599,36:605}
    rf_mhz = CH.get(args.rf, 575)
    print(f"  MULTIPATH finder: RF{args.rf} ({rf_mhz} MHz) on {args.antenna}")
    print(f"  HIGHER pitch = FLATTER channel = LESS multipath = cleaner picture.")
    print(f"  Rotate/slide antenna for the highest STEADY pitch. (Ctrl-C to stop)\n")

    sdr = make_sdr(args.antenna, rf_mhz, args.ifgr, args.rfgain)
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32); sdr.activateStream(st)
    buf = np.empty(FFT, dtype=np.complex64)
    win = np.hanning(FFT).astype(np.float32)
    w = np.empty(65536, dtype=np.complex64)
    try: sdr.readStream(st, [w], 65536, timeoutUs=int(0.5e6))
    except Exception: pass

    smooth = ROUGH_DB
    try:
        with Tone() as tone:
            while True:
                m = measure(sdr, st, buf, win)
                if m is None: continue
                ripple, shelf = m
                smooth += (ripple - smooth) * 0.4
                frac = max(0.0, min(1.0, (ROUGH_DB - smooth) / (ROUGH_DB - FLAT_DB)))
                tone.set_freq(LO_HZ + frac * (HI_HZ - LO_HZ))
                bar = "#" * int(frac * 40)
                sys.stdout.write(f"\r  ripple {smooth:4.1f}dB (flat={frac*100:3.0f}%) "
                                 f"shelf {shelf:+5.1f} |{bar:<40}| ")
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        sdr.deactivateStream(st); sdr.closeStream(st)
        print("\n  stopped.")


if __name__ == "__main__":
    main()
