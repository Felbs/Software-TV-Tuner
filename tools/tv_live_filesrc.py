"""tv_live_filesrc — diagnostic sibling of tv_live.py.

Same flowgraph as tv_live.py, but the SDR source is replaced with a
blocks.file_source reading a captured CF32 IQ file. Used to determine
whether the Linux OsO drops are caused by real-time SDR I/O (would
disappear here) or by the flowgraph itself (would persist).

Usage:
    python3 tv_live_filesrc.py --iq /tmp/wetadata.cf32 \
        --out /tmp/live_filesrc.ts --log /tmp/tv_filesrc.log

Output paths default to /tmp/ to avoid clobbering the real live.ts.
"""
from __future__ import annotations
import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

from gnuradio import gr, blocks, analog, dtv
from gnuradio import filter as gr_filter
from gnuradio import atscplus
from gnuradio.dtv.atsc_rx_filter import atsc_rx_filter, ATSC_SYMBOL_RATE

ATSC_NATIVE_SAMPLE_RATE = 8_000_000
ATSC_RX_SAMPLE_RATE     = 6_250_000
RESAMP_INTERP = 25
RESAMP_DECIM  = 32


class FileTVTopBlock(gr.top_block):
    def __init__(self, iq_path: Path, ts_path: Path, repeat: bool = False):
        super().__init__("tv_live_filesrc")
        # File source — CF32 captured at 8 MS/s, identical scaling to
        # what soapy.source(fc32) produces (±1.0 normalized complex floats).
        src = blocks.file_source(gr.sizeof_gr_complex, str(iq_path), repeat)

        SPS         = 1.5
        output_rate = ATSC_SYMBOL_RATE * SPS

        # 2026-05-22: mirror tv_live.py's env-var-based block selection so
        # iq_sweep can A/B different chain configs. Previous hardcoded
        # topology used dtv.atsc_sync (worse alignment) + dtv.atsc_rs_decoder
        # only — silently ignoring STVT_EQ/VITERBI/RS/NB env vars.
        scaler = blocks.multiply_const_cc(32768.0)
        resamp = gr_filter.rational_resampler_ccc(
            interpolation=RESAMP_INTERP, decimation=RESAMP_DECIM,
        )
        rxf  = atsc_rx_filter(ATSC_RX_SAMPLE_RATE, SPS)
        _fpll_alpha   = float(os.environ.get("STVT_FPLL_ALPHA",   "0.003"))
        _fpll_afc_tau = float(os.environ.get("STVT_FPLL_AFC_TAU", "20.0"))
        fpll = atscplus.atsc_fpll_tight(output_rate, _fpll_alpha, _fpll_afc_tau)
        dcr  = gr_filter.dc_blocker_ff(32)
        agc  = analog.agc_ff(1e-6, 4.0)
        sync = atscplus.atsc_sync_soft(output_rate)
        fs_check = atscplus.atsc_fs_checker_inst()

        # Optional noise blanker (STVT_NB=1).
        nb = None
        if int(os.environ.get("STVT_NB", "0")):
            _nb_thresh = float(os.environ.get("STVT_NB_THRESHOLD",     "3.0"))
            _nb_blank  = int  (os.environ.get("STVT_NB_BLANK_SAMPLES", "8"))
            _nb_alpha  = float(os.environ.get("STVT_NB_ALPHA",         "1e-4"))
            nb = atscplus.atsc_noise_blanker(_nb_thresh, _nb_blank, _nb_alpha)

        _eq_name = os.environ.get("STVT_EQ", "long")
        if   _eq_name == "long":          equalizer = atscplus.atsc_equalizer_long()
        elif _eq_name == "pilot":         equalizer = atscplus.atsc_equalizer_pilot()
        elif _eq_name == "pilot_dd":      equalizer = atscplus.atsc_equalizer_pilot_dd()
        elif _eq_name == "pilot_dd_soft": equalizer = atscplus.atsc_equalizer_pilot_dd_soft()
        elif _eq_name == "cma":           equalizer = atscplus.atsc_equalizer_cma()
        elif _eq_name == "multifs":       equalizer = atscplus.atsc_equalizer_pilot_multifs()
        elif _eq_name == "multifs_dd":    equalizer = atscplus.atsc_equalizer_pilot_multifs_dd()
        elif _eq_name == "stock":         equalizer = dtv.atsc_equalizer()
        else: raise ValueError(f"Unknown STVT_EQ={_eq_name}")

        _vit_name = os.environ.get("STVT_VITERBI", "hard")
        if   _vit_name == "hard": viterbi = dtv.atsc_viterbi_decoder()
        elif _vit_name == "soft": viterbi = atscplus.atsc_viterbi_soft()
        else: raise ValueError(f"Unknown STVT_VITERBI={_vit_name}")

        deinterleaver = dtv.atsc_deinterleaver()

        _rs_kind = os.environ.get("STVT_RS", "stock")
        if _rs_kind == "erasure":
            _rs_eras = int(os.environ.get("STVT_RS_ERASURES", "14"))
            rs = atscplus.atsc_rs_decoder_erasure(_rs_eras)
        else:
            rs = dtv.atsc_rs_decoder()

        derand = dtv.atsc_derandomizer()
        depad = dtv.atsc_depad()

        ts_file = blocks.file_sink(gr.sizeof_char, str(ts_path))
        ts_file.set_unbuffered(True)

        # Same chain wiring as tv_live.py.
        if nb is None:
            self.connect(src, scaler, resamp, rxf, fpll, dcr, agc, sync, fs_check)
        else:
            self.connect(src, scaler, nb, resamp, rxf, fpll, dcr, agc, sync, fs_check)
        for blk_in, blk_out in [(fs_check, equalizer),
                                 (equalizer, viterbi),
                                 (viterbi, deinterleaver),
                                 (deinterleaver, rs),
                                 (rs, derand)]:
            self.connect((blk_in, 0), (blk_out, 0))
            self.connect((blk_in, 1), (blk_out, 1))
        self.connect(derand, depad)
        self.connect(depad, ts_file)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iq",  default="/tmp/wetadata.cf32",
                    help="Input CF32 IQ file (8 MS/s).")
    ap.add_argument("--out", default="/tmp/live_filesrc.ts",
                    help="Output MPEG-TS path.")
    ap.add_argument("--log", default="/tmp/tv_filesrc.log",
                    help="Where stdout/stderr (FPLL+OsO markers) gets written.")
    ap.add_argument("--repeat", action="store_true",
                    help="Loop the IQ file forever (default: stop at EOF).")
    args = ap.parse_args()

    # Redirect both Python logging and C++ stdout/stderr to the log file
    # so we capture FPLL prints and OsO markers from the C++ blocks.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    LOG = logging.getLogger("sdr_agent.tv_live_filesrc")

    iq = Path(args.iq).resolve()
    out = Path(args.out).resolve()
    log = Path(args.log).resolve()
    if not iq.exists():
        LOG.error(f"IQ file not found: {iq}")
        sys.exit(2)
    sz = iq.stat().st_size
    samples = sz // 8
    duration = samples / ATSC_NATIVE_SAMPLE_RATE
    LOG.info(f"IQ file: {iq} size={sz} samples={samples} ~{duration:.2f}s @ {ATSC_NATIVE_SAMPLE_RATE} Hz")
    LOG.info(f"Writing TS to {out}")
    LOG.info(f"Logging C++ markers to {log}")

    # Open log fd; redirect stdout/stderr at the FD level so dup2 catches
    # writes from the C++ side too (printf inside fpll_tight et al).
    log_fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)

    tb = FileTVTopBlock(iq, out, repeat=args.repeat)

    def _stop(signum, frame):
        print("[tv_live_filesrc] stop signal", flush=True)
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print(f"[tv_live_filesrc] starting flowgraph iq={iq}", flush=True)
    t0 = time.time()
    tb.start()
    tb.wait()  # blocks until file_source EOFs (or signal)
    elapsed = time.time() - t0
    print(f"[tv_live_filesrc] DONE elapsed={elapsed:.2f}s out_size={out.stat().st_size if out.exists() else 0}", flush=True)


if __name__ == "__main__":
    main()
