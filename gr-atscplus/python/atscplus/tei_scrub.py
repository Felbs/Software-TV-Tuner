#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""TEI Scrub: replace RS-uncorrectable transport packets with NULL packets, and count.

A packet the Reed-Solomon decoder could not correct leaves with its Transport Error
Indicator set (byte 1, bit 7). Players choke on the garbage inside it; a NULL packet
(PID 0x1FFF) they simply skip. This is the block STVT's live chain has always run
(tools/tv_live.py, `TEIScrub`), moved here so a GRC flowgraph can use it, byte for byte.

It is also the receiver's honest LIVENESS signal: a count of packets that came out
clean cannot be faked by a locked-looking but dead chain."""
import time

import numpy as np
import pmt
from gnuradio import gr

NULL_PACKET = np.full(188, 0xFF, dtype=np.uint8)
NULL_PACKET[:4] = (0x47, 0x1F, 0xFF, 0x10)


class tei_scrub(gr.sync_block):
    """
    in / out: 188-byte transport packets (vector of bytes)
    message 'liveness': cumulative clean packets (long), about once a second
    message 'stats'   : {packets, scrubbed, clean, clean_per_s}
    """

    def __init__(self, scrub=True, report_s=1.0):
        gr.sync_block.__init__(self, name="atscplus_tei_scrub",
                               in_sig=[(np.uint8, 188)], out_sig=[(np.uint8, 188)])
        self.scrub = bool(scrub)
        self.report_s = float(report_s)
        self.n_packets = self.n_scrubbed = 0
        self._t = time.time()
        self._last_clean = 0
        self.message_port_register_out(pmt.intern("liveness"))
        self.message_port_register_out(pmt.intern("stats"))

    @property
    def n_clean(self):
        return self.n_packets - self.n_scrubbed

    def work(self, input_items, output_items):
        in0, out = input_items[0], output_items[0]
        bad = (in0[:, 1] & 0x80) != 0
        out[:] = in0
        n_bad = int(np.count_nonzero(bad))
        if n_bad and self.scrub:
            out[bad] = NULL_PACKET
        self.n_packets += len(in0)
        self.n_scrubbed += n_bad
        now = time.time()
        if now - self._t >= self.report_s:
            clean = self.n_clean
            rate = (clean - self._last_clean) / max(now - self._t, 1e-9)
            self._t, self._last_clean = now, clean
            self.message_port_pub(pmt.intern("liveness"), pmt.from_long(clean))
            self.message_port_pub(pmt.intern("stats"), pmt.to_pmt(
                {"packets": self.n_packets, "scrubbed": self.n_scrubbed, "clean": clean,
                 "clean_per_s": float(rate)}))
        return len(out)
