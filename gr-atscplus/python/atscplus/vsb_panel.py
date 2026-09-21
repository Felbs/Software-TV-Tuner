#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""8-VSB Panel (Qt): is the eye open, where are the echoes, is television coming out?

  * EYE      equalised symbols against the eight nominal levels. Eight clean bands =
             a picture; a smear = no picture, whatever any lock light says.
  * ECHOES   |equalizer taps| in dB: the main path and every reflection the equalizer
             is cancelling (needs the probe's `eq_getter`; blank otherwise).
  * MER      the dial, over the last minutes, with the ~15 dB picture cliff marked.
  * totals   transport packets out, how many the RS decoder gave up on, clean rate.

Inputs are the Equalizer Probe's and the TEI Scrub's messages. Optional: the receiver
runs without it."""
import collections
import threading

import numpy as np
import pmt
from gnuradio import gr
from PyQt5 import QtCore, QtGui, QtWidgets

LEVELS = (-7, -5, -3, -1, 1, 3, 5, 7)
BG = QtGui.QColor(18, 20, 24)
GRID = QtGui.QColor(60, 64, 72)
TEXT = QtGui.QColor(150, 154, 160)


class _Plot(QtWidgets.QWidget):
    def __init__(self, panel, kind, title):
        super().__init__()
        self.p, self.kind, self.title = panel, kind, title
        self.setMinimumSize(220, 150)

    def paintEvent(self, _):
        qp = QtGui.QPainter(self)
        w, h = self.width(), self.height()
        qp.fillRect(0, 0, w, h, BG)
        qp.setPen(TEXT)
        qp.drawText(8, 16, self.title)
        with self.p.lock:
            sym = self.p.sym
            taps = self.p.taps
            hist = list(self.p.mer_hist)
        top, bot = 24, h - 8
        if self.kind == "eye":
            y_of = lambda v: bot - (v + 9.0) / 18.0 * (bot - top)          # noqa: E731
            qp.setPen(GRID)
            for lv in LEVELS:
                qp.drawLine(0, int(y_of(lv)), w, int(y_of(lv)))
            if sym is not None and len(sym):
                qp.setPen(QtGui.QPen(QtGui.QColor(90, 200, 120, 150), 2))
                n = len(sym)
                pts = [QtCore.QPointF(i * w / n, y_of(float(np.clip(v, -9, 9)))) for i, v in enumerate(sym)]
                qp.drawPoints(QtGui.QPolygonF(pts))
        elif self.kind == "taps":
            if taps is None or not len(taps):
                qp.drawText(8, 40, "no taps (connect the probe's equalizer reference)")
                return
            mag = np.abs(np.asarray(taps, np.float64))
            db = 20.0 * np.log10(np.maximum(mag / max(mag.max(), 1e-12), 1e-4))   # 0 .. -80 dB
            qp.setPen(GRID)
            for d in (-20, -40, -60):
                y = top + (-d) / 80.0 * (bot - top)
                qp.drawLine(0, int(y), w, int(y))
                qp.drawText(4, int(y) - 2, f"{d} dB")
            qp.setPen(QtGui.QPen(QtGui.QColor(240, 170, 60), 1))
            n = len(db)
            for i in range(n):
                x = int(i * w / n)
                qp.drawLine(x, bot, x, int(top + (-db[i]) / 80.0 * (bot - top)))
        else:
            lo, hi = self.p.mer_lo, self.p.mer_hi
            y_of = lambda v: bot - (min(max(v, lo), hi) - lo) / (hi - lo) * (bot - top)   # noqa: E731
            qp.setPen(GRID)
            for d in range(int(lo), int(hi) + 1, 5):
                qp.drawLine(0, int(y_of(d)), w, int(y_of(d)))
                qp.drawText(4, int(y_of(d)) - 2, f"{d} dB")
            qp.setPen(QtGui.QPen(QtGui.QColor(200, 70, 60), 1, QtCore.Qt.DashLine))
            qp.drawLine(0, int(y_of(self.p.cliff)), w, int(y_of(self.p.cliff)))
            if len(hist) > 1:
                qp.setPen(QtGui.QPen(QtGui.QColor(90, 170, 255), 2))
                n = self.p.mer_hist.maxlen
                pts = [QtCore.QPointF(i * w / n, y_of(v)) for i, v in enumerate(hist)]
                qp.drawPolyline(QtGui.QPolygonF(pts))


class vsb_panel(gr.basic_block, QtWidgets.QWidget):
    def __init__(self, label="ATSC 1.0 / 8-VSB", cliff_db=15.2, mer_lo=5.0, mer_hi=35.0, parent=None):
        gr.basic_block.__init__(self, name="atscplus_vsb_panel", in_sig=None, out_sig=None)
        QtWidgets.QWidget.__init__(self, parent)
        self.lock = threading.Lock()
        self.cliff, self.mer_lo, self.mer_hi = float(cliff_db), float(mer_lo), float(mer_hi)
        self.sym = self.taps = None
        self.mer = None
        self.mer_hist = collections.deque(maxlen=600)
        self.stats = {}
        lay = QtWidgets.QVBoxLayout(self)
        self.totals = QtWidgets.QLabel(f"<b>{label}</b>")
        self.totals.setStyleSheet("font-size: 13pt;")
        lay.addWidget(self.totals)
        row = QtWidgets.QHBoxLayout()
        self.plots = [_Plot(self, "eye", "EYE: equalised symbols vs the 8 levels"),
                      _Plot(self, "taps", "ECHOES: |equalizer taps|"),
                      _Plot(self, "mer", "MER (dashed = picture cliff)")]
        for p in self.plots:
            row.addWidget(p, 1)
        lay.addLayout(row, 1)
        self.label = label
        for port, fn in (("dial", self.on_dial), ("symbols", self.on_symbols), ("taps", self.on_taps),
                         ("stats", self.on_stats)):
            self.message_port_register_in(pmt.intern(port))
            self.set_msg_handler(pmt.intern(port), fn)
        self.timer = QtCore.QTimer(self)               # handlers run on GNU Radio threads; paint on Qt's
        self.timer.timeout.connect(self.refresh)
        self.timer.start(200)

    def on_dial(self, msg):
        if pmt.is_pair(msg) and pmt.is_number(pmt.cdr(msg)):
            v = pmt.to_double(pmt.cdr(msg))
            with self.lock:
                self.mer = v
                self.mer_hist.append(v)

    def on_symbols(self, msg):
        v = np.array(pmt.f32vector_elements(pmt.cdr(msg)), np.float32)
        with self.lock:
            self.sym = v[::max(1, len(v) // 1500)]

    def on_taps(self, msg):
        v = np.array(pmt.f32vector_elements(pmt.cdr(msg)), np.float32)
        with self.lock:
            self.taps = v

    def on_stats(self, msg):
        d = pmt.to_python(msg)
        if isinstance(d, dict):
            with self.lock:
                self.stats = d

    def refresh(self):
        with self.lock:
            mer, st = self.mer, dict(self.stats)
        m = "-" if mer is None else f"{mer:.1f} dB"
        verdict = "" if mer is None else ("  eye OPEN" if mer >= self.cliff else "  eye closed")
        pk, bad = st.get("packets", 0), st.get("scrubbed", 0)
        pct = f"{100.0 * (pk - bad) / pk:.3f}% clean" if pk else "no transport packets yet"
        self.totals.setText(f"<b>{self.label}</b> &nbsp; MER {m}{verdict} &nbsp;|&nbsp; TS packets {pk:,} "
                            f"({pct}, {bad:,} uncorrectable) &nbsp;|&nbsp; {st.get('clean_per_s', 0):,.0f} clean/s")
        for p in self.plots:
            p.update()
