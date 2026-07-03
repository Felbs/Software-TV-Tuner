"""find_signal.py — combined VISUAL + AUDIO antenna signal finder.

Merges the two signal finders into one tool:
  • VISUAL  : big live dB readout + colored bar + peak-hold (watch at the PC)
  • AUDIO   : a sonar ping whose PITCH rises with signal (walk the SDR around
              on a long USB cable and follow the pitch — no screen needed)

It measures the ATSC 6 MHz in-band power SHELF (channel power above the guard
bands, DC spur masked) several times a second on one RF channel + antenna port.
Higher shelf = stronger signal. A fast double-ping (and green bar) means you've
hit a likely-decodable spot (≈ the level the attic antenna decodes at).

No chain needed — this reads raw IQ, so use it for roaming/aiming BEFORE you
try to decode. When the bar/pitch peaks, stop and run the decode test there.

Usage:
    python find_signal.py                          # RF36 WTTG, Antenna B
    python find_signal.py --rf 31 --antenna "Antenna B"
    python find_signal.py --ifgr 35 --mute         # visual only, no beeps
"""
import argparse
import sys
import time

import numpy as np
from PyQt5 import QtCore, QtWidgets

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

# shelf (dB) -> beep pitch (Hz); higher signal = higher pitch
SHELF_LO, SHELF_HI = 0.0, 18.0
FREQ_LO, FREQ_HI = 350, 2200
LIKELY_DECODE_DB = 12.5     # ≈ attic level that actually decodes


def shelf_to_freq(db):
    f = max(0.0, min(1.0, (db - SHELF_LO) / (SHELF_HI - SHELF_LO)))
    return int(FREQ_LO + f * (FREQ_HI - FREQ_LO))


class Meter(QtCore.QThread):
    reading = QtCore.pyqtSignal(float, float)   # shelf_db, peak_db

    def __init__(self, rf, antenna, ifgr, rfgain, mute):
        super().__init__()
        self.center = ATSC_RF_CENTERS.get(rf, 605.0) * 1e6
        self.antenna = antenna
        self.ifgr = ifgr
        self.rfgain = rfgain
        self.mute = mute or not HAVE_SOUND
        self.rate, self.fft = 8_000_000, 4096
        self.peak = -200.0
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        sdr = SoapySDR.Device("driver=sdrplay")
        sdr.setAntenna(SOAPY_SDR_RX, 0, self.antenna)
        sdr.setSampleRate(SOAPY_SDR_RX, 0, self.rate)
        sdr.setFrequency(SOAPY_SDR_RX, 0, self.center)
        try: sdr.setGainMode(SOAPY_SDR_RX, 0, False)
        except Exception: pass
        sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", float(self.ifgr))
        try: sdr.writeSetting("rfgain_sel", str(self.rfgain))
        except Exception: pass
        stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        sdr.activateStream(stream)
        buf = np.empty(self.fft, dtype=np.complex64)
        win = np.hanning(self.fft).astype(np.float32)
        bin_hz = self.rate / self.fft
        dc = self.fft // 2
        dc_half = int(100_000 / bin_hz)
        in_lo, in_hi = dc - int(3e6 / bin_hz), dc + int(3e6 / bin_hz)
        warm = np.empty(65536, dtype=np.complex64)
        try: sdr.readStream(stream, [warm], 65536, timeoutUs=int(0.5e6))
        except Exception: pass
        while not self._stop:
            acc = np.zeros(self.fft); n = 0
            t0 = time.time()
            while time.time() - t0 < 0.22:
                sr = sdr.readStream(stream, [buf], self.fft, timeoutUs=int(0.3e6))
                if sr.ret < self.fft: continue
                acc += np.abs(np.fft.fftshift(np.fft.fft(buf * win))) ** 2; n += 1
            if n == 0: continue
            psd = acc / n
            mask = np.ones(self.fft, dtype=bool)
            mask[dc - dc_half:dc + dc_half] = False
            inband = psd[in_lo:in_hi][mask[in_lo:in_hi]]
            outband = np.concatenate([psd[:in_lo], psd[in_hi:]])
            shelf = 10 * np.log10(np.mean(inband) / (np.mean(outband) + 1e-20) + 1e-20)
            self.peak = max(self.peak, shelf)
            self.reading.emit(float(shelf), float(self.peak))
            if not self.mute:                      # audio runs in THIS thread (won't block GUI)
                freq = shelf_to_freq(shelf)
                winsound.Beep(freq, 120)
                if shelf >= LIKELY_DECODE_DB:
                    winsound.Beep(min(2500, freq + 300), 80)
        sdr.deactivateStream(stream); sdr.closeStream(stream)


class FinderWindow(QtWidgets.QWidget):
    def __init__(self, rf, antenna, ifgr, rfgain, mute):
        super().__init__()
        snd = "muted" if (mute or not HAVE_SOUND) else "sonar ON"
        self.setWindowTitle(f"SIGNAL FINDER — RF{rf} ({ATSC_RF_CENTERS.get(rf,605):.0f} MHz) {antenna} [{snd}]")
        self.resize(940, 400)
        self.setStyleSheet("background:#111;")
        v = QtWidgets.QVBoxLayout(self)

        self.big = QtWidgets.QLabel("-- dB")
        self.big.setAlignment(QtCore.Qt.AlignCenter)
        self.big.setStyleSheet("color:#00ff66; font-size:90px; font-family:Consolas; font-weight:bold;")
        v.addWidget(self.big)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 250)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(70)
        v.addWidget(self.bar)

        self.sub = QtWidgets.QLabel("walk the antenna / rotate slowly — peak the bar & the pitch")
        self.sub.setAlignment(QtCore.Qt.AlignCenter)
        self.sub.setStyleSheet("color:#aaaaaa; font-size:22px; font-family:Consolas;")
        v.addWidget(self.sub)

        self.meter = Meter(rf, antenna, ifgr, rfgain, mute)
        self.meter.reading.connect(self.update_reading, QtCore.Qt.QueuedConnection)
        self.meter.start()

    def update_reading(self, shelf, peak):
        self.big.setText(f"{shelf:4.1f} dB")
        self.bar.setValue(max(0, min(250, int(shelf * 10))))
        if shelf >= LIKELY_DECODE_DB: col = "#00ff66"
        elif shelf >= 8:             col = "#ffcc00"
        else:                        col = "#ff4444"
        self.big.setStyleSheet(f"color:{col}; font-size:90px; font-family:Consolas; font-weight:bold;")
        self.bar.setStyleSheet("QProgressBar{background:#222;border:2px solid #444;border-radius:6px;}"
                               f"QProgressBar::chunk{{background:{col};border-radius:4px;}}")
        delta = shelf - peak
        hot = "  ◄ LIKELY DECODES!" if shelf >= LIKELY_DECODE_DB else ""
        near = " ◄ PEAK!" if delta > -0.5 else "keep moving"
        self.sub.setText(f"PEAK {peak:4.1f} dB   (now {delta:+.1f})   {near}{hot}")

    def closeEvent(self, e):
        self.meter.stop(); self.meter.wait(2000); super().closeEvent(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=36)
    ap.add_argument("--antenna", default="Antenna B")
    ap.add_argument("--ifgr", type=int, default=35)
    ap.add_argument("--rfgain", type=int, default=4)
    ap.add_argument("--mute", action="store_true", help="visual only, no audio pings")
    args = ap.parse_args()
    app = QtWidgets.QApplication(sys.argv)
    w = FinderWindow(args.rf, args.antenna, args.ifgr, args.rfgain, args.mute)
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
