"""tone_finder.py — antenna-aiming finder with a CONTINUOUS tone (Bluetooth-safe).

winsound.Beep makes discrete beeps; Bluetooth headphones power-down between them
and clip/drop the start of each one. This streams an UNINTERRUPTED sine whose
PITCH rises with signal strength, so the BT audio link never idles and never
cuts out. Aim the antenna for the HIGHEST steady pitch.

Measures the in-band ATSC shelf (carrier-above-floor) on a channel and maps it
to tone pitch. Console prints a live bar too. Ctrl-C to stop.

Usage:
    python tone_finder.py --rf 36 --antenna "Antenna A" --ifgr 30
"""
import argparse, sys, threading, time
import numpy as np
import sounddevice as sd
import SoapySDR
SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX

RATE, FFT = 8_000_000, 4096
SR_AUDIO = 44100

# Shelf dB -> tone Hz. Below LO_DB = low drone; above HI_DB = top pitch.
LO_DB, HI_DB = 0.0, 14.0
LO_HZ, HI_HZ = 220.0, 1320.0


class Tone:
    """Continuous sine; set_freq() glides pitch with no gaps (BT stays awake)."""
    def __init__(self):
        self._target = LO_HZ
        self._cur = LO_HZ
        self._phase = 0.0
        self._amp = 0.18
        self.stream = sd.OutputStream(samplerate=SR_AUDIO, channels=1,
                                      blocksize=1024, callback=self._cb)

    def set_freq(self, hz):
        self._target = float(hz)

    def _cb(self, outdata, frames, t, status):
        # glide cur->target so pitch changes are smooth, never silent
        out = np.empty(frames, dtype=np.float32)
        for i in range(frames):
            self._cur += (self._target - self._cur) * 0.0008
            self._phase += 2 * np.pi * self._cur / SR_AUDIO
            if self._phase > 2 * np.pi:
                self._phase -= 2 * np.pi
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


def measure_shelf(sdr, st, buf, win):
    sr = sdr.readStream(st, [buf], FFT, timeoutUs=int(0.4e6))
    if sr.ret < FFT:
        return None
    psd = np.abs(np.fft.fftshift(np.fft.fft(buf * win))) ** 2
    bin_hz = RATE / FFT
    dc = FFT // 2
    dh = int(100_000 / bin_hz)
    lo, hi = dc - int(3e6 / bin_hz), dc + int(3e6 / bin_hz)
    m = np.ones(FFT, bool); m[dc - dh:dc + dh] = False
    inb = psd[lo:hi][m[lo:hi]]
    outb = np.concatenate([psd[:lo], psd[hi:]])
    return 10 * np.log10(np.mean(inb) / (np.mean(outb) + 1e-20) + 1e-20)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=36)
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--ifgr", type=int, default=30)
    ap.add_argument("--rfgain", type=int, default=3)
    args = ap.parse_args()

    CH = {14:473,17:491,20:509,21:515,26:545,30:569,31:575,33:587,34:593,35:599,36:605}
    rf_mhz = CH.get(args.rf, 605)
    print(f"  aiming finder: RF{args.rf} ({rf_mhz} MHz) on {args.antenna}")
    print(f"  CONTINUOUS tone -> higher pitch = stronger signal. Aim for top pitch.")
    print(f"  (Ctrl-C to stop)\n")

    sdr = make_sdr(args.antenna, rf_mhz, args.ifgr, args.rfgain)
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32); sdr.activateStream(st)
    buf = np.empty(FFT, dtype=np.complex64)
    win = np.hanning(FFT).astype(np.float32)
    w = np.empty(65536, dtype=np.complex64)
    try: sdr.readStream(st, [w], 65536, timeoutUs=int(0.5e6))
    except Exception: pass

    smooth = LO_DB
    try:
        with Tone() as tone:
            while True:
                vals = []
                for _ in range(8):
                    s = measure_shelf(sdr, st, buf, win)
                    if s is not None: vals.append(s)
                if not vals:
                    continue
                shelf = float(np.median(vals))
                smooth += (shelf - smooth) * 0.5
                frac = max(0.0, min(1.0, (smooth - LO_DB) / (HI_DB - LO_DB)))
                tone.set_freq(LO_HZ + frac * (HI_HZ - LO_HZ))
                bar = "#" * int(frac * 40)
                sys.stdout.write(f"\r  shelf {smooth:+5.1f} dB |{bar:<40}| ")
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        sdr.deactivateStream(st); sdr.closeStream(st)
        print("\n  stopped.")


if __name__ == "__main__":
    main()
