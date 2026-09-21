#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""QA for the pure-Python blocks. Needs no radio, capture or compiled module beyond an
installed gnuradio.atscplus (from the source tree: examples/_devpath.py grafts them on)."""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
import pmt  # noqa: E402
from gnuradio import blocks, gr, gr_unittest  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "examples"))
try:
    from gnuradio.atscplus import tei_scrub, eq_probe, dd_mer_db  # noqa: E402
except ImportError:
    import _devpath  # noqa: E402,F401
    from gnuradio.atscplus import tei_scrub, eq_probe, dd_mer_db  # noqa: E402
from gnuradio import atscplus  # noqa: E402

LEVELS = np.array([-7, -5, -3, -1, 1, 3, 5, 7], np.float32)


class msg_sink(gr.basic_block):
    def __init__(self, *ports):
        gr.basic_block.__init__(self, name="msg_sink", in_sig=None, out_sig=None)
        self.got = {p: [] for p in ports}
        for p in ports:
            self.message_port_register_in(pmt.intern(p))

            def h(msg, p=p):
                self.got[p].append(msg)
            h.__name__ = "_h_" + p
            setattr(self, h.__name__, h)
            self.set_msg_handler(pmt.intern(p), h)


class qa_python_blocks(gr_unittest.TestCase):

    def test_001_tei_scrub_replaces_only_uncorrectable_packets_and_counts(self):
        pk = np.zeros((5, 188), np.uint8)
        pk[:, 0] = 0x47
        pk[1, 1] = 0x80 | 0x11                                   # TEI set
        pk[3, 1] = 0x80
        src = blocks.vector_source_b(pk.ravel().tolist(), False, 188)
        tei = tei_scrub(report_s=0.0)
        snk = blocks.vector_sink_b(188)
        tb = gr.top_block()
        tb.connect(src, tei, snk)
        tb.run()
        out = np.array(snk.data(), np.uint8).reshape(-1, 188)
        self.assertEqual(out.shape[0], 5)
        for i in (1, 3):
            self.assertEqual(tuple(out[i, :4]), (0x47, 0x1F, 0xFF, 0x10))
            self.assertTrue(np.all(out[i, 4:] == 0xFF))
        for i in (0, 2, 4):
            self.assertTrue(np.array_equal(out[i], pk[i]))
        self.assertEqual((tei.n_packets, tei.n_scrubbed, tei.n_clean), (5, 2, 3))

    def test_002_scrub_off_passes_bad_packets_through_but_still_counts(self):
        pk = np.zeros((2, 188), np.uint8)
        pk[:, 0] = 0x47
        pk[0, 1] = 0x80
        src = blocks.vector_source_b(pk.ravel().tolist(), False, 188)
        tei = tei_scrub(scrub=False, report_s=0.0)
        snk = blocks.vector_sink_b(188)
        tb = gr.top_block()
        tb.connect(src, tei, snk)
        tb.run()
        out = np.array(snk.data(), np.uint8).reshape(-1, 188)
        self.assertTrue(np.array_equal(out, pk))
        self.assertEqual(tei.n_scrubbed, 1)

    def test_003_decision_directed_mer(self):
        rng = np.random.default_rng(1)
        sym = rng.choice(LEVELS, 832 * 8)
        self.assertGreater(dd_mer_db(sym), 60)                    # perfect symbols
        m = dd_mer_db(sym + rng.normal(0, 0.3, sym.shape).astype(np.float32))
        self.assertAlmostEqual(m, 10 * np.log10(21 / 0.09), delta=0.6)        # 23.7 dB: eye open, exact
        # as the eye closes, decisions flip and the error looks SMALLER: decision-directed MER reads HIGH.
        # That bias is the documented reason it is a dial above the cliff and a floor below it.
        m = dd_mer_db(sym + rng.normal(0, 0.6, sym.shape).astype(np.float32))
        self.assertGreater(m, 10 * np.log10(21 / 0.36))                          # true 17.7 dB; reads ~18.9

    def test_004_probe_publishes_dial_symbols_and_taps(self):
        rng = np.random.default_rng(2)
        sym = (rng.choice(LEVELS, 832 * 48) + rng.normal(0, 0.3, 832 * 48)).astype(np.float32)

        class fake_eq:
            def taps(self):
                return [0.0, 1.0, 0.0, 0.2]
        src = blocks.vector_source_f(np.tile(sym, 20).tolist(), False, 832)
        pr = eq_probe(rate_hz=1000.0, segments=48, eq_getter=lambda: fake_eq(), name="MER")
        out = msg_sink("dial", "symbols", "taps")
        tb = gr.top_block()
        tb.connect(src, pr)
        for p in out.got:
            tb.msg_connect(pr, p, out, p)
        tb.start()
        time.sleep(0.5)
        tb.stop()
        tb.wait()
        self.assertTrue(out.got["dial"])
        d = out.got["dial"][0]
        self.assertEqual(pmt.symbol_to_string(pmt.car(d)), "MER")
        self.assertAlmostEqual(pmt.to_double(pmt.cdr(d)), 10 * np.log10(21 / 0.09), delta=0.6)
        self.assertEqual(len(pmt.f32vector_elements(pmt.cdr(out.got["symbols"][0]))), 8 * 832)
        np.testing.assert_allclose(pmt.f32vector_elements(pmt.cdr(out.got["taps"][0])), [0.0, 1.0, 0.0, 0.2], atol=1e-6)

    def test_005_panel_is_a_class_paints_and_reports(self):
        from PyQt5 import QtWidgets
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        self.assertTrue(isinstance(atscplus.vsb_panel, type))
        self.assertTrue(isinstance(atscplus.vsb_panel, type))     # second access: still the class, not the module
        p = atscplus.vsb_panel(cliff_db=15.2)
        p.on_dial(pmt.cons(pmt.intern("MER"), pmt.from_double(18.5)))
        p.on_symbols(pmt.cons(pmt.PMT_NIL, pmt.init_f32vector(8, LEVELS)))
        p.on_taps(pmt.cons(pmt.PMT_NIL, pmt.init_f32vector(3, [0.0, 1.0, 0.1])))
        p.on_stats(pmt.to_pmt({"packets": 1000, "scrubbed": 3, "clean": 997, "clean_per_s": 12000.0}))
        p.resize(900, 300)
        p.show()
        p.refresh()
        app.processEvents()
        self.assertFalse(p.grab().isNull())
        self.assertIn("18.5 dB", p.totals.text())
        self.assertIn("eye OPEN", p.totals.text())
        self.assertIn("99.700%", p.totals.text())
        p.on_dial(pmt.cons(pmt.intern("MER"), pmt.from_double(12.0)))
        p.refresh()
        self.assertIn("eye closed", p.totals.text())
        p.timer.stop()
        p.close()

    def test_006_video_pane_gives_up_cleanly_on_a_missing_player(self):
        from PyQt5 import QtWidgets
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        v = atscplus.video_pane(player="no-such-player-xyz", start_delay_s=0.0, max_restarts=1)
        v.show()
        t = time.time()
        while time.time() - t < 3 and "cannot start" not in v.note.text():
            app.processEvents()
            time.sleep(0.05)
        self.assertIn("cannot start", v.note.text())
        self.assertIsNone(v.proc)
        v.watch.stop()
        v.close()


if __name__ == '__main__':
    gr_unittest.run(qa_python_blocks)
