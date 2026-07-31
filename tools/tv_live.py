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
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# ── Windows console UTF-8 ────────────────────────────────────
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── Windows: surface SDRplay API DLL to radioconda ───────────
# The SDRplay vendor installer puts sdrplay_api.dll at C:\Program Files\
# SDRplay\API\x64\ but radioconda's env doesn't inherit that PATH entry,
# so SoapySDR's sdrPlaySupport.dll fails to load. Both PATH (for child
# procs) and add_dll_directory (for LoadLibrary in this proc) are needed.
if sys.platform == "win32":
    _sdrplay_api_x64 = r"C:\Program Files\SDRplay\API\x64"
    if os.path.isdir(_sdrplay_api_x64):
        os.environ["PATH"] = _sdrplay_api_x64 + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(_sdrplay_api_x64)
        except (AttributeError, OSError):
            pass

from gnuradio import gr, blocks, analog, dtv
from gnuradio import filter as gr_filter
from gnuradio import soapy
from gnuradio import network
# gr-atscplus fork — atsc_fpll_tight is the unlock for clean
# decode quality on marginal signals. Stock gr-dtv FPLL uses alpha=0.01
# which is too wide; the shipped combo is alpha=0.001 + AFC tau=50us.
from gnuradio import atscplus
from gnuradio.dtv.atsc_rx_filter import atsc_rx_filter, ATSC_SYMBOL_RATE
from gnuradio.filter import firdes


def make_rx_filter(input_rate, sps):
    """Tunable matched RRC filter (replica of dtv.atsc_rx_filter with a
    shorter RRC span). STVT_RRC_SYMS (default 8 = stock) sets the half-span;
    fewer symbols = fewer taps = less work in the chain's single-threaded
    bottleneck. Offline replay confirmed decode stays 100% clean down to ~3
    symbols. See memory live_drought_is_oso_not_rf."""
    rrc_syms = int(os.environ.get("STVT_RRC_SYMS", "8"))
    nfilts = int(os.environ.get("STVT_RX_NFILTS", "16"))
    output_rate = ATSC_SYMBOL_RATE * sps
    filter_rate = input_rate * nfilts
    symbol_rate = ATSC_SYMBOL_RATE / 2.0
    excess_bw = 0.1152
    ntaps = int((2 * rrc_syms + 1) * sps * nfilts)
    interp = output_rate / input_rate
    gain = nfilts * symbol_rate / filter_rate
    rrc_taps = firdes.root_raised_cosine(gain, filter_rate, symbol_rate,
                                         excess_bw, ntaps)
    LOG.info(f"rx_filter: rrc_syms={rrc_syms} nfilts={nfilts} ntaps={ntaps}")
    return gr_filter.pfb_arb_resampler_ccf(interp, rrc_taps, nfilts)


def make_fused_rx_filter(native_rate, sps):
    """FUSED resampler + matched filter (STVT_RXF_FUSED=1).

    Replaces TWO blocks — the rational_resampler (native 8M -> 6.25M) AND the
    arbitrary pfb_arb_resampler matched filter (6.25M -> sps*symbol) — with ONE
    fixed-ratio rational_resampler that goes native_rate -> output_rate directly
    while carrying the RRC matched-filter taps. The arbitrary resampler was the
    chain's hottest thread (~90% of a core, profiled); a fixed P/Q polyphase has
    no per-sample phase computation and drops the redundant 6.25M round-trip.

    Returns (block, actual_output_rate). The output rate is native*P/Q, which is
    ~sps*symbol for the chosen P/Q (37/25 at 8 MS/s -> 11.84 MS/s, sps~1.10).
    """
    from fractions import Fraction
    rrc_syms = int(os.environ.get("STVT_RRC_SYMS", "8"))
    target_out  = ATSC_SYMBOL_RATE * sps
    frac        = Fraction(target_out / native_rate).limit_denominator(64)
    interp, decim = frac.numerator, frac.denominator
    out_rate    = native_rate * interp / decim
    proto_rate  = native_rate * interp            # polyphase prototype rate
    symbol_rate = ATSC_SYMBOL_RATE / 2.0          # mirror make_rx_filter
    excess_bw   = 0.1152
    ntaps       = int((2 * rrc_syms + 1) * (proto_rate / symbol_rate))
    # The fused filter must hand the FPLL roughly the SAME amplitude the proven
    # two-stage chain did (in_rms ~33), or the FPLL's loop is under-driven and
    # decode goes marginal -> drought. The bare RRC gain undershoots ~2x, so
    # apply a matching multiplier (tune via STVT_RXF_FUSED_GAIN if in_rms is off).
    gmul        = float(os.environ.get("STVT_RXF_FUSED_GAIN", "1.9"))
    gain        = gmul * interp * symbol_rate / proto_rate
    rrc_taps    = firdes.root_raised_cosine(gain, proto_rate, symbol_rate,
                                            excess_bw, ntaps)
    LOG.info(f"fused_rx_filter: {interp}/{decim} native={native_rate:.0f} "
             f"out={out_rate:.0f} sps_eff={out_rate/ATSC_SYMBOL_RATE:.4f} "
             f"rrc_syms={rrc_syms} ntaps={ntaps} gain_mul={gmul}")
    return (gr_filter.rational_resampler_ccc(interpolation=interp,
                                             decimation=decim, taps=rrc_taps),
            out_rate)

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
    def __init__(self, rf_channel: int, ts_path: Path,
                 soapy_args: str = "driver=sdrplay",
                 stream_args: str = ""):
        super().__init__("tv_live")
        freq = rf_to_freq_hz(rf_channel)
        LOG.info(f"Tuning RF {rf_channel} = {freq/1e6:.3f} MHz "
                 f"(antenna={ATSC_ANTENNA}, IFGR={ATSC_IF_GAIN_REDUCTION}, "
                 f"rfgain_sel={ATSC_RFGAIN_SEL})")
        LOG.info(f"SoapySDR device: {soapy_args}")
        if stream_args:
            LOG.info(f"SoapySDR stream args: {stream_args}")

        # SDR source — retry on "no available RSP devices" since SDRplay's
        # API service can take a few seconds to release after a prior process
        # exits, and the daemon's release window may not always overlap.
        # Measured 2026-07-29: after a prior tv_live exits, the SDRplay API
        # service can hold the device for well over 20 s (back-to-back A/B
        # runs failed reproducibly on the SECOND decode with the old
        # 0/3/6/10 ladder = 19 s of patience). Wait a full minute, then try
        # a service restart once as the documented cure before giving up.
        src = None
        last_err = None
        settles = [0, 3, 6, 10, 15, 15, 15]
        for attempt, settle in enumerate(settles, start=1):
            if settle:
                LOG.info(f"SDR busy; retry {attempt} after {settle}s")
                time.sleep(settle)
            try:
                src = soapy.source(
                    soapy_args, "fc32", 1, "", stream_args,
                    [""], [""],
                )
                break
            except RuntimeError as e:
                last_err = e
                if "no available RSP" not in str(e):
                    raise
        if src is None and "sdrplay" in (soapy_args or "").lower():
            LOG.info("SDR still busy after ~64s; restarting SDRplay API "
                     "service (the documented cure) and retrying once")
            try:
                # PLATFORM LAYER (fixed 2026-07-31): this fallback shipped
                # PowerShell-only and therefore CRASHED on Linux with
                # "[Errno 2] No such file or directory: 'powershell'" — the
                # documented cure turned into a confusing traceback on the
                # one rig most likely to need it. Caught on radiopi2 after an
                # overnight run: the chain hit SDR contention, tried to
                # self-heal, and died here instead. Same cure, right command
                # per OS.
                if sys.platform == "win32":
                    _cure = ["powershell", "-NoProfile", "-Command",
                             "Restart-Service -Name SDRplayAPIService "
                             "-Force -Confirm:$false"]
                else:
                    _cure = ["sudo", "-n", "systemctl", "restart", "sdrplay"]
                _r = subprocess.run(_cure, capture_output=True, timeout=60)
                if _r.returncode != 0:
                    LOG.info("service-restart cure failed rc=%s: %s",
                             _r.returncode,
                             (_r.stderr or b"")[:200].decode(errors="replace"))
                time.sleep(10)
                src = soapy.source(soapy_args, "fc32", 1, "", stream_args,
                                   [""], [""])
            except Exception as e:
                last_err = e
        if src is None:
            raise RuntimeError(f"SDR open gave up: {last_err}")
        # Gain knobs: STVT_IFGR, STVT_RFGAIN_SEL, STVT_ANTENNA env vars override config defaults.
        _ifgr    = int(os.environ.get("STVT_IFGR",        ATSC_IF_GAIN_REDUCTION))
        _rfgsel  = int(os.environ.get("STVT_RFGAIN_SEL",  ATSC_RFGAIN_SEL))
        _antenna = os.environ.get("STVT_ANTENNA",         ATSC_ANTENNA)
        LOG.info(f"gain knobs: IFGR={_ifgr} rfgain_sel={_rfgsel} antenna={_antenna}")

        src.set_sample_rate(0, ATSC_NATIVE_SAMPLE_RATE)
        src.set_frequency(0, freq)
        try:
            src.set_antenna(0, _antenna)
        except Exception as exc:
            # single-antenna radios (Pluto: only A_BALANCED) reject the
            # RSPdx port names — not fatal, the radio uses what it has
            LOG.info(f"antenna '{_antenna}' not settable on this radio "
                     f"({exc!r}); using its default")
        # AGC: default OFF (manual gain via IFGR/RFGR), but STVT_SDR_AGC=1
        # enables the SDR's internal AGC. Useful when RF level drifts and
        # samples clip faster than manual gain can compensate. Observed
        # 2026-05-22 with max|x|=1.57 clips ~50s after lock, killing chain.
        # Hardware AGC adapts in microseconds.
        _sdr_agc = int(os.environ.get("STVT_SDR_AGC", "0"))
        try:
            src.set_gain_mode(0, bool(_sdr_agc))
        except Exception:
            pass
        # STVT_AGC_SETPOINT (dBFS, default driver -30): the one calibrated
        # invariant in AGC mode. -30 levels ATSC thin (in_rms ~50, error-prone);
        # raising toward -20 restores headroom while the servo still rides drift.
        if _sdr_agc and os.environ.get("STVT_AGC_SETPOINT"):
            try:
                src.write_setting("agc_setpoint",
                                  os.environ["STVT_AGC_SETPOINT"])
                LOG.info(f"agc_setpoint={os.environ['STVT_AGC_SETPOINT']}")
            except Exception:
                pass
        if "sdrplay" in (soapy_args or "").lower():
            try:
                src.set_gain(0, "IFGR", float(_ifgr))
            except Exception:
                src.set_gain(0, float(_ifgr))
            try:
                src.write_setting("rfgain_sel", str(_rfgsel))
            except Exception:
                pass
        else:
            # non-SDRplay radio: IFGR is a *reduction* (20..59, lower =
            # hotter) — map its hotness onto a plain gain figure instead
            # of feeding the raw number to a driver that reads it as dB
            # of gain. Starting point only; the tuners hill-climb.
            hot = max(0.0, min(1.0, (59.0 - float(_ifgr)) / 39.0))
            try:
                rng = src.get_gain_range(0)
                lo, hi = float(rng.start()), float(rng.stop())
            except Exception:
                lo, hi = 0.0, 70.0
            g = lo + hot * (hi - lo)
            try:
                src.set_gain(0, g)
                LOG.info(f"generic radio gain {g:.1f} dB "
                         f"(range {lo:.0f}..{hi:.0f}, from IFGR {_ifgr})")
            except Exception as exc:
                LOG.info(f"gain not settable ({exc!r}); driver default")

        # 2026-05-22 Day 16: expose RSPdx-specific SoapySDR tunables. These
        # are no-ops on RSP1/RSP2 (write_setting just fails silently). All
        # default to "leave alone" — opt-in via env vars.
        #
        #   STVT_RFNOTCH=1   enable RF notch (FM/AM rejection). Harmless
        #                    for UHF ATSC (FM is 88-108 MHz, far away),
        #                    but can suppress intermod from strong nearby
        #                    FM broadcasts. Worth trying on marginal lock.
        #   STVT_DABNOTCH=1  enable DAB-band notch (174-240 MHz). Not
        #                    useful for UHF ATSC unless you have a strong
        #                    DAB transmitter near the antenna.
        #   STVT_IQCORR=1    enable IQ correction (DC offset + imbalance).
        #                    Generally improves dynamic range; cost is
        #                    a tiny CPU bump. Try ON if image rejection
        #                    issues are suspected.
        #   STVT_BIAST=1     enable bias-T (only for active antennas).
        #
        for env_key, soapy_key in [
            ("STVT_RFNOTCH",  "rfnotch_ctrl"),
            ("STVT_DABNOTCH", "dabnotch_ctrl"),
            ("STVT_IQCORR",   "iqcorr_ctrl"),
            ("STVT_BIAST",    "biasT_ctrl"),
        ]:
            v = os.environ.get(env_key)
            if v is not None:
                try:
                    src.write_setting(soapy_key, "true" if v == "1" else "false")
                    LOG.info(f"soapy: {soapy_key}={'true' if v == '1' else 'false'} ({env_key})")
                except Exception as exc:
                    LOG.info(f"soapy: {soapy_key} write failed ({exc!r})")

        # PERSISTENT-RADIO RETUNE (2026-07-10): keep refs so retune() can
        # re-point the running source without tearing down the flowgraph.
        self._src = src
        self._rf = rf_channel

        # ── EXACT REPLICA OF run_combo.py fpll_a002_tau20 PIPELINE ──
        # The proven offline chain that gave clean decode:
        #   src -> rs(8M->6.25M) -> rx_filt -> fpll_tight -> dcr -> agc
        #   -> sync -> fs_check -> equ -> viterbi -> dei -> rs -> der -> dep
        # Critical pieces that were missing in the previous version:
        #   1. atsc_rx_filter (matched filter at rx side)
        #   2. dc_blocker_ff post-PLL
        #   3. agc_ff between dc-block and sync
        #   4. fpll runs at output_rate (16143357), NOT 6.25M
        # SPS = internal oversampling. 1.5 = stock (16.14 MS/s). Lowering it
        # cuts the matched-filter/resampler + all front-end blocks ~proportionally
        # — the real-time throughput lever for OsO. STVT_SPS overrides. Offline
        # replay confirmed decode stays 100% clean down to ~1.0 (align tapers
        # below ~1.1). See memory live_drought_is_oso_not_rf.
        SPS         = float(os.environ.get("STVT_SPS", "1.5"))
        output_rate = ATSC_SYMBOL_RATE * SPS    # ~16,143,357 Hz at SPS=1.5
        LOG.info(f"SPS={SPS} output_rate={output_rate:.0f}")

        # ── SCALING MATCH ──
        # run_combo.py's file_source(short) -> interleaved_short_to_complex
        # produces complex floats in ±32767 range (raw int16 cast). Our
        # soapy.source(fc32) produces ±1.0 normalized complex floats — a
        # 32,768x scale mismatch that destroys downstream AGC/FPLL/equalizer
        # behavior. Multiplying by 32768 makes the live samples bit-equivalent
        # to the offline chain's input (and AGC will trim per-block anyway).
        scaler = blocks.multiply_const_cc(32768.0)

        # 2026-05-22 Day 18: optional impulse noise blanker BEFORE resamp.
        # Enable with STVT_NB=1 (default off). Clips samples above
        # STVT_NB_THRESHOLD × running |sample| EMA, blanks STVT_NB_BLANK_SAMPLES
        # consecutive samples per trigger. Helps with impulse interference
        # (lightning, ignition, electrical arcing) that would otherwise saturate
        # the matched filter / equalizer.
        nb = None
        if int(os.environ.get("STVT_NB", "0")):
            _nb_thresh = float(os.environ.get("STVT_NB_THRESHOLD",     "3.0"))
            _nb_blank  = int  (os.environ.get("STVT_NB_BLANK_SAMPLES", "8"))
            _nb_alpha  = float(os.environ.get("STVT_NB_ALPHA",         "1e-4"))
            nb = atscplus.atsc_noise_blanker(_nb_thresh, _nb_blank, _nb_alpha)
            LOG.info(f"noise_blanker: ENABLED thresh={_nb_thresh} "
                     f"blank_samples={_nb_blank} alpha={_nb_alpha}")
        else:
            LOG.info("noise_blanker: disabled (STVT_NB=0)")

        # Software resample to 6.25 MS/s. If STVT_SKIP_RESAMP=1, the SDR
        # is already running at the right rate (set STVT_NATIVE_RATE=6250000)
        # and we omit the resampler entirely — saves a CPU stage that was
        # contributing to OsO sample-overflow bursts.
        if int(os.environ.get("STVT_SKIP_RESAMP", "0")):
            resamp = None
            LOG.info("resampler: SKIPPED (STVT_SKIP_RESAMP=1)")
        else:
            resamp = gr_filter.rational_resampler_ccc(
                interpolation=RESAMP_INTERP, decimation=RESAMP_DECIM,
            )
            LOG.info(f"resampler: {RESAMP_INTERP}/{RESAMP_DECIM}")
        # 2026-05-29 SPECTRAL SMOOTHER — FFT-domain outlier suppression.
        # Per-block (no IIR transients), suppresses bins that are >K×
        # the local-median neighborhood. Smooth proportional pull-down
        # (no flapping). Same chain position as the (disabled) adaptive
        # notch. Disabled by default (STVT_SPECTRAL=0).
        smoother = None
        if int(os.environ.get("STVT_SPECTRAL", "0")):
            _sm_fft   = int  (os.environ.get("STVT_SPECTRAL_FFT",          "1024"))
            _sm_nbr   = int  (os.environ.get("STVT_SPECTRAL_NEIGHBORHOOD", "32"))
            _sm_thr   = float(os.environ.get("STVT_SPECTRAL_THRESH",       "3.0"))
            smoother = atscplus.atsc_spectral_smoother(
                ATSC_RX_SAMPLE_RATE, _sm_fft, _sm_nbr, _sm_thr)
            LOG.info(f"spectral_smoother: ENABLED fft={_sm_fft} "
                     f"neighborhood={_sm_nbr} thresh={_sm_thr}")
        else:
            LOG.info("spectral_smoother: disabled (STVT_SPECTRAL=0)")

        # 2026-05-29 ADAPTIVE NOTCH — narrowband interferer suppression.
        # Inserted between the resampler and the matched filter so it runs
        # at the chain's working sample rate (ATSC_RX_SAMPLE_RATE = 6.25 MS/s)
        # where the pilot offset and ATSC band edges are well-defined.
        # Disabled by default (STVT_NOTCH=0); when enabled, periodically
        # FFTs the input, finds the strongest non-pilot peak, and notches
        # it with a sharp complex IIR.
        notch = None
        if int(os.environ.get("STVT_NOTCH", "0")):
            _notch_fft    = int  (os.environ.get("STVT_NOTCH_FFT",         "1024"))
            _notch_thresh = float(os.environ.get("STVT_NOTCH_THRESH_DB",   "12.0"))
            _notch_r      = float(os.environ.get("STVT_NOTCH_R",           "0.985"))
            _notch_pilot  = float(os.environ.get("STVT_NOTCH_PILOT_HZ",   "-2.69e6"))
            _notch_guard  = float(os.environ.get("STVT_NOTCH_GUARD_HZ",    "200e3"))
            notch = atscplus.atsc_adaptive_notch(
                ATSC_RX_SAMPLE_RATE,
                _notch_fft, _notch_thresh, _notch_r,
                _notch_pilot, _notch_guard)
            LOG.info(f"adaptive_notch: ENABLED fft={_notch_fft} "
                     f"thresh_db={_notch_thresh} r={_notch_r} "
                     f"pilot_offset={_notch_pilot} guard={_notch_guard}")
        else:
            LOG.info("adaptive_notch: disabled (STVT_NOTCH=0)")
        # Front-end matched filter; outputs samples at output_rate (16.14 MS/s).
        if int(os.environ.get("STVT_RXF_FUSED", "0")):
            # Fuse resampler + matched filter into ONE fixed-ratio block running
            # straight from the native SDR rate — removes the 6.25M round-trip and
            # the arbitrary-resampler hot thread (profiled ~90% of a core). Skip
            # the standalone resampler; adopt the fused block's actual output rate
            # (used below by fpll/sync). Incompatible with notch/smoother (they
            # assume the 6.25M intermediate) — disable them in fused mode.
            rxf, output_rate = make_fused_rx_filter(ATSC_NATIVE_SAMPLE_RATE, SPS)
            resamp = None
            if notch is not None or smoother is not None:
                LOG.warning("STVT_RXF_FUSED: disabling notch/smoother "
                            "(they expect the 6.25M intermediate)")
                notch = None
                smoother = None
            LOG.info(f"FUSED rx filter active — output_rate={output_rate:.0f}")
        else:
            rxf  = (make_rx_filter(ATSC_RX_SAMPLE_RATE, SPS)
                    if os.environ.get("STVT_RRC_SYMS")
                    else atsc_rx_filter(ATSC_RX_SAMPLE_RATE, SPS))
        # FPLL knobs via STVT_FPLL_ALPHA / STVT_FPLL_AFC_TAU env vars.
        _fpll_alpha   = float(os.environ.get("STVT_FPLL_ALPHA",   "0.001"))
        _fpll_afc_tau = float(os.environ.get("STVT_FPLL_AFC_TAU", "25"))
        fpll = atscplus.atsc_fpll_tight(output_rate, _fpll_alpha, _fpll_afc_tau)
        LOG.info(f"fpll: alpha={_fpll_alpha} afc_tau_us={_fpll_afc_tau}")

        # DC blocker tap count via STVT_DCR_TAPS env var.
        _dcr_taps = int(os.environ.get("STVT_DCR_TAPS", "32"))
        dcr  = gr_filter.dc_blocker_ff(_dcr_taps)

        # AGC knobs via STVT_AGC_ALPHA / STVT_AGC_REFERENCE env vars.
        _agc_alpha = float(os.environ.get("STVT_AGC_ALPHA",     "1e-6"))
        _agc_ref   = float(os.environ.get("STVT_AGC_REFERENCE", "4.0"))
        agc  = analog.agc_ff(_agc_alpha, _agc_ref)
        LOG.info(f"agc: alpha={_agc_alpha} ref={_agc_ref}  dc_blocker_taps={_dcr_taps}")
        sync = atscplus.atsc_sync_soft(output_rate)
        fs_check = atscplus.atsc_fs_checker_inst()
        # Equalizer selected by STVT_EQ env var. Menu in run_ab.sh.
        _eq_name = os.environ.get("STVT_EQ", "long")
        if   _eq_name == "long":          equalizer = atscplus.atsc_equalizer_long()
        elif _eq_name == "pilot":         equalizer = atscplus.atsc_equalizer_pilot()
        elif _eq_name == "pilot_dd":      equalizer = atscplus.atsc_equalizer_pilot_dd()
        elif _eq_name == "pilot_dd_soft": equalizer = atscplus.atsc_equalizer_pilot_dd_soft()
        elif _eq_name == "cma":           equalizer = atscplus.atsc_equalizer_cma()
        elif _eq_name == "multifs":       equalizer = atscplus.atsc_equalizer_pilot_multifs()
        elif _eq_name == "multifs_dd":    equalizer = atscplus.atsc_equalizer_pilot_multifs_dd()
        elif _eq_name == "stock":         equalizer = dtv.atsc_equalizer()
        elif _eq_name == "wl":            equalizer = atscplus.atsc_equalizer_wl()
        else: raise ValueError(f"Unknown STVT_EQ={_eq_name}")
        LOG.info(f"equalizer: {_eq_name} (STVT_EQ)")

        # Viterbi: hard (gr-dtv default) or soft (atscplus fork, ~1-2 dB BER gain)
        _vit_name = os.environ.get("STVT_VITERBI", "hard")
        if   _vit_name == "hard": viterbi = dtv.atsc_viterbi_decoder()
        elif _vit_name == "soft": viterbi = atscplus.atsc_viterbi_soft()
        else: raise ValueError(f"Unknown STVT_VITERBI={_vit_name}")
        LOG.info(f"viterbi: {_vit_name} (STVT_VITERBI)")
        # 2026-05-22: when both soft viterbi AND erasure RS are active, use
        # atscplus.atsc_deinterleaver (tag-forwarding clone) so the
        # viterbi_metric tags emitted by atsc_viterbi_soft reach
        # atsc_rs_decoder_erasure. The stock dtv.atsc_deinterleaver was
        # observed to deliver tags=0 in this configuration.
        _use_tagged_dei = (_vit_name == "soft" and
                           os.environ.get("STVT_RS", "stock") == "erasure")
        if _use_tagged_dei:
            deinterleaver = atscplus.atsc_deinterleaver()
            LOG.info("deinterleaver: atscplus (tag-forwarding)")
        else:
            deinterleaver = dtv.atsc_deinterleaver()
            LOG.info("deinterleaver: dtv (stock)")
        # STVT_RS=erasure switches to the empirical-erasure RS decoder.
        _rs_kind = os.environ.get("STVT_RS", "stock")
        if _rs_kind == "erasure":
            _rs_eras = int(os.environ.get("STVT_RS_ERASURES", "14"))
            rs = atscplus.atsc_rs_decoder_erasure(_rs_eras)
            # 2026-05-21 Day 4: erasure block now 2-port (matches stock).
            _rs_is_2port = True
            LOG.info(f"rs: erasure (max_erasures={_rs_eras}) (STVT_RS)")
        else:
            rs = dtv.atsc_rs_decoder()
            _rs_is_2port = True
            LOG.info("rs: stock dtv.atsc_rs_decoder (STVT_RS)")
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
        # Exposed so the rotation watchdog can reopen it cleanly (file_sink.open
        # resets the write offset to 0). A bare os truncate(0) does NOT — the
        # sink keeps its fd at the old offset and writes a HUGE sparse file whose
        # tail is mostly zero-holes, which starves the live player and eventually
        # wedges the chain. See main()'s rotation loop.
        self._ts_sink = ts_file
        self._ts_path = ts_path

        # Wire the proven run_combo.py topology end-to-end (with soapy.source
        # + scaler in place of file_source + s2c for live capture):
        #   src -> scaler(*32768) -> resamp -> rxf -> fpll -> dcr -> agc -> sync -> fs_check
        # fs_checker through rs_decoder carry TWO streams (data + tag);
        # derandomizer collapses to one, then depad emits raw TS bytes.
        # Skip the resampler stage when STVT_SKIP_RESAMP=1 (SDR runs at 6.25 MS/s).
        # Day 18: insert noise blanker between scaler and (resamp or rxf)
        # so it operates on the raw scaled SDR samples — earliest point
        # impulse spikes show up. Disabled by default; the variable `nb` is
        # None when STVT_NB=0 and falls back to the original chain.
        # 2026-05-29: adaptive notch sits between (resamp output / scaler)
        # and rxf, so it runs at the working sample rate of 6.25 MS/s.
        # `notch` is None when STVT_NOTCH=0 and falls back to legacy chain.
        chain_blocks = [src, scaler]
        if nb is not None:
            chain_blocks.append(nb)
        if resamp is not None:
            chain_blocks.append(resamp)
        if notch is not None:
            chain_blocks.append(notch)
        if smoother is not None:
            chain_blocks.append(smoother)
        # STVT_WL_FUSED=1 (default when STVT_EQ=wl): fused atsc_wl_frontend
        # replaces the three-block companion chain (sync dual-interp +
        # fs_check passthrough) — ONE inlined dual-plane interpolation per
        # symbol. The 2026-07-27 real-time fix: sync's second per-symbol
        # mmse interpolate() call was the entire WL overflow deficit.
        # STVT_WL_FUSED=0 keeps the legacy 3-block companion path for A/B.
        _wl_fused = (_eq_name == "wl" and
                     int(os.environ.get("STVT_WL_FUSED", "1")) != 0)
        wl_front = None

        # STVT_FPLL_FOLD=1: atsc_fpll_tight runs the dc_blocker+agc arithmetic
        # in its own output loop (bit-identical replica), so the separate
        # blocks are omitted — two fewer threads + buffer crossings.
        if _wl_fused:
            if not int(os.environ.get("STVT_FPLL_FOLD", "0")):
                raise ValueError("STVT_EQ=wl requires STVT_FPLL_FOLD=1 "
                                 "(complex companion must match the folded real path)")
            wl_front = atscplus.atsc_wl_frontend(output_rate)
            chain_blocks += [rxf, fpll]
        elif int(os.environ.get("STVT_FPLL_FOLD", "0")):
            chain_blocks += [rxf, fpll, sync, fs_check]
        else:
            chain_blocks += [rxf, fpll, dcr, agc, sync, fs_check]

        # STVT_MIN_BUF (items) / STVT_MIN_BUF_BYTES (bytes-per-edge): enlarge
        # the output buffers of the front-end blocks. GR's default (~32KB)
        # buffers ran the 4-core Pi 5 in lockstep (every thread <100%, chain
        # <1x real-time); 8MB/edge measured 0.91x -> 1.09x there, output
        # bit-identical. x86 runs several x real-time with no lockstep, so this
        # is OFF by default (0 = stock) — set it only if you measure a benefit.
        # The BYTES form scales per item size so vector-output blocks (832B
        # items) don't eat GB of RAM.
        _min_buf = int(os.environ.get("STVT_MIN_BUF", "0"))
        _min_buf_bytes = int(os.environ.get("STVT_MIN_BUF_BYTES", "0"))
        if _min_buf or _min_buf_bytes:
            for blk in chain_blocks:
                try:
                    n = _min_buf
                    if _min_buf_bytes:
                        isz = blk.output_signature().sizeof_stream_item(0)
                        n = max(4096, _min_buf_bytes // max(1, isz))
                    blk.set_min_output_buffer(n)
                except Exception:
                    pass
            LOG.info(f"min_output_buffer: items={_min_buf} bytes={_min_buf_bytes}")

        self.connect(*chain_blocks)

        # ── GLITCH SPECIMEN RECORDER (2026-07-06, E-ladder) ──
        # STVT_IQ_RING=<secs> taps the scaler output (raw scaled SDR
        # samples, int16 range) into a RAM ring; a TRIGGER file in
        # STVT_IQ_RING_DIR dumps the surrounding IQ as a .cs16 specimen.
        # Transient glitches become reproducible lab evidence. Zero cost
        # when unset.
        _ring_secs = float(os.environ.get("STVT_IQ_RING", "0"))
        if _ring_secs > 0:
            from iq_ring import iq_ring_sink
            _ring_dir = os.environ.get(
                "STVT_IQ_RING_DIR",
                str(Path(__file__).parent / "data" / "specimens"))
            self._iq_ring = iq_ring_sink(
                ATSC_NATIVE_SAMPLE_RATE, _ring_secs, _ring_dir, scale=1.0,
                meta={"rf": rf_channel,
                      "antenna": os.environ.get("STVT_ANTENNA", "?")})
            self.connect(scaler, self._iq_ring)
            LOG.info(f"iq_ring: {_ring_secs}s specimen ring -> {_ring_dir}")
        # ── WIDELY-LINEAR path (STVT_EQ=wl, 2026-07-26) ──
        # Route the carrier-corrected complex companion fpll(1)->sync(1)->fs_check(1)
        # into the widely-linear equalizer (complex segments = fs_check out2, plinfo
        # = out1). The real segment (out0) is unused by WL -> null sink; it still
        # drives timing/framing. Requires FPLL fold mode (else the complex bypasses
        # the separate dc_blocker+agc and its Re desyncs from the real path).
        if _wl_fused:
            # FUSED WL front end (2026-07-27): fpll real+imag -> fused
            # sync+framing block -> WL equalizer. See atsc_wl_frontend.h.
            self.connect((fpll, 0), (wl_front, 0))        # real (folded dcr+agc)
            self.connect((fpll, 1), (wl_front, 1))        # imag float companion
            self.connect((wl_front, 0), (equalizer, 0))   # REAL 8-VSB segments
            self.connect((wl_front, 1), (equalizer, 1))   # plinfo
            self.connect((wl_front, 2), (equalizer, 2))   # IMAG segments
            _eq_edges = []            # wl_front->equalizer already wired
            LOG.info("WIDELY-LINEAR equalizer wired (FUSED front end)")
        elif _eq_name == "wl":
            if not int(os.environ.get("STVT_FPLL_FOLD", "0")):
                raise ValueError("STVT_EQ=wl requires STVT_FPLL_FOLD=1 "
                                 "(complex companion must match the folded real path)")
            self.connect((fpll, 1), (sync, 1))            # imag float companion
            self.connect((sync, 1), (fs_check, 1))
            self.connect((fs_check, 0), (equalizer, 0))   # REAL 8-VSB segments
            self.connect((fs_check, 1), (equalizer, 1))   # plinfo
            self.connect((fs_check, 2), (equalizer, 2))   # IMAG segments
            _eq_edges = []            # fs_check->equalizer already wired (real+imag)
            LOG.info("WIDELY-LINEAR equalizer wired (legacy 3-block companion path)")
        else:
            _eq_edges = [(fs_check, equalizer)]
        if _rs_is_2port:
            for blk_in, blk_out in _eq_edges + [(equalizer, viterbi),
                                     (viterbi, deinterleaver),
                                     (deinterleaver, rs),
                                     (rs, derand)]:
                self.connect((blk_in, 0), (blk_out, 0))
                self.connect((blk_in, 1), (blk_out, 1))
            # ── SOVA reliability plane (2026-07-07, STVT_SOVA=1) ──
            # viterbi port 2 carries per-byte trellis confidence; a TWIN
            # deinterleaver applies the identical permutation so each
            # reliability byte stays glued to its data byte; rs input 2
            # turns the weakest bytes into TRUE erasures.
            if (int(os.environ.get("STVT_SOVA", "0"))
                    and os.environ.get("STVT_VITERBI") == "soft"
                    and os.environ.get("STVT_RS") == "erasure"):
                dei_rel = atscplus.atsc_deinterleaver()
                self.connect((viterbi, 2), (dei_rel, 0))
                self.connect((viterbi, 1), (dei_rel, 1))
                self.connect((dei_rel, 0), (rs, 2))
                # every output must land somewhere: twin's plinfo -> null
                _rel_pl_sink = blocks.null_sink(
                    gr.sizeof_char * 4)  # sizeof(plinfo) == 4
                self.connect((dei_rel, 1), _rel_pl_sink)
                self._dei_rel = dei_rel
                self._rel_pl_sink = _rel_pl_sink
                LOG.info("SOVA: reliability plane wired "
                         "(viterbi:2 -> twin deinterleaver -> rs:2)")
                # ── TURBO 2B: trellis pinning (2026-07-10) ──
                # rs port 3 takes the post-equalizer soft symbols (the
                # exact viterbi input; sync blocks, so item indices
                # coincide). When a codeword fails RS+GMD the block
                # re-runs a pinned Viterbi over the affected trellis
                # spans using bytes of DECODED codewords as branch
                # constraints, then retries RS. Converts 54-70% of
                # failed packets on marginal air (replay A/B 7/10).
                # Opt-in: STVT_TURBO=1 (requires the SOVA plane above).
                if int(os.environ.get("STVT_TURBO", "0")):
                    self.connect((equalizer, 0), (rs, 3))
                    # rs consumes the eq buffer ~lag+64 segments behind
                    # the viterbi path reader — give the shared eq
                    # output buffer comfortable headroom
                    equalizer.set_min_output_buffer(512)
                    LOG.info("TURBO 2B: soft-symbol plane wired (live)")
        else:
            for blk_in, blk_out in _eq_edges + [(equalizer, viterbi),
                                     (viterbi, deinterleaver)]:
                self.connect((blk_in, 0), (blk_out, 0))
                self.connect((blk_in, 1), (blk_out, 1))
            self.connect((deinterleaver, 0), (rs, 0))
            self.connect((rs, 0), (derand, 0))
            self.connect((deinterleaver, 1), (derand, 1))
        self.connect(derand, depad)
        # TEI-scrub: pack depad's byte stream into 188-byte TS packets,
        # rewrite RS-uncorrectable packets to NULL packets (preserves CC),
        # then back to a byte stream into the file sink.
        # Hold strong refs on `self` — Python sync_block instances must
        # outlive top_block.start(); otherwise GR's block_executor crashes
        # when its weakref to the deallocated Python wrapper is cleared.
        self._v2s_in   = blocks.stream_to_vector(gr.sizeof_char, 188)
        self._teiscrub = TEIScrub()
        self._v2s_out  = blocks.vector_to_stream(gr.sizeof_char, 188)
        if os.environ.get("STVT_TEISCRUB", "1") == "1":
            self.connect(depad, self._v2s_in, self._teiscrub, self._v2s_out, ts_file)
        else:
            self.connect(depad, ts_file)

        # EQUALIZER RESEARCH TAP (2026-07-26): STVT_EQ_DUMP=<dir> dumps the
        # equalizer INPUT (fs_check out = the real 8-VSB symbols the LMS sees)
        # and the equalizer OUTPUT (C++ baseline) as raw float32, so an offline
        # testbench can prototype superior equalizers (CIR-sparse, widely-linear,
        # turbo-eq) on real marginal captures without touching the live chain.
        # Off by default; fan-out taps don't perturb the decode path.
        _eq_dump = os.environ.get("STVT_EQ_DUMP")
        if _eq_dump:
            os.makedirs(_eq_dump, exist_ok=True)
            self._eqin_sink = blocks.file_sink(gr.sizeof_float,
                                               f"{_eq_dump}/eq_in.f32")
            self._eqin_sink.set_unbuffered(False)
            self._eqout_sink = blocks.file_sink(gr.sizeof_float,
                                                f"{_eq_dump}/eq_out.f32")
            self._eqout_sink.set_unbuffered(False)
            _eqin_src = wl_front if _wl_fused else fs_check
            self.connect((_eqin_src, 0), self._eqin_sink)
            self.connect((equalizer, 0), self._eqout_sink)
            LOG.info(f"EQ RESEARCH DUMP -> {_eq_dump}/eq_in.f32, eq_out.f32")

        # Diagnostic taps — capture bytes at 5 decode-chain points.
        # OFF by default (2026-05-16): a multi-hour chain run filled /tmp
        # tmpfs with 6.1 GB of diag data and broke the system with
        # "Disk quota exceeded". Set STVT_DIAG=1 to re-enable during
        # algorithm tuning; for normal viewing the chain doesn't need them.
        if os.environ.get("STVT_DIAG"):
            diag_dir = "/tmp/diag_taps"
            os.makedirs(diag_dir, exist_ok=True)

            # RS input bytes (post-deinterleaver, 207-byte RS code blocks).
            self._diag_dei_v2s  = blocks.vector_to_stream(gr.sizeof_char, 207)
            self._diag_dei_sink = blocks.file_sink(gr.sizeof_char, f"{diag_dir}/dei_out.bin")
            self._diag_dei_sink.set_unbuffered(True)
            self.connect((deinterleaver, 0), self._diag_dei_v2s, self._diag_dei_sink)

            # RS output bytes (corrected, 188-byte packets, sync 0x47 at offset 0).
            self._diag_rs_v2s   = blocks.vector_to_stream(gr.sizeof_char, 188)
            self._diag_rs_sink  = blocks.file_sink(gr.sizeof_char, f"{diag_dir}/rs_out.bin")
            self._diag_rs_sink.set_unbuffered(True)
            self.connect((rs, 0), self._diag_rs_v2s, self._diag_rs_sink)

            # RS metadata stream (4 bytes per packet — plinfo: sync, errors, ...).
            # Erasure RS variant has no plinfo port; skip when in use.
            if _rs_is_2port:
                self._diag_rs_meta_sink = blocks.file_sink(4, f"{diag_dir}/rs_meta.bin")
                self._diag_rs_meta_sink.set_unbuffered(True)
                self.connect((rs, 1), self._diag_rs_meta_sink)

            # Derand output (descrambled, 188-byte packets).
            self._diag_derand_v2s  = blocks.vector_to_stream(gr.sizeof_char, 188)
            self._diag_derand_sink = blocks.file_sink(gr.sizeof_char, f"{diag_dir}/derand_out.bin")
            self._diag_derand_sink.set_unbuffered(True)
            self.connect(derand, self._diag_derand_v2s, self._diag_derand_sink)

            # Depad output — raw TS bytes BEFORE TEIScrub rewrites failed packets.
            # This is the real RS failure rate (live.ts hides it because scrub
            # converts TEI=1 packets into NULL packets, hiding the loss rate).
            self._diag_depad_sink = blocks.file_sink(gr.sizeof_char, f"{diag_dir}/depad_out.bin")
            self._diag_depad_sink.set_unbuffered(True)
            self.connect(depad, self._diag_depad_sink)

    # ── PERSISTENT-RADIO RETUNE (2026-07-10, STVT_PERSIST_RETUNE=1) ──
    # Cross-tower channel change WITHOUT killing the process: today every
    # tune pays 3.2 s process kill + 3.6 s SDRplay driver reopen before the
    # 1.1 s pilot lock + ~3 s EQ converge even start. The soapy source
    # exposes runtime setters (the same methods __init__ already calls:
    # set_frequency / set_antenna / set_gain / write_setting), so a live
    # chain can re-point the radio in ~0.1 s and let the DSP re-acquire
    # (FPLL hunts the new pilot, fs_checker re-syncs, and — since
    # 2026-07-29, speed-1 lever 1 — the equalizer REBINDS its warm-start
    # tap cache instead of cold-retraining, see _eq_cache_rebind below).
    def _eq_cmd(self, verb: str) -> bool:
        """Drop a one-word command for the equalizer's E5 command port,
        which polls it once per field sync (~21 Hz). Returns False if no
        command file is configured (then the caller just skips the step)."""
        path = os.environ.get("STVT_EQ_CMD_FILE")
        if not path:
            return False
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="ascii") as f:
                f.write(verb)
            os.replace(tmp, path)
            return True
        except OSError as exc:
            LOG.info(f"PERSIST-RETUNE: eq cmd '{verb}' failed ({exc!r})")
            return False

    def _eq_cache_path(self, rf: int, antenna: str | None) -> str | None:
        """<STVT_EQ_TAP_CACHE>/taps_<ANTENNA>_rf<RF>.bin, or None when the
        user has not configured a cache directory. Keyed by channel AND
        antenna because the taps are a fingerprint of the whole RF path."""
        cache_dir = os.environ.get("STVT_EQ_TAP_CACHE")
        # main() only publishes STVT_EQ_TAP_CACHE_FILE when the cache is
        # actually enabled for this session, so its absence is the exact
        # signal that we must not touch the cache here either — including
        # under STVT_PERSIST_RETUNE_CACHE=0, where a retune that rebound the
        # path would otherwise quietly re-enable the equalizer's periodic
        # WRITE while the load stayed disabled.
        if not cache_dir or not os.environ.get("STVT_EQ_TAP_CACHE_FILE"):
            return None
        ant = re.sub(r"\W+", "",
                     antenna or os.environ.get("STVT_ANTENNA", "A"))
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            return None
        return os.path.join(cache_dir, f"taps_{ant}_rf{rf}.bin")

    def retune(self, cmd: dict):
        rf = int(cmd["rf"])
        freq = rf_to_freq_hz(rf)
        src = self._src
        t0 = time.time()
        ant = cmd.get("antenna")
        LOG.info(f"PERSIST-RETUNE: rf {self._rf} -> {rf} "
                 f"({freq/1e6:.3f} MHz) antenna={ant} "
                 f"rfgain_sel={cmd.get('rfgain_sel')} ifgr={cmd.get('ifgr')} "
                 f"dabnotch={cmd.get('dabnotch')}")
        # ── STEP 0: persist the OUTGOING channel's taps BEFORE the radio
        # moves, while d_taps_lkg still describes this channel's multipath.
        # One field sync (~24.2 ms) is the command port's polling period.
        new_cache = self._eq_cache_path(rf, ant)
        if new_cache and self._eq_cmd("save"):
            time.sleep(float(os.environ.get("STVT_RETUNE_EQ_SAVE_MS", "40"))
                       / 1000.0)
        # antenna first (RSPdx port switch), then frequency — if either
        # hard-fails we raise so the panel's classic respawn takes over.
        if ant:
            src.set_antenna(0, ant)
        src.set_frequency(0, freq)
        if cmd.get("ifgr") is not None:
            try:
                src.set_gain(0, "IFGR", float(cmd["ifgr"]))
            except Exception:
                src.set_gain(0, float(cmd["ifgr"]))
        if cmd.get("rfgain_sel") is not None:
            try:
                src.write_setting("rfgain_sel", str(cmd["rfgain_sel"]))
            except Exception as exc:
                LOG.info(f"PERSIST-RETUNE: rfgain_sel write failed ({exc!r})")
        if cmd.get("dabnotch") is not None:
            # RSPdx-specific; only matters on UHF<->VHF transitions. A
            # failure here is logged, not fatal (UHF<->UHF unaffected).
            try:
                src.write_setting("dabnotch_ctrl",
                                  "true" if int(cmd["dabnotch"]) else "false")
            except Exception as exc:
                LOG.info(f"PERSIST-RETUNE: dabnotch_ctrl write failed "
                         f"({exc!r}) — VHF transitions may need respawn")
        # Rotate live.ts so followers see a FRESH stream (rotation law:
        # the extractor must never read old-channel bytes as new). The
        # sink holds the Windows file handle, so unlink is impossible —
        # file_sink.open() reopens the same path truncated at offset 0,
        # the exact mechanism the size-rotation watchdog already uses
        # (never a bare truncate(0): sparse-file trap, see main()).
        self._ts_sink.open(str(self._ts_path))
        self._ts_sink.set_unbuffered(True)
        self._rf = rf
        # ── STEP 1: rebind the cache to the INCOMING channel and warm-load.
        # Deliberately AFTER the radio has been re-pointed: the RSPdx's AGC
        # settles in 7.5-80 ms depending on level, and the documented
        # live-debut explosion (2026-07-10) is stale taps adapting while the
        # front end is still moving. STVT_RETUNE_EQ_HOLD_MS (default 100,
        # covering the slow end of the AGC settle) is that hold; the
        # equalizer's own d_fs_trained>=3 DD guard is re-armed by `warm`.
        # If the new channel has NO cache, `warm` resets to the delta —
        # exactly the state a fresh process on that channel would start in,
        # never the old channel's multipath carried across.
        if new_cache:
            os.environ["STVT_EQ_TAP_CACHE_FILE"] = new_cache
            hold_ms = float(os.environ.get("STVT_RETUNE_EQ_HOLD_MS", "100"))
            if hold_ms > 0:
                time.sleep(hold_ms / 1000.0)
            if self._eq_cmd("warm"):
                LOG.info(f"PERSIST-RETUNE: eq cache rebound -> {new_cache} "
                         f"({'warm' if os.path.exists(new_cache) else 'cold'})")
        if hasattr(self, "_iq_ring"):
            try:
                self._iq_ring.meta.update(
                    {"rf": rf, "antenna": ant or self._iq_ring.meta.get("antenna")})
            except Exception:
                pass
        LOG.info(f"PERSIST-RETUNE: applied in {time.time()-t0:.2f}s — "
                 "chain re-acquiring (FPLL hunt -> fs sync -> EQ retrain)")


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
    # Rotation now reopens the sink cleanly (offset reset, non-sparse), so the
    # file stays truly bounded at this size. The live player only ever reads the
    # last ~25 MB, so a smaller cap costs nothing and keeps disk modest; 20 GB ≈
    # 2.3 h between the brief tail re-follow blips. Override with STVT_ROTATE_GB.
    ap.add_argument("--rotate-gb", type=float,
                    default=float(os.environ.get("STVT_ROTATE_GB", "20.0")))
    ap.add_argument("--soapy-args",
                    default=os.environ.get("STVT_SOAPY_ARGS", ""),
                    help="SoapySDR device specifier. Default 'driver=sdrplay'. "
                         "For SoapyRemote (e.g. WSL2 -> Windows host) use "
                         "'driver=remote,remote=<host>:55132,"
                         "remote:driver=sdrplay'. Can also be set via the "
                         "$STVT_SOAPY_ARGS env var so subprocess callers "
                         "(tv_tuner.py scan + lock-test) inherit it.")
    ap.add_argument("--stream-args",
                    default=os.environ.get("STVT_STREAM_ARGS", ""),
                    help="SoapySDR stream args (passed to setupStream). "
                         "For SoapyRemote use 'prot=tcp' to force lossless "
                         "TCP transport (slower than UDP but no drops, which "
                         "matters because RS decoding is intolerant of "
                         "sample loss). Also reads $STVT_STREAM_ARGS.")
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

    _persist = os.environ.get("STVT_PERSIST_RETUNE") == "1"

    # WARM START (2026-07-05): if a tap-cache directory is configured,
    # compose the per-channel+antenna cache file path for the equalizer
    # (which reads STVT_EQ_TAP_CACHE_FILE at construction). Keyed by both
    # because taps are a fingerprint of the full RF path.
    cache_dir = os.environ.get("STVT_EQ_TAP_CACHE")
    # 2026-07-29 (speed-1 lever 1) RUNTIME TAP-CACHE REBIND.
    # This guard used to read: persist-retune DISABLES the cache, because
    # the equalizer latched STVT_EQ_TAP_CACHE_FILE in a `static` and would
    # go on writing NEW-channel taps into the OLD channel's file. That made
    # the fast channel-change path deliberately give up the fast equalizer
    # — the single biggest avoidable cost in a channel change.
    # The equalizer now reads that env at call time and exposes `save` /
    # `warm` verbs on its command port, so TVLive.retune() can persist the
    # old channel's taps, rebind, and warm-load the new channel's. Set
    # STVT_PERSIST_RETUNE_CACHE=0 to restore the old cache-less behaviour.
    if cache_dir and _persist and \
            os.environ.get("STVT_PERSIST_RETUNE_CACHE", "1") == "0":
        LOG.info("tap cache DISABLED (STVT_PERSIST_RETUNE_CACHE=0)")
        cache_dir = None
    if cache_dir:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            ant = re.sub(r"\W+", "", os.environ.get("STVT_ANTENNA", "A"))
            os.environ["STVT_EQ_TAP_CACHE_FILE"] = os.path.join(
                cache_dir, f"taps_{ant}_rf{args.rf}.bin")
            LOG.info(f"tap cache: {os.environ['STVT_EQ_TAP_CACHE_FILE']}")
            # DEFECT D3 (dossier §3.4): the equalizer only ever fills its
            # LKG snapshot when STVT_EQ_LKG is on, and the C++ default is
            # OFF — so a direct tv_live run (stvt_run.sh, lab harnesses)
            # asked for a tap cache and then never wrote one. tv_tuner's
            # CHAIN_DEFAULTS already sets it to "1"; do the same here so
            # "I configured a cache" implies "the cache can be filled".
            # Only when the user asked for a cache, and never overriding
            # an explicit setting.
            if not os.environ.get("STVT_EQ_LKG"):
                os.environ["STVT_EQ_LKG"] = "1"
                LOG.info("STVT_EQ_LKG defaulted to 1 (a tap cache is "
                         "configured; without LKG nothing would be saved)")
            if _persist and not os.environ.get("STVT_EQ_CMD_FILE"):
                # The retune rebind talks to the equalizer over its E5
                # command port; give it a path if nobody else has. Only on
                # the persist path, so the default chain is untouched (no
                # command file = no per-field-sync stat()).
                os.environ["STVT_EQ_CMD_FILE"] = str(
                    out.parent / "eq_cmd.txt")
                try:
                    os.unlink(os.environ["STVT_EQ_CMD_FILE"])
                except OSError:
                    pass
        except OSError as e:
            LOG.info(f"tap cache disabled ({e})")

    if not args.soapy_args:
        # auto-detect the plugged-in radio (issue #2: PlutoSDR owners
        # got "SDR not found" while their radio probed perfectly)
        try:
            import sdr_compat
            args.soapy_args = sdr_compat.resolve_soapy_args()
        except Exception:
            args.soapy_args = "driver=sdrplay"
    tb = LiveTVTopBlock(args.rf, out, soapy_args=args.soapy_args,
                        stream_args=args.stream_args)

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

    # ── PERSISTENT-RADIO RETUNE watcher (2026-07-10) ──
    # Opt-in via STVT_PERSIST_RETUNE=1 (default OFF, zero change unset).
    # Watches data/tv_live/retune.cmd for JSON
    #   {"rf":N,"antenna":"...","rfgain_sel":N,"ifgr":N,"dabnotch":0|1}
    # written atomically by the panel, retunes the RUNNING chain in place
    # and answers via retune.ack. The panel gates on the ack plus fresh
    # field-sync telemetry and falls back to classic kill+respawn if
    # either never arrives — fallback-first by design.
    if _persist:
        cmd_path = out.parent / "retune.cmd"
        ack_path = out.parent / "retune.ack"
        for _p in (cmd_path, ack_path):     # stale files from a dead session
            try:
                _p.unlink()
            except OSError:
                pass

        def _retune_watcher():
            bad_reads = 0
            while True:
                time.sleep(0.2)
                try:
                    raw = cmd_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                try:
                    cmd = json.loads(raw)
                except ValueError:
                    bad_reads += 1
                    if bad_reads >= 5:      # corrupt file — don't spin on it
                        try:
                            cmd_path.unlink()
                        except OSError:
                            pass
                        bad_reads = 0
                    continue
                bad_reads = 0
                try:
                    cmd_path.unlink()
                except OSError:
                    pass
                try:
                    tb.retune(cmd)
                    ack = {"ok": True, "rf": cmd.get("rf"), "t": time.time()}
                except Exception as e:
                    LOG.error(f"PERSIST-RETUNE failed: {e!r}")
                    ack = {"ok": False, "error": str(e), "t": time.time()}
                _tmp = ack_path.parent / (ack_path.name + ".tmp")
                try:
                    _tmp.write_text(json.dumps(ack), encoding="utf-8")
                    os.replace(_tmp, ack_path)
                except OSError:
                    pass

        threading.Thread(target=_retune_watcher, daemon=True).start()
        LOG.info(f"PERSIST-RETUNE: watcher armed on {cmd_path}")

    # File-rotation watchdog: when live.ts > rotate-gb, restart write
    rotate_bytes = int(args.rotate_gb * 1e9)
    while True:
        time.sleep(10)
        try:
            sz = out.stat().st_size
            if sz > rotate_bytes:
                LOG.info(f"Rotating live.ts ({sz/1e9:.1f} GB)")
                # Reopen the sink's own file descriptor at offset 0. file_sink.open()
                # is thread-safe (applied at the next work() via do_update) and
                # actually resets the write position — unlike a bare truncate(0),
                # which left the sink writing at the old offset and ballooned a
                # sparse file (zero-hole tail) that starved the player and wedged
                # the chain after ~9 h. set_unbuffered persists, but reassert it.
                tb._ts_sink.open(str(out))
                tb._ts_sink.set_unbuffered(True)
        except FileNotFoundError:
            continue


if __name__ == "__main__":
    main()
