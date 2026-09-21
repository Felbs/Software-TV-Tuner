#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Equalizer Probe: the receiver's own opinion of the signal, as messages.

Taps the equalizer's output (832-symbol data segments, nominal 8-VSB levels
+-1, +-3, +-5, +-7) and publishes, a few times a second:

  'dial'     (MER . dB)  decision-directed modulation error ratio - the number a tuning
                         loop (gr-rxtune) maximises, and what the panel plots
  'symbols'  f32vector   a few segments of equalised symbols, for an eye display
  'taps'     f32vector   the equalizer's taps (the channel's echoes), when `eq_getter`
                         can reach the equalizer block

Decision-directed MER is honest while the eye is open and compresses as it closes
(wrong decisions look like small errors), so read it as: above ~15 dB it is the true MER,
below that it is a floor. STVT's field-sync MER (the equalizer's STVT_EQ_TELEM line,
measured on KNOWN symbols) is the unbiased one; the two agree where television decodes.

Only a slice of each buffer is examined, so the block costs almost nothing at 10.76 M
symbols a second."""
import time

import numpy as np
import pmt
from gnuradio import gr

SEG = 832
LEVELS_MS = 21.0            # mean square of +-1, +-3, +-5, +-7


def dd_mer_db(x):
    """Decision-directed MER of 8-VSB symbols on the +-1..+-7 grid."""
    x = np.asarray(x, np.float64).ravel()
    dec = np.clip(2.0 * np.round((x - 1.0) / 2.0) + 1.0, -7.0, 7.0)
    err = float(np.mean((x - dec) ** 2))
    return 10.0 * np.log10(LEVELS_MS / max(err, 1e-12))


class eq_probe(gr.sync_block):
    def __init__(self, rate_hz=5.0, segments=48, eq_getter=None, name="MER"):
        gr.sync_block.__init__(self, name="atscplus_eq_probe",
                               in_sig=[(np.float32, SEG)], out_sig=None)
        self.period = 1.0 / max(float(rate_hz), 0.1)
        self.segments = int(segments)
        self.eq_getter = eq_getter
        self.dial_name = name
        self.mer_db = None
        self._t = 0.0
        for p in ("dial", "symbols", "taps"):
            self.message_port_register_out(pmt.intern(p))

    def work(self, input_items, output_items):
        in0 = input_items[0]
        now = time.time()
        if now - self._t >= self.period and len(in0):
            self._t = now
            x = np.array(in0[:self.segments], dtype=np.float32)     # own it: the buffer is reused
            if np.all(np.isfinite(x)):
                self.mer_db = dd_mer_db(x)
                self.message_port_pub(pmt.intern("dial"), pmt.cons(
                    pmt.intern(self.dial_name), pmt.from_double(self.mer_db)))
                show = x[:8].ravel()
                self.message_port_pub(pmt.intern("symbols"), pmt.cons(
                    pmt.PMT_NIL, pmt.init_f32vector(len(show), show)))
            if self.eq_getter is not None:
                try:
                    eq = self.eq_getter()
                    taps = np.asarray(eq.taps(), np.float32) if eq is not None else None
                except Exception:                                   # noqa: BLE001
                    taps = None
                if taps is not None and len(taps):
                    self.message_port_pub(pmt.intern("taps"), pmt.cons(
                        pmt.PMT_NIL, pmt.init_f32vector(len(taps), taps)))
        return len(in0)
