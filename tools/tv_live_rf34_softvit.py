"""Live ATSC TV streamer — Tier 9 variant using atscplus.atsc_viterbi_soft.

Identical to tv_live_rf34.py but swaps stock dtv.atsc_viterbi_decoder()
for atscplus.atsc_viterbi_soft() to debug the soft Viterbi.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from gnuradio import gr, blocks, analog, dtv
from gnuradio import filter as gr_filter
from gnuradio import soapy
from gnuradio import network
from gnuradio import atscplus
from gnuradio.dtv.atsc_rx_filter import atsc_rx_filter, ATSC_SYMBOL_RATE

import numpy as np


class TEIScrub(gr.sync_block):
    def __init__(self):
        gr.sync_block.__init__(
            self, name="tei_scrub",
            in_sig=[(np.uint8, 188)],
            out_sig=[(np.uint8, 188)],
        )
        self._scrubbed = 0

    def work(self, input_items, output_items):
        in0 = input_items[0]
        out = output_items[0]
        bad = (in0[:, 1] & 0x80) != 0
        out[:] = in0
        if np.any(bad):
            null_pkt = np.full(188, 0xFF, dtype=np.uint8)
            null_pkt[0] = 0x47
            null_pkt[1] = 0x1F
            null_pkt[2] = 0xFF
            null_pkt[3] = 0x10
            out[bad] = null_pkt
            self._scrubbed += int(np.sum(bad))
        return len(out)

from config import (DATA_DIR, ATSC_ANTENNA, ATSC_IF_GAIN_REDUCTION,
                     ATSC_RFGAIN_SEL, ATSC_LIVE_TCP_PORT,
                     ATSC_DEFAULT_RF_CHANNEL)

LOG = logging.getLogger("sdr_agent.tv_live_softvit")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

ATSC_NATIVE_SAMPLE_RATE = 8_000_000
ATSC_RX_SAMPLE_RATE     = 6_250_000
RESAMP_INTERP = 25
RESAMP_DECIM  = 32


def rf_to_freq_hz(rf: int) -> int:
    if 2 <= rf <= 4:
        return 57_000_000 + (rf - 2) * 6_000_000
    if 5 <= rf <= 6:
        return 79_000_000 + (rf - 5) * 6_000_000
    if 7 <= rf <= 13:
        return 177_000_000 + (rf - 7) * 6_000_000
    if 14 <= rf <= 36:
        return 473_000_000 + (rf - 14) * 6_000_000
    raise ValueError(f"Unsupported RF channel: {rf}")


class LiveTVTopBlock(gr.top_block):
    def __init__(self, rf_channel: int, ts_path: Path):
        super().__init__("tv_live_softvit")
        freq = rf_to_freq_hz(rf_channel)
        LOG.info(f"Tuning RF {rf_channel} = {freq/1e6:.3f} MHz "
                 f"(antenna={ATSC_ANTENNA}, IFGR={ATSC_IF_GAIN_REDUCTION}, "
                 f"rfgain_sel={ATSC_RFGAIN_SEL})")

        src = None
        last_err = None
        for attempt, settle in enumerate([0, 3, 6, 10], start=1):
            if settle:
                LOG.info(f"SDR busy; retry {attempt} after {settle}s")
                time.sleep(settle)
            try:
                src = soapy.source(
                    "driver=sdrplay", "fc32", 1, "", "",
                    [""], [""],
                )
                break
            except RuntimeError as e:
                last_err = e
                if "no available RSP" not in str(e):
                    raise
        if src is None:
            raise RuntimeError(f"SDR open gave up: {last_err}")
        src.set_sample_rate(0, ATSC_NATIVE_SAMPLE_RATE)
        src.set_frequency(0, freq)
        src.set_antenna(0, ATSC_ANTENNA)
        try:
            src.set_gain_mode(0, False)
        except Exception:
            pass
        try:
            src.set_gain(0, "IFGR", float(ATSC_IF_GAIN_REDUCTION))
        except Exception:
            src.set_gain(0, float(ATSC_IF_GAIN_REDUCTION))
        try:
            src.write_setting("rfgain_sel", str(ATSC_RFGAIN_SEL))
        except Exception:
            pass

        SPS         = 1.5
        output_rate = ATSC_SYMBOL_RATE * SPS

        scaler = blocks.multiply_const_cc(32768.0)
        resamp = gr_filter.rational_resampler_ccc(
            interpolation=RESAMP_INTERP, decimation=RESAMP_DECIM,
        )
        rxf  = atsc_rx_filter(ATSC_RX_SAMPLE_RATE, SPS)
        fpll = atscplus.atsc_fpll_tight(output_rate, 0.001, 50.0)
        dcr  = gr_filter.dc_blocker_ff(32)
        agc  = analog.agc_ff(1e-6, 4.0)
        sync = dtv.atsc_sync(output_rate)
        fs_check = atscplus.atsc_fs_checker_inst()
        equalizer = atscplus.atsc_equalizer_long()
        # ── TIER 9 SWAP: use the fork's soft Viterbi instead of stock ──
        viterbi = atscplus.atsc_viterbi_soft()
        deinterleaver = dtv.atsc_deinterleaver()
        rs = dtv.atsc_rs_decoder()
        derand = dtv.atsc_derandomizer()
        depad = dtv.atsc_depad()

        ts_file = blocks.file_sink(gr.sizeof_char, str(ts_path))
        ts_file.set_unbuffered(True)

        self.connect(src, scaler, resamp, rxf, fpll, dcr, agc, sync, fs_check)
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
    if sys.platform == "win32":
        try:
            import ctypes
            REALTIME_PRIORITY_CLASS = 0x100
            HIGH_PRIORITY_CLASS     = 0x80
            ok = ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(),
                REALTIME_PRIORITY_CLASS,
            )
            if ok:
                LOG.info("Process priority bumped to REALTIME")
            else:
                ctypes.windll.kernel32.SetPriorityClass(
                    ctypes.windll.kernel32.GetCurrentProcess(),
                    HIGH_PRIORITY_CLASS,
                )
                LOG.info("Process priority bumped to HIGH (REALTIME denied)")
        except Exception as e:
            LOG.warning(f"priority bump failed: {e}")

    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=ATSC_DEFAULT_RF_CHANNEL,
                     help="RF channel number (default 34)")
    ap.add_argument("--out", default=str(DATA_DIR / "tv_live" / "live_softvit.ts"))
    ap.add_argument("--rotate-gb", type=float, default=50.0)
    ap.add_argument("--seconds", type=float, default=0.0,
                     help="if >0, run for this many seconds then exit (test mode)")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(8):
        if not out.exists():
            break
        try:
            out.unlink()
            break
        except (PermissionError, OSError):
            time.sleep(0.5)

    LOG.info(f"Live TV (softvit) starting — RF {args.rf}")
    LOG.info(f"Writing TS to {out}")

    tb = LiveTVTopBlock(args.rf, out)

    def _stop(signum, frame):
        LOG.info("Stopping live TV flowgraph...")
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        signal.signal(signal.SIGBREAK, _stop)
    except AttributeError:
        pass

    tb.start()

    if args.seconds > 0:
        LOG.info(f"Test mode: will exit after {args.seconds:.0f}s")
        time.sleep(args.seconds)
        LOG.info("Test time elapsed; stopping.")
        tb.stop()
        tb.wait()
        return

    rotate_bytes = int(args.rotate_gb * 1e9)
    while True:
        time.sleep(10)
        try:
            sz = out.stat().st_size
            if sz > rotate_bytes:
                LOG.info(f"Rotating live.ts ({sz/1e9:.1f} GB)")
                with open(out, "rb+") as f:
                    f.truncate(0)
        except FileNotFoundError:
            continue


if __name__ == "__main__":
    main()
