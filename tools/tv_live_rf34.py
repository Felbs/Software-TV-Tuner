"""Live ATSC TV streamer — RF34 by default, any RF chan via --rf.

Spawns a GR-soapy + gr-dtv flowgraph:
  RSPdx → soapy.source(antenna=A, ifgr=59, rfgain_sel=5)
        → atsc receiver chain (using champion combo fpll_a002_tau20)
        → MPEG-TS file_sink (live.ts) + TCP server sink on :5559

The dashboard's TV tab connects to /api/tv_live/stream which proxies
the TCP TS to the browser/VLC. Captures rotate at 1 GB so live.ts on
disk doesn't grow unbounded.
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

# ── Windows console UTF-8 ────────────────────────────────────
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from gnuradio import gr, blocks, analog, dtv
from gnuradio import filter as gr_filter
from gnuradio import soapy
from gnuradio import network
# Magic TV decoder fork — atsc_fpll_tight is the unlock for clean
# decode quality on marginal signals. Stock gr-dtv FPLL uses alpha=0.01
# which is too wide; the shipped combo is alpha=0.001 + AFC tau=50us.
from gnuradio import atscplus
from gnuradio.dtv.atsc_rx_filter import atsc_rx_filter, ATSC_SYMBOL_RATE

import numpy as np


# ── TEI scrub block ─────────────────────────────────────────────
# After RS decode, packets gr-dtv couldn't correct have transport_error_indicator
# (TEI) set in the TS header. Leaving them lets VLC see corrupt video PIDs and
# choke. Dropping them breaks MPEG-2 continuity counters. The middle path is to
# rewrite each TEI=1 packet to a NULL packet (PID 0x1FFF), which VLC silently
# discards while the bytestream keeps proper packet alignment.
#
# Per project_atsc_status.md (2026-04-28): "TEI scrub (rewrite to NULL, not drop)
# preserves continuity" was one of the unlock fixes for watchable playback.
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
        # Vectorize the TEI test: byte 1 bit 7 set means RS-uncorrectable.
        bad = (in0[:, 1] & 0x80) != 0
        out[:] = in0
        if np.any(bad):
            # Build a single canonical NULL packet and broadcast it to bad rows.
            # MPEG-TS NULL: 0x47 0x1F 0xFF 0x10 + 184 bytes of 0xFF.
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

LOG = logging.getLogger("sdr_agent.tv_live")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# Proven recipe (per memory project_atsc_status.md, validated 2026-04-28):
# Native 8 MS/s capture (only natively supported SDRplay rates are 5/6/7/8/10),
# software-resample to 6.25 MS/s via rational_resampler 25/32 to preserve
# alias-free filtering (driver-internal resample produces noise that floors
# RS-decode), then feed gr-dtv's high-level dtv.atsc_rx(6.25e6, 1.5).
ATSC_NATIVE_SAMPLE_RATE = 8_000_000   # native RSPdx capture rate
ATSC_RX_SAMPLE_RATE     = 6_250_000   # rate fed to atsc_rx
RESAMP_INTERP = 25
RESAMP_DECIM  = 32                    # 8M * 25/32 = 6.25M


def rf_to_freq_hz(rf: int) -> int:
    """US ATSC channel center frequency (excluding chan 37 which is radio
    astronomy). chan 14-36 = UHF; chan 7-13 = VHF-hi; 2-6 = VHF-lo."""
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
        super().__init__("tv_live")
        freq = rf_to_freq_hz(rf_channel)
        LOG.info(f"Tuning RF {rf_channel} = {freq/1e6:.3f} MHz "
                 f"(antenna={ATSC_ANTENNA}, IFGR={ATSC_IF_GAIN_REDUCTION}, "
                 f"rfgain_sel={ATSC_RFGAIN_SEL})")

        # SDR source — retry on "no available RSP devices" since SDRplay's
        # API service can take a few seconds to release after a prior process
        # exits, and the daemon's release window may not always overlap.
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
        # Disable AGC so manual IFGR/RFGR settings stick.
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

        # ── EXACT REPLICA OF run_combo.py fpll_a002_tau20 PIPELINE ──
        # The proven offline chain that gave clean decode:
        #   src -> rs(8M->6.25M) -> rx_filt -> fpll_tight -> dcr -> agc
        #   -> sync -> fs_check -> equ -> viterbi -> dei -> rs -> der -> dep
        # Critical pieces that were missing in the previous version:
        #   1. atsc_rx_filter (matched filter at rx side)
        #   2. dc_blocker_ff post-PLL
        #   3. agc_ff between dc-block and sync
        #   4. fpll runs at output_rate (16143357), NOT 6.25M
        SPS         = 1.5
        output_rate = ATSC_SYMBOL_RATE * SPS    # ~16,143,357 Hz

        # ── SCALING MATCH ──
        # run_combo.py's file_source(short) -> interleaved_short_to_complex
        # produces complex floats in ±32767 range (raw int16 cast). Our
        # soapy.source(fc32) produces ±1.0 normalized complex floats — a
        # 32,768x scale mismatch that destroys downstream AGC/FPLL/equalizer
        # behavior. Multiplying by 32768 makes the live samples bit-equivalent
        # to the offline chain's input (and AGC will trim per-block anyway).
        scaler = blocks.multiply_const_cc(32768.0)

        # Software resample 8 MS/s -> 6.25 MS/s with the proven 25/32 ratio.
        resamp = gr_filter.rational_resampler_ccc(
            interpolation=RESAMP_INTERP, decimation=RESAMP_DECIM,
        )
        # Front-end matched filter; outputs samples at output_rate (16.14 MS/s).
        rxf  = atsc_rx_filter(ATSC_RX_SAMPLE_RATE, SPS)
        # Tight-FPLL — the unlock. Operates at output_rate. Tightened
        # from (0.002, 20.0) to (0.001, 50.0) per Tier 7 finding: the
        # post-t=60s "equalizer drift" was actually FPLL margin loss.
        # 90s real-RF test: PAT=97 distinct=34 TEI=0% zero CC jumps.
        fpll = atscplus.atsc_fpll_tight(output_rate, 0.001, 50.0)
        # Smaller DC blocker (32 taps vs 4096) — cuts CPU significantly
        # while still removing residual DC at 16.14 MS/s.
        dcr  = gr_filter.dc_blocker_ff(32)
        # Slower AGC (1e-6) lets the long equalizer settle on stable amplitude.
        agc  = analog.agc_ff(1e-6, 4.0)
        sync = dtv.atsc_sync(output_rate)
        fs_check = atscplus.atsc_fs_checker_inst()
        # 256-tap LMS equalizer from the magic-tv fork — drops PAT count
        # from 2/14MB → 97/14MB, distinct PIDs 7841 → 29, TEI 100% → 0%.
        # Convergence is probabilistic (~1 in 3 cold-starts); see watchdog.
        equalizer = atscplus.atsc_equalizer_long()
        viterbi = dtv.atsc_viterbi_decoder()
        deinterleaver = dtv.atsc_deinterleaver()
        rs = dtv.atsc_rs_decoder()
        derand = dtv.atsc_derandomizer()
        depad = dtv.atsc_depad()

        # Sinks: file ONLY. The TCP sink we used to have back-pressured
        # the gr flowgraph when no client was connected, throttling the
        # whole chain to ~40% real-time. tv_hls.py reads from the file.
        # set_unbuffered(True) so writes flush to disk every packet —
        # otherwise tv_hls's chunker reads stale empty bytes (the OS
        # buffer holds 64KB+ of TS that hasn't reached disk yet).
        ts_file = blocks.file_sink(gr.sizeof_char, str(ts_path))
        ts_file.set_unbuffered(True)

        # Wire the proven run_combo.py topology end-to-end (with soapy.source
        # + scaler in place of file_source + s2c for live capture):
        #   src -> scaler(*32768) -> resamp -> rxf -> fpll -> dcr -> agc -> sync -> fs_check
        # fs_checker through rs_decoder carry TWO streams (data + tag);
        # derandomizer collapses to one, then depad emits raw TS bytes.
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
    # Windows: bump process priority so VLC + other apps don't preempt the
    # decoder (which sits at ~95% CPU on one core when locked). Without
    # this, brief OS scheduling delays cause sample drops -> ATSC sync
    # break -> RS produces TEI=1 packets -> VLC video stalls.
    if sys.platform == "win32":
        try:
            import ctypes
            # REALTIME (0x100) gives best scheduling but can starve other
            # apps; HIGH (0x80) is the fallback. Try REALTIME first; if it
            # fails (would need admin in some Windows configs), fall back.
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
    ap.add_argument("--out", default=str(DATA_DIR / "tv_live" / "live.ts"))
    # 50 GB ≈ 5-6 hours at ATSC bitrate. Won't rotate during a TV session;
    # avoids VLC seeing a truncation that would force it to seek back to
    # byte 0 mid-watch (looking like the show "restarted").
    ap.add_argument("--rotate-gb", type=float, default=50.0)
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Retry unlink — Windows holds the file handle for ~1-2s after a
    # prior tv_live exits, even though the process is gone.
    for _ in range(8):
        if not out.exists():
            break
        try:
            out.unlink()
            break
        except (PermissionError, OSError):
            time.sleep(0.5)
    # If we couldn't delete it, just write through — file_sink truncates.


    LOG.info(f"Live TV starting — RF {args.rf}, TCP port {ATSC_LIVE_TCP_PORT}")
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

    # File-rotation watchdog: when live.ts > rotate-gb, restart write
    rotate_bytes = int(args.rotate_gb * 1e9)
    while True:
        time.sleep(10)
        try:
            sz = out.stat().st_size
            if sz > rotate_bytes:
                LOG.info(f"Rotating live.ts ({sz/1e9:.1f} GB)")
                # GR's file_sink lacks a clean rotation API; safest is
                # truncate-on-disk while leaving the file handle open.
                # Better long-term: stop+start, but this is a pragmatic
                # bound for live viewing.
                with open(out, "rb+") as f:
                    f.truncate(0)
        except FileNotFoundError:
            continue


if __name__ == "__main__":
    main()
