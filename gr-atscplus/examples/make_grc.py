#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writes the ATSC 1.0 example flowgraphs from one description, so they cannot drift.

  atsc1_replay.grc    headless: an 8 MS/s cf32 capture in, transport stream to a file (the
                      byte-for-byte check against tools/tv_live.py / tv_replay.py)
  atsc1_live_qt.grc   Qt: a SoapySDR radio in; spectrum, the 8-VSB panel (eye, echoes, MER),
                      the picture in the window (mpv); transport stream on UDP and to a file

THE CHAIN IS THE PRODUCTION CHAIN, block for block: tools/tv_live.py with the STVT_* defaults
the day-to-day tools use (adaptive-tv/chain_lab.py BASE_ENV): 8 MS/s -> x25/32 -> 6.25 MS/s ->
stock RRC matched filter at 1.1 samples/symbol -> ATSC+ FPLL (tight, DC block + AGC folded in)
-> ATSC+ Sync (soft) -> ATSC+ FS Checker -> ATSC+ Equalizer (long) -> ATSC+ Viterbi (soft) ->
stock deinterleaver -> stock RS decoder -> derandomizer -> depad -> TEI scrub.
The one process-wide knob that block has no parameter for, STVT_FPLL_FOLD=1, is set by the
flowgraph's Import block. Nothing about any station is in the files: channel, antenna and
gains are Parameters."""
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYMBOL_RATE = "4.5e6/286*684"           # 10.76238 MS/s, as gr-dtv defines it


def blk(name_, id_, x, y, **params):
    return {"name": name_, "id": id_, "parameters": {k: str(v) for k, v in params.items()},
            "states": {"bus_sink": False, "bus_source": False, "bus_structure": None,
                       "coordinate": [x, y], "rotation": 0, "state": "enabled"}}


def options(name, title, qt, desc):
    return {"parameters": {"id": name, "title": title, "author": "gr-atscplus",
                           "generate_options": "qt_gui" if qt else "no_gui", "output_language": "python",
                           "category": "[GRC Hier Blocks]", "run": "True",
                           "run_options": "prompt" if qt else "run", "gen_cmake": "On", "description": desc},
            "states": {"bus_sink": False, "bus_source": False, "bus_structure": None,
                       "coordinate": [8, 8], "rotation": 0, "state": "enabled"}}


def chain(y):
    """The receiver: identical in both flowgraphs. Returns (blocks, connections)."""
    b = [
        blk("fold", "import", 230, 100, imports="import os; os.environ.setdefault('STVT_FPLL_FOLD', '1')",
            comment="production setting: DC block + AGC folded into the FPLL"),
        blk("scale", "blocks_multiply_const_vxx", 8, y + 220, type="complex", const=32768.0,
            comment="the chain was tuned on int16-scale samples"),
        blk("resamp", "rational_resampler_xxx", 200, y + 200, type="ccc", interp=25, decim=32, taps="[]",
            fbw=0, comment="8 MS/s -> 6.25 MS/s"),
        blk("rxf", "dtv_atsc_rx_filter", 420, y + 220, rate="6.25e6", sps="sps",
            comment="RRC matched filter; out at sps x symbol rate"),
        blk("fpll", "atscplus_atsc_fpll_tight", 640, y + 200, rate="out_rate", alpha=0.001, afc_tau_us=25),
        blk("sync", "atscplus_atsc_sync_soft", 900, y + 220, rate="out_rate"),
        blk("fsc", "atscplus_atsc_fs_checker_inst", 1120, y + 220),
        blk("eq", "atscplus_atsc_equalizer_long", 8, y + 420),
        blk("vit", "atscplus_atsc_viterbi_soft", 280, y + 420),
        blk("dei", "dtv_atsc_deinterleaver", 520, y + 420),
        blk("rs", "dtv_atsc_rs_decoder", 740, y + 420),
        blk("derand", "dtv_atsc_derandomizer", 960, y + 420),
        blk("depad", "dtv_atsc_depad", 1180, y + 430),
        blk("s2v", "blocks_stream_to_vector", 8, y + 760, type="byte", num_items=188, vlen=1),
        blk("tei", "atscplus_tei_scrub", 220, y + 750, scrub="True", report_s=1.0,
            comment="uncorrectable packets -> NULL; the liveness count"),
        blk("v2s", "blocks_vector_to_stream", 470, y + 760, type="byte", num_items=188, vlen=1),
    ]
    c = [["scale", "0", "resamp", "0"], ["resamp", "0", "rxf", "0"], ["rxf", "0", "fpll", "0"],
         ["fpll", "0", "sync", "0"], ["sync", "0", "fsc", "0"],
         ["fsc", "0", "eq", "0"], ["fsc", "1", "eq", "1"],
         ["eq", "0", "vit", "0"], ["eq", "1", "vit", "1"],
         ["vit", "0", "dei", "0"], ["vit", "1", "dei", "1"],
         ["dei", "0", "rs", "0"], ["dei", "1", "rs", "1"],
         ["rs", "0", "derand", "0"], ["rs", "1", "derand", "1"],
         ["derand", "0", "depad", "0"], ["depad", "0", "s2v", "0"], ["s2v", "0", "tei", "0"],
         ["tei", "0", "v2s", "0"]]
    return b, c


def variables():
    return [
        blk("sps", "variable", 230, 12, value="1.1", comment="samples per symbol into the sync (production: 1.1)"),
        blk("out_rate", "variable", 370, 12, value=f"({SYMBOL_RATE})*sps"),
    ]


def build_replay():
    y = 100
    b = variables() + [
        blk("capture", "parameter", 560, 12, label="Capture (.cf32, 8 MS/s)", type="str", value='"capture.cf32"',
            short_id="c"),
        blk("out", "parameter", 800, 12, label="Transport stream out", type="str", value='"out.ts"', short_id="o"),
        blk("src", "blocks_file_source", 8, y + 100, type="complex", file="capture", repeat="False", vlen=1,
            begin_tag="pmt.PMT_NIL", offset=0, length=0),
        blk("ts", "blocks_file_sink", 700, y + 760, type="byte", file="out", unbuffered="False", append="False",
            vlen=1),
    ]
    cb, cc = chain(y)
    c = [["src", "0", "scale", "0"], ["v2s", "0", "ts", "0"]] + cc
    return {"options": options("atsc1_replay", "ATSC 1.0 (8-VSB) receiver: capture to transport stream", False,
                               "The production STVT chain, block for block, on a capture file."),
            "blocks": b + cb, "connections": c,
            "metadata": {"file_format": 1, "grc_version": "3.10.12.0"}}, "atsc1_replay"


def build_live():
    y = 380
    b = variables() + [
        blk("freq", "parameter", 560, 12, label="Channel centre (Hz)", type="eng_float", value="600e6", short_id="f"),
        blk("driver", "parameter", 740, 12, label="SoapySDR driver", type="str", value='"sdrplay"', short_id="d"),
        blk("antenna", "parameter", 920, 12, label="Antenna port", type="str", value='""', short_id="a"),
        blk("gain", "parameter", 1080, 12, label="Overall gain (dB)", type="eng_float", value="30", short_id="g",
            comment="or the gain elements, from gr-rxtune"),
        blk("ts_out", "parameter", 1260, 12, label="Also record the TS to", type="str", value='""', short_id="o"),
        blk("udp_port", "parameter", 1260, 110, label="UDP port for the player", type="intx", value="5004",
            short_id="p"),
        blk("src", "soapy_custom_source", 8, y + 80, driver="driver", type="fc32", nchan=1, dev_args='""',
            samp_rate="8e6", center_freq0="freq", bandwidth0="8e6", antenna0="antenna", gain0="gain", agc0=False,
            minoutbuf=str(1 << 22), comment="8 MS/s, as the production chain captures"),
        blk("spectrum", "qtgui_freq_sink_x", 330, y - 300, type="complex", name='"channel (baseband)"',
            fftsize=2048, fc=0, bw="8e6", average=0.05, gui_hint="0,0,1,1"),
        blk("probe", "atscplus_eq_probe", 8, y + 560, rate_hz=5, segments=48, eq_getter="lambda: self.eq",
            name='"MER"', comment="dial for gr-rxtune + the panel"),
        blk("panel", "atscplus_vsb_panel", 330, y + 560, label='"ATSC 1.0 / 8-VSB"', cliff_db=15.2,
            gui_hint="1,0,1,1"),
        blk("udp", "network_udp_sink", 700, y + 740, type="byte", addr='"127.0.0.1"', port="udp_port",
            header="0", payloadsize=1316, send_eof="False", vlen=1, comment="7 TS packets per datagram"),
        blk("video", "atscplus_video_pane", 700, y + 560, url='"udp://127.0.0.1:"+str(udp_port)+"?fifo_size=1000000&overrun_nonfatal=1"',
            player='"mpv"', vid=0, aid=0, player_args='""', start_delay_s=4, gui_hint="0,1,2,1"),
        blk("ts", "blocks_file_sink", 980, y + 740, type="byte", file="ts_out", unbuffered="False", append="False",
            vlen=1, comment="empty name = no file"),
    ]
    cb, cc = chain(y)
    c = [["src", "0", "scale", "0"], ["src", "0", "spectrum", "0"], ["eq", "0", "probe", "0"],
         ["probe", "dial", "panel", "dial"], ["probe", "symbols", "panel", "symbols"],
         ["probe", "taps", "panel", "taps"], ["tei", "stats", "panel", "stats"],
         ["v2s", "0", "udp", "0"], ["v2s", "0", "ts", "0"]] + cc
    return {"options": options("atsc1_live_qt", "ATSC 1.0 (8-VSB) television in GNU Radio", True,
                               "The production STVT chain on a radio, with the picture in the window."),
            "blocks": b + cb, "connections": c,
            "metadata": {"file_format": 1, "grc_version": "3.10.12.0"}}, "atsc1_live_qt"


def main():
    for build in (build_replay, build_live):
        doc, name = build()
        path = os.path.join(ROOT, "examples", name + ".grc")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            yaml.safe_dump(doc, fh, sort_keys=False, default_flow_style=False)
        print("wrote", path)


if __name__ == "__main__":
    main()
