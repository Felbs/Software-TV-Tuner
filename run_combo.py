#!/usr/bin/env python3
"""Parameterized ATSC decoder — picks blocks by combo name from combos.yaml.

Usage:
    python run_combo.py <input.cs16> <output.ts> <combo_name>

The combo_name must match an entry in combos.yaml at repo root.
"""
import sys
import os
from math import gcd
from pathlib import Path

import yaml

from gnuradio import gr, blocks, dtv, analog
from gnuradio import filter as gr_filter
from gnuradio import atscplus
from gnuradio.dtv.atsc_rx_filter import atsc_rx_filter, ATSC_SYMBOL_RATE


def load_combo(combo_name: str) -> dict:
    here = Path(__file__).parent
    cfg = yaml.safe_load((here / "combos.yaml").read_text())
    for c in cfg["combos"]:
        if c["name"] == combo_name:
            return c
    raise SystemExit(f"unknown combo: {combo_name}; choices: "
                     + ", ".join(c["name"] for c in cfg["combos"]))


def build_pipeline(combo: dict, target_rate: float, sps: float):
    """Return (pll, btl, fsc, equ, vit) blocks for this combo."""
    output_rate = ATSC_SYMBOL_RATE * sps

    if combo["fpll"] == "tight":
        pll = atscplus.atsc_fpll_tight(output_rate,
                                       float(combo.get("fpll_alpha", 0.003)),
                                       float(combo.get("fpll_afc_tau_us", 20.0)))
    else:
        pll = dtv.atsc_fpll(output_rate)

    if combo["sync"] == "tunable":
        btl = atscplus.atsc_sync_tunable(
            output_rate,
            int(combo.get("sync_min_lock", 3)),
            int(combo.get("sync_unlock", 1)),
            bool(combo.get("sync_emit_unlocked", False)))
    else:
        btl = dtv.atsc_sync(output_rate)

    # FS checker — always use instrumented variant for stats
    fsc = atscplus.atsc_fs_checker_inst()

    if combo["eq"] == "long":
        equ = atscplus.atsc_equalizer_long()
    else:
        equ = dtv.atsc_equalizer()

    if combo["viterbi"] == "soft":
        vit = atscplus.atsc_viterbi_soft()
    else:
        vit = dtv.atsc_viterbi_decoder()

    return pll, btl, fsc, equ, vit


class atsc_combo_tb(gr.top_block):
    def __init__(self, in_path: str, out_path: str, combo: dict,
                 input_rate: int = 8_000_000, target_rate: int = 6_250_000,
                 sps: float = 1.5):
        gr.top_block.__init__(self, f"atsc_combo_{combo['name']}")
        g = gcd(input_rate, target_rate)
        interp, decim = target_rate // g, input_rate // g

        src = blocks.file_source(gr.sizeof_short, in_path, repeat=False)
        s2c = blocks.interleaved_short_to_complex()
        rs  = gr_filter.rational_resampler_ccc(interpolation=interp, decimation=decim)
        rx_filt = atsc_rx_filter(target_rate, sps)
        pll, btl, fsc, equ, vit = build_pipeline(combo, target_rate, sps)
        dcr = gr_filter.dc_blocker_ff(4096)
        agc = analog.agc_ff(1e-5, 4.0)
        dei = dtv.atsc_deinterleaver()
        rsd = dtv.atsc_rs_decoder()
        der = dtv.atsc_derandomizer()
        dep = dtv.atsc_depad()
        sink = blocks.file_sink(gr.sizeof_char, out_path)

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


def main():
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    in_path, out_path, combo_name = sys.argv[1], sys.argv[2], sys.argv[3]
    combo = load_combo(combo_name)
    print(f"[run_combo] {combo_name}: {combo.get('description', '')}", file=sys.stderr)
    tb = atsc_combo_tb(in_path, out_path, combo)
    tb.start()
    tb.wait()
    print(f"[run_combo] {combo_name} done", file=sys.stderr)


if __name__ == "__main__":
    main()
