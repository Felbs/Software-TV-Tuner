"""ch_scan.py — sweep DC-area UHF channels on one port, report in-band shelf.

Finds which channel a (directional) antenna is actually seeing, instead of
assuming RF34. Higher shelf = stronger ATSC carrier. Use to aim/pick a channel.
"""
import argparse, time
import numpy as np
import SoapySDR
SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX

RATE, FFT = 8_000_000, 4096
# DC / Baltimore real ATSC RF channels -> center MHz
CH = {14:473,15:479,16:485,17:491,18:497,19:503,20:509,21:515,22:521,23:527,
      24:533,25:539,26:545,27:551,28:557,29:563,30:569,31:575,32:581,33:587,
      34:593,35:599,36:605}


def measure(sdr, st, buf, win):
    acc = np.zeros(FFT); n = 0; rms = 0.0
    t0 = time.time()
    while time.time() - t0 < 0.8:
        sr = sdr.readStream(st, [buf], FFT, timeoutUs=int(0.4e6))
        if sr.ret < FFT:
            continue
        acc += np.abs(np.fft.fftshift(np.fft.fft(buf * win))) ** 2; n += 1
        rms += float(np.mean(np.abs(buf) ** 2))
    if n == 0:
        return None
    psd = acc / n
    bin_hz = RATE / FFT
    dc = FFT // 2
    dh = int(100_000 / bin_hz)
    lo, hi = dc - int(3e6 / bin_hz), dc + int(3e6 / bin_hz)
    m = np.ones(FFT, bool); m[dc - dh:dc + dh] = False
    inb = psd[lo:hi][m[lo:hi]]
    outb = np.concatenate([psd[:lo], psd[hi:]])
    shelf = 10 * np.log10(np.mean(inb) / (np.mean(outb) + 1e-20) + 1e-20)
    return shelf, (rms / n) ** 0.5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--ifgr", type=int, default=40)
    ap.add_argument("--rfgain", type=int, default=3)
    ap.add_argument("--biast", action="store_true", help="power a bias-T LNA")
    args = ap.parse_args()

    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, RATE)
    try: sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception: pass
    sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", float(args.ifgr))
    try: sdr.writeSetting("rfgain_sel", str(args.rfgain))
    except Exception: pass
    sdr.setAntenna(SOAPY_SDR_RX, 0, args.antenna)
    try: sdr.writeSetting("biasT_ctrl", "true" if args.biast else "false")
    except Exception: pass
    if args.biast: time.sleep(1.0)          # LNA power-up settle
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32); sdr.activateStream(st)
    buf = np.empty(FFT, dtype=np.complex64)
    win = np.hanning(FFT).astype(np.float32)

    print(f"  channel sweep on {args.antenna} (IFGR={args.ifgr}, rfgain={args.rfgain})\n")
    print(f"  {'RF':>4}{'MHz':>7}{'shelf dB':>10}{'rawRMS':>10}")
    rows = []
    for rf in sorted(CH):
        sdr.setFrequency(SOAPY_SDR_RX, 0, CH[rf] * 1e6)
        time.sleep(0.15)
        m = measure(sdr, st, buf, win)
        if m is None:
            continue
        shelf, rms = m
        flag = "  <-- carrier" if shelf > 5 else (" weak" if shelf > 3.8 else "")
        print(f"  {rf:>4}{CH[rf]:>7}{shelf:>+10.1f}{rms:>10.4f}{flag}")
        rows.append((rf, shelf, rms))
    sdr.deactivateStream(st); sdr.closeStream(st)
    rows.sort(key=lambda r: -r[1])
    print("\n  strongest:")
    for rf, shelf, rms in rows[:5]:
        print(f"    RF{rf} ({CH[rf]} MHz): {shelf:+.1f} dB")


if __name__ == "__main__":
    main()
