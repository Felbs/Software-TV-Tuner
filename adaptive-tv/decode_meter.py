"""Live REAL-DECODE meter — counts actual decoded video, not file growth.

The trap with measuring live.ts GROWTH RATE: the ATSC chain writes at full
rate (~2.4 MB/s) even when the output is GARBAGE (carrier locks but Reed-
Solomon can't correct the data). So a byte-rate meter reads "green" on noise.

This meter instead counts MPEG-2 sequence-header start codes (00 00 01 B3)
in the tail of live.ts — those only appear when video is REALLY decoding.
0 headers = no decode (multipath/below-cliff), even if the file is growing.
Many headers = real watchable TV. THIS is the honest aiming/repositioning
metric: sweep/move the antenna until the bar goes GREEN.

Run the tv_live chain first, then this.

Usage:
    python decode_meter.py
"""
import io
import os
import sys
import time

from PyQt5 import QtCore, QtWidgets

LIVE_TS = r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts"
TAIL_MB = 6
FULL_HDRS = 18.0   # seq-headers/6MB that counts as a clean, full decode


def count_seq_headers(buf: bytes) -> int:
    n = 0
    i = buf.find(b"\x00\x00\x01\xb3")
    while i != -1:
        n += 1
        i = buf.find(b"\x00\x00\x01\xb3", i + 4)
    return n


def tail_headers() -> int:
    try:
        with open(LIVE_TS, "rb", buffering=0) as f:
            f.seek(0, io.SEEK_END)
            size = f.tell()
            if size < 2 * 1024 * 1024:
                return -1   # not enough data yet
            start = (max(0, size - TAIL_MB * 1024 * 1024) // 188) * 188
            f.seek(start)
            return count_seq_headers(f.read())
    except OSError:
        return -2   # no chain / no file


class DecodeMeter(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("REAL-DECODE METER — move antenna until GREEN")
        self.resize(940, 380)
        self.setStyleSheet("background:#111;")
        v = QtWidgets.QVBoxLayout(self)

        self.big = QtWidgets.QLabel("…")
        self.big.setAlignment(QtCore.Qt.AlignCenter)
        self.big.setStyleSheet("color:#ff4444; font-size:80px; font-family:Consolas; font-weight:bold;")
        v.addWidget(self.big)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, int(FULL_HDRS))
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(70)
        v.addWidget(self.bar)

        self.sub = QtWidgets.QLabel("waiting for chain…")
        self.sub.setAlignment(QtCore.Qt.AlignCenter)
        self.sub.setStyleSheet("color:#aaaaaa; font-size:22px; font-family:Consolas;")
        v.addWidget(self.sub)

        self.best = 0
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(900)

    def tick(self):
        h = tail_headers()
        if h == -2:
            self.big.setText("no chain"); self.big.setStyleSheet(
                "color:#888; font-size:60px; font-family:Consolas; font-weight:bold;")
            self.sub.setText("start tv_live first"); self.bar.setValue(0); return
        if h == -1:
            self.big.setText("warming up"); self.sub.setText("equalizer converging…")
            self.bar.setValue(0); return
        self.best = max(self.best, h)
        self.big.setText(f"{h}")
        self.bar.setValue(int(min(FULL_HDRS, h)))
        if h >= 12:
            col, msg = "#00ff66", "◄◄◄ DECODING! LEAVE IT HERE ►►►"
        elif h >= 3:
            col, msg = "#ffcc00", "partial decode — nudge this way, it's close"
        else:
            col, msg = "#ff4444", "carrier may be there but NOT decoding — keep moving"
        self.big.setStyleSheet(f"color:{col}; font-size:80px; font-family:Consolas; font-weight:bold;")
        self.bar.setStyleSheet("QProgressBar{background:#222;border:2px solid #444;border-radius:6px;}"
                               f"QProgressBar::chunk{{background:{col};border-radius:4px;}}")
        self.sub.setText(f"{msg}    (seq-headers/{TAIL_MB}MB; best so far: {self.best})")


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = DecodeMeter(); w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
