"""lna_probe.py — quick: which port has signal, and does bias-T power the LNA?

Measures raw IQ RMS + in-band ATSC shelf (signal-above-guard) at a known UHF
channel on each antenna port, with bias-T OFF then ON. A powered LNA shows a
big jump in RMS/shelf; a bias-T-powered LNA jumps when bias-T turns ON.
"""
import time
import numpy as np
import SoapySDR
SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX

RF34_MHZ = 593.0           # known DC UHF mux that the attic antenna decoded
RATE, FFT = 8_000_000, 4096


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
    while time.time() - t0 < 1.0:
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
    return dict(shelf=shelf, rms=(rms / n) ** 0.5, peak=peak)


def main():
    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, RATE)
    try: sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception: pass
    sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 40.0)
    try: sdr.writeSetting("rfgain_sel", "3")
    except Exception: pass

    ants = sdr.listAntennas(SOAPY_SDR_RX, 0)
    print(f"  ports: {list(ants)}   (measuring RF34 / {RF34_MHZ} MHz)\n")
    print(f"  {'port':<12}{'biasT':>6}{'rawRMS':>10}{'peak':>8}{'shelf dB':>10}")
    for ant in ants:
        for bt in ("0", "1"):
            try: sdr.writeSetting("biasT_ctrl", bt)
            except Exception: pass
            time.sleep(0.3)
            m = measure(sdr, ant, RF34_MHZ)
            if m is None:
                print(f"  {ant:<12}{bt:>6}{'  (no samples)':>28}")
            else:
                flag = "  <-- signal!" if m["shelf"] > 4 else ""
                print(f"  {ant:<12}{bt:>6}{m['rms']:>10.4f}{m['peak']:>8.3f}{m['shelf']:>+10.1f}{flag}")
    try: sdr.writeSetting("biasT_ctrl", "0")
    except Exception: pass
    print("\n  shelf > ~4 dB = a real ATSC carrier is present on that port.")
    print("  If a port jumps from biasT 0 -> 1, the LNA is bias-T powered (keep it ON).")


if __name__ == "__main__":
    main()
