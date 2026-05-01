#!/usr/bin/env python3
"""v8 decoder: tunable atsc_sync (lower lock threshold + always-emit).
Args: input.cs16 output.ts [min_lock] [unlock] [emit_when_unlocked]
"""
import sys
from math import gcd
from gnuradio import gr, blocks, dtv, analog
from gnuradio import filter as gr_filter
from gnuradio import atscplus
from gnuradio.dtv.atsc_rx_filter import atsc_rx_filter, ATSC_SYMBOL_RATE

IN  = sys.argv[1]
OUT = sys.argv[2]
MIN_LOCK = int(sys.argv[3]) if len(sys.argv) > 3 else 3
UNLOCK   = int(sys.argv[4]) if len(sys.argv) > 4 else 1
EMIT_UNL = bool(int(sys.argv[5])) if len(sys.argv) > 5 else True

INPUT_RATE = 8_000_000
TARGET_RATE = 6_250_000
SPS = 1.5
g = gcd(INPUT_RATE, TARGET_RATE)
INTERP, DECIM = TARGET_RATE // g, INPUT_RATE // g


class atsc_v8_tb(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "atsc_v8")

        src = blocks.file_source(gr.sizeof_short, IN, repeat=False)
        s2c = blocks.interleaved_short_to_complex()
        rs  = gr_filter.rational_resampler_ccc(interpolation=INTERP, decimation=DECIM)

        rx_filt = atsc_rx_filter(TARGET_RATE, SPS)
        output_rate = ATSC_SYMBOL_RATE * SPS
        pll = dtv.atsc_fpll(output_rate)
        dcr = gr_filter.dc_blocker_ff(4096)
        agc = analog.agc_ff(1e-5, 4.0)
        btl = atscplus.atsc_sync_tunable(output_rate, MIN_LOCK, UNLOCK, EMIT_UNL)  # ★
        fsc = atscplus.atsc_fs_checker_inst()
        equ = atscplus.atsc_equalizer_long()
        vit = atscplus.atsc_viterbi_soft()
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


tb = atsc_v8_tb()
tb.start()
tb.wait()
print(f"v8 done: min_lock={MIN_LOCK} unlock={UNLOCK} emit_unl={EMIT_UNL}", file=sys.stderr)
