#!/usr/bin/env python3
"""v1 decoder: same as gr-dtv's atsc_rx but with our 256-tap equalizer
from gr-atscplus replacing the stock 64-tap atsc_equalizer.

Args: input.cs16 output.ts
"""
import sys
from math import gcd
from gnuradio import gr, blocks, dtv, analog
from gnuradio import filter as gr_filter
from gnuradio import atscplus
from gnuradio.dtv.atsc_rx_filter import atsc_rx_filter, ATSC_SYMBOL_RATE

IN  = sys.argv[1]
OUT = sys.argv[2]
INPUT_RATE = 8_000_000   # baseline IQ is 8 MS/s
TARGET_RATE = 6_250_000  # what atsc_rx wants for sps=1.5
SPS = 1.5
g = gcd(INPUT_RATE, TARGET_RATE)
INTERP, DECIM = TARGET_RATE // g, INPUT_RATE // g


class atsc_long_tb(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "atsc_long")

        src = blocks.file_source(gr.sizeof_short, IN, repeat=False)
        s2c = blocks.interleaved_short_to_complex()
        rs  = gr_filter.rational_resampler_ccc(interpolation=INTERP, decimation=DECIM)

        # === atsc_rx pipeline replicated, with our long equalizer ===
        rx_filt = atsc_rx_filter(TARGET_RATE, SPS)
        output_rate = ATSC_SYMBOL_RATE * SPS
        pll = dtv.atsc_fpll(output_rate)
        dcr = gr_filter.dc_blocker_ff(4096)
        agc = analog.agc_ff(1e-5, 4.0)
        btl = dtv.atsc_sync(output_rate)
        fsc = dtv.atsc_fs_checker()
        equ = atscplus.atsc_equalizer_long()       # ★ 256-tap + DD-LMS fork ★
        vit = atscplus.atsc_viterbi_soft()         # ★ L2-metric soft Viterbi ★
        dei = dtv.atsc_deinterleaver()
        rsd = dtv.atsc_rs_decoder()
        der = dtv.atsc_derandomizer()
        dep = dtv.atsc_depad()
        sink = blocks.file_sink(gr.sizeof_char, OUT)

        self.connect(src, s2c, rs, rx_filt, pll, dcr, agc, btl, fsc)
        self.connect((fsc, 0), (equ, 0))
        self.connect((fsc, 1), (equ, 1))
        self.connect((equ, 0), (vit, 0))
        self.connect((equ, 1), (vit, 1))
        self.connect((vit, 0), (dei, 0))
        self.connect((vit, 1), (dei, 1))
        self.connect((dei, 0), (rsd, 0))
        self.connect((dei, 1), (rsd, 1))
        self.connect((rsd, 0), (der, 0))
        self.connect((rsd, 1), (der, 1))
        self.connect((der, 0), (dep, 0))
        self.connect(dep, sink)


tb = atsc_long_tb()
tb.start()
tb.wait()
print(f"v1 256-tap decode done: {IN} -> {OUT}")
