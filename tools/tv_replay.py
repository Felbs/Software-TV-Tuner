#!/usr/bin/env python3
"""tv_replay.py — byte-faithful, instrumented replay of the production chain.

This is the deterministic test instrument for the drift investigation. It is a
LINE-FOR-LINE replica of tv_live.py's flowgraph, with the soapy SDR source
replaced by a blocks.file_source reading a captured CF32 IQ file. Because the
input is a frozen file, the SDR and RF are completely removed from the
equation: any clean->noise drift seen here is produced ENTIRELY by our DSP.

It reads the exact same STVT_* env vars as tv_live.py (so sourcing the
production winner env reproduces the production chain), and adds diagnostic
taps (STVT_DIAG=1) that write to STVT_DIAG_DIR (default /home/user/diag_taps,
NOT /tmp — replays are large). New tap vs tv_live.py: the post-equalizer
SOFT-SYMBOL stream (eq_out.f32), so we can tell whether the 8-VSB eye is open
during a 'noise' stretch (=> framing/derand bug downstream) or closed
(=> genuine symbol-quality problem).

Usage:
    python3 tools/tv_replay.py --iq /home/user/iq_drift_rf34.cf32 \
        --out /home/user/replay_1.ts --log /home/user/replay_1.log
"""
from __future__ import annotations
import argparse, logging, os, signal, sys, time
from pathlib import Path

from gnuradio import gr, blocks, analog, dtv
from gnuradio import filter as gr_filter
from gnuradio import atscplus
from gnuradio.dtv.atsc_rx_filter import atsc_rx_filter, ATSC_SYMBOL_RATE
from gnuradio import filter as _gfilter
from gnuradio.filter import firdes


def make_rx_filter(input_rate, sps):
    """Matched RRC filter, replica of dtv.atsc_rx_filter but with a tunable RRC
    span (STVT_RRC_SYMS, default 8 = stock). Fewer symbols = fewer taps =
    proportionally less work in the chain's single-threaded bottleneck. ATSC's
    11.5% excess BW tolerates a shorter RRC. Returns a pfb_arb_resampler_ccf."""
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
    return _gfilter.pfb_arb_resampler_ccf(interp, rrc_taps, nfilts)

ATSC_NATIVE_SAMPLE_RATE = 8_000_000
ATSC_RX_SAMPLE_RATE     = 6_250_000
RESAMP_INTERP = 25
RESAMP_DECIM  = 32

LOG = logging.getLogger("tv_replay")


# TEIScrub — identical to tv_live.py (rewrite RS-failed packets to NULL).
class TEIScrub(gr.sync_block):
    def __init__(self):
        gr.sync_block.__init__(self, name="TEIScrub",
                               in_sig=[(gr.gr_complex.__class__ if False else None)])
        # placeholder; real impl mirrors tv_live (188-byte vectors)


def _make_teiscrub():
    import numpy as np
    class _TEIScrub(gr.sync_block):
        def __init__(self):
            gr.sync_block.__init__(
                self, name="TEIScrub",
                in_sig=[(np.uint8, 188)], out_sig=[(np.uint8, 188)])
        def work(self, input_items, output_items):
            inp = input_items[0]; out = output_items[0]
            n = len(inp)
            for i in range(n):
                pkt = inp[i]
                out[i] = pkt
                # TEI bit = bit 7 of byte 1; if set, rewrite to NULL packet.
                if pkt[1] & 0x80:
                    out[i][0] = 0x47
                    out[i][1] = 0x1F
                    out[i][2] = 0xFF
                    out[i][3] = pkt[3] & 0x0F  # preserve CC
            return n
    return _TEIScrub()


class ReplayTopBlock(gr.top_block):
    def __init__(self, iq_path: Path, ts_path: Path, repeat: bool, diag_dir: str):
        super().__init__("tv_replay")

        # Input format: CF32 (complex float32, default) or CS16 (interleaved
        # int16, half the disk — what record_iq.py --format cs16 writes). Detected
        # by .cs16/.sc16 extension, override with STVT_IQ_FORMAT=cf32|cs16. The
        # CS16 path divides by 32767 to undo record_iq's *32767 (verified exact).
        _ext = str(iq_path).lower()
        _fmt = os.environ.get("STVT_IQ_FORMAT",
                              "cs16" if _ext.endswith((".cs16", ".sc16")) else "cf32")
        # STVT_IQ_SKIP: drop the first N complex samples — decode-diversity
        # knob for E7 voting (a different start gives the EQ/FPLL a different
        # adaptation trajectory over the identical material).
        _skip = int(os.environ.get("STVT_IQ_SKIP", "0"))
        if _fmt == "cs16":
            _fsrc = blocks.file_source(gr.sizeof_short, str(iq_path), repeat)
            _s2c  = blocks.interleaved_short_to_complex(False, False, 32767.0)
            self.connect(_fsrc, _s2c)
            src = _s2c
            LOG.info(f"input: CS16 interleaved int16 (scale 32767) {iq_path}")
        else:
            src = blocks.file_source(gr.sizeof_gr_complex, str(iq_path), repeat)
            LOG.info(f"input: CF32 {iq_path}")
        if _skip:
            _sk = blocks.skiphead(gr.sizeof_gr_complex, _skip)
            self.connect(src, _sk)
            src = _sk
            LOG.info(f"skiphead: {_skip} samples")

        # SPS = internal oversampling (samples/symbol). 1.5 = stock 16.14 MS/s.
        # Lowering it cuts the matched-filter/resampler + all front-end blocks
        # proportionally (the real-time throughput lever). STVT_SPS overrides.
        SPS = float(os.environ.get("STVT_SPS", "1.5"))
        output_rate = ATSC_SYMBOL_RATE * SPS
        LOG.info(f"SPS={SPS} output_rate={output_rate:.0f}")

        scaler = blocks.multiply_const_cc(32768.0)

        # STVT_ADD_NOISE=<amp>: inject complex AWGN after scaling to push the
        # decode toward the cliff, for marginal-signal config testing. 0 = off
        # (byte-faithful). Signal RMS post-scale ~1500-2000, so amp ~500-1600
        # spans clean -> cliff -> dead.
        _noise_amp = float(os.environ.get("STVT_ADD_NOISE", "0"))
        noise_src = noise_add = None
        if _noise_amp > 0:
            noise_src = analog.noise_source_c(
                analog.GR_GAUSSIAN, _noise_amp,
                int(os.environ.get("STVT_NOISE_SEED", "42")))
            noise_add = blocks.add_cc()

        nb = None
        if int(os.environ.get("STVT_NB", "0")):
            nb = atscplus.atsc_noise_blanker(
                float(os.environ.get("STVT_NB_THRESHOLD", "3.0")),
                int(os.environ.get("STVT_NB_BLANK_SAMPLES", "8")),
                float(os.environ.get("STVT_NB_ALPHA", "1e-4")))

        if int(os.environ.get("STVT_SKIP_RESAMP", "0")):
            resamp = None
        else:
            resamp = gr_filter.rational_resampler_ccc(
                interpolation=RESAMP_INTERP, decimation=RESAMP_DECIM)

        smoother = None
        if int(os.environ.get("STVT_SPECTRAL", "0")):
            smoother = atscplus.atsc_spectral_smoother(
                ATSC_RX_SAMPLE_RATE,
                int(os.environ.get("STVT_SPECTRAL_FFT", "1024")),
                int(os.environ.get("STVT_SPECTRAL_NEIGHBORHOOD", "32")),
                float(os.environ.get("STVT_SPECTRAL_THRESH", "3.0")))

        notch = None
        if int(os.environ.get("STVT_NOTCH", "0")):
            notch = atscplus.atsc_adaptive_notch(
                ATSC_RX_SAMPLE_RATE,
                int(os.environ.get("STVT_NOTCH_FFT", "1024")),
                float(os.environ.get("STVT_NOTCH_THRESH_DB", "12.0")),
                float(os.environ.get("STVT_NOTCH_R", "0.985")),
                float(os.environ.get("STVT_NOTCH_PILOT_HZ", "-2.69e6")),
                float(os.environ.get("STVT_NOTCH_GUARD_HZ", "200e3")))

        # Stock matched filter, or the tunable one when STVT_RRC_SYMS is set.
        if os.environ.get("STVT_RRC_SYMS"):
            rxf = make_rx_filter(ATSC_RX_SAMPLE_RATE, SPS)
        else:
            rxf = atsc_rx_filter(ATSC_RX_SAMPLE_RATE, SPS)
        fpll = atscplus.atsc_fpll_tight(
            output_rate,
            float(os.environ.get("STVT_FPLL_ALPHA", "0.001")),
            float(os.environ.get("STVT_FPLL_AFC_TAU", "25")))
        dcr = gr_filter.dc_blocker_ff(int(os.environ.get("STVT_DCR_TAPS", "32")))
        agc = analog.agc_ff(
            float(os.environ.get("STVT_AGC_ALPHA", "1e-6")),
            float(os.environ.get("STVT_AGC_REFERENCE", "4.0")))
        sync = atscplus.atsc_sync_soft(output_rate)
        fs_check = atscplus.atsc_fs_checker_inst()

        _eq = os.environ.get("STVT_EQ", "long")
        eqmap = {
            "long": atscplus.atsc_equalizer_long,
            "pilot": atscplus.atsc_equalizer_pilot,
            "pilot_dd": atscplus.atsc_equalizer_pilot_dd,
            "pilot_dd_soft": atscplus.atsc_equalizer_pilot_dd_soft,
            "cma": atscplus.atsc_equalizer_cma,
            "multifs": atscplus.atsc_equalizer_pilot_multifs,
            "multifs_dd": atscplus.atsc_equalizer_pilot_multifs_dd,
            "stock": dtv.atsc_equalizer,
            "wl": atscplus.atsc_equalizer_wl,   # widely-linear (complex-fed)
        }
        equalizer = eqmap[_eq]()

        _vit = os.environ.get("STVT_VITERBI", "hard")
        viterbi = (atscplus.atsc_viterbi_soft() if _vit == "soft"
                   else dtv.atsc_viterbi_decoder())

        _use_tagged_dei = (_vit == "soft" and
                           os.environ.get("STVT_RS", "stock") == "erasure")
        deinterleaver = (atscplus.atsc_deinterleaver() if _use_tagged_dei
                         else dtv.atsc_deinterleaver())

        _rs_kind = os.environ.get("STVT_RS", "stock")
        if _rs_kind == "erasure":
            rs = atscplus.atsc_rs_decoder_erasure(
                int(os.environ.get("STVT_RS_ERASURES", "14")))
        else:
            rs = dtv.atsc_rs_decoder()
        derand = dtv.atsc_derandomizer()
        depad = dtv.atsc_depad()

        ts_file = blocks.file_sink(gr.sizeof_char, str(ts_path))
        ts_file.set_unbuffered(True)

        chain_blocks = [src, scaler]
        if noise_add is not None: chain_blocks.append(noise_add)
        if nb is not None:       chain_blocks.append(nb)
        if resamp is not None:   chain_blocks.append(resamp)
        if notch is not None:    chain_blocks.append(notch)
        if smoother is not None: chain_blocks.append(smoother)
        # STVT_FPLL_FOLD=1: atsc_fpll_tight runs the dc_blocker+agc arithmetic
        # in its own output loop (bit-identical replica), so the separate
        # blocks are omitted — two fewer threads + buffer crossings.
        if int(os.environ.get("STVT_FPLL_FOLD", "0")):
            chain_blocks += [rxf, fpll, sync, fs_check]
        else:
            chain_blocks += [rxf, fpll, dcr, agc, sync, fs_check]

        # STVT_MIN_BUF (items) / STVT_MIN_BUF_BYTES (bytes-per-edge): enlarge
        # the front-end output buffers. The Pi 5 needed this (GR's ~32KB
        # default ran its 4 cores in lockstep at 0.91x); x86 runs several x
        # real-time with no lockstep, so it is OFF by default (0 = stock).
        # Output is bit-identical either way; the BYTES form scales per item
        # size so vector-output blocks (832B items) don't eat GB of RAM.
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
        if noise_add is not None:
            self.connect(noise_src, (noise_add, 1))

        if _eq == "wl":
            # WIDELY-LINEAR path (2026-07-26): route the carrier-corrected complex
            # companion fpll(1)->sync(1)->fs_check(1); the equalizer consumes the
            # COMPLEX segments (fs_check out2) + plinfo (out1). The real segment
            # (out0) is unused by WL -> null sink (it still drives timing/framing).
            self.connect((fpll, 1), (sync, 1))
            self.connect((sync, 1), (fs_check, 1))
            self.connect((fs_check, 2), (equalizer, 0))   # complex 8-VSB segments
            self.connect((fs_check, 1), (equalizer, 1))   # plinfo
            self.connect((fs_check, 0),
                         blocks.null_sink(gr.sizeof_float * 832))
            for a, b in [(equalizer, viterbi), (viterbi, deinterleaver),
                         (deinterleaver, rs), (rs, derand)]:
                self.connect((a, 0), (b, 0))
                self.connect((a, 1), (b, 1))
        else:
            for a, b in [(fs_check, equalizer), (equalizer, viterbi),
                         (viterbi, deinterleaver), (deinterleaver, rs), (rs, derand)]:
                self.connect((a, 0), (b, 0))
                self.connect((a, 1), (b, 1))
        # ── SOVA reliability plane (2026-07-07) — mirror of tv_live.py ──
        if (int(os.environ.get("STVT_SOVA", "0"))
                and os.environ.get("STVT_VITERBI") == "soft"
                and os.environ.get("STVT_RS") == "erasure"):
            dei_rel = atscplus.atsc_deinterleaver()
            self.connect((viterbi, 2), (dei_rel, 0))
            self.connect((viterbi, 1), (dei_rel, 1))
            self.connect((dei_rel, 0), (rs, 2))
            _rel_pl_sink = blocks.null_sink(gr.sizeof_char * 4)
            self.connect((dei_rel, 1), _rel_pl_sink)
            self._dei_rel = dei_rel
            self._rel_pl_sink = _rel_pl_sink
            LOG.info("SOVA: reliability plane wired (replay)")
            # ── TURBO STAGE 2B (2026-07-10): trellis pinning ──
            # Feed the rs_erasure block the post-equalizer SOFT SYMBOLS
            # (the exact viterbi input; all chain blocks are sync blocks so
            # the item index spaces coincide). When a codeword fails RS+GMD
            # the block re-runs a pinned Viterbi over the affected trellis
            # spans using bytes of DECODED codewords as branch constraints,
            # then retries RS. Opt-in: STVT_TURBO=1 (requires SOVA plane).
            if int(os.environ.get("STVT_TURBO", "0")):
                self.connect((equalizer, 0), (rs, 3))
                # rs consumes the eq buffer ~lag+64 segments behind the
                # viterbi path reader — give the shared eq output buffer
                # comfortable headroom
                equalizer.set_min_output_buffer(512)
                LOG.info("TURBO 2B: soft-symbol plane wired (replay)")
        self.connect(derand, depad)

        if os.environ.get("STVT_TEISCRUB", "1") == "1":
            self._v2s_in  = blocks.stream_to_vector(gr.sizeof_char, 188)
            self._teiscrub = _make_teiscrub()
            self._v2s_out = blocks.vector_to_stream(gr.sizeof_char, 188)
            self.connect(depad, self._v2s_in, self._teiscrub, self._v2s_out, ts_file)
        else:
            self.connect(depad, ts_file)

        # ── Diagnostic taps ───────────────────────────────────────────────
        if os.environ.get("STVT_DIAG"):
            os.makedirs(diag_dir, exist_ok=True)
            # Post-equalizer SOFT SYMBOLS (float). The decisive new tap:
            # equalizer port 0 emits 832-float segments. Stream them so we
            # can histogram the 8-VSB eye over file-time.
            self._eq_v2s  = blocks.vector_to_stream(gr.sizeof_float, 832)
            self._eq_sink = blocks.file_sink(gr.sizeof_float, f"{diag_dir}/eq_out.f32")
            self._eq_sink.set_unbuffered(True)
            self.connect((equalizer, 0), self._eq_v2s, self._eq_sink)
            # EQ INPUT (fs_check out = the real 8-VSB symbols the equalizer
            # receives, same 832-float segment framing). Paired to eq_out so an
            # offline testbench can run CANDIDATE equalizers on the EXACT live
            # input and score them against the C++ baseline (2026-07-26).
            self._eqin_v2s  = blocks.vector_to_stream(gr.sizeof_float, 832)
            self._eqin_sink = blocks.file_sink(gr.sizeof_float, f"{diag_dir}/eq_in.f32")
            self._eqin_sink.set_unbuffered(True)
            self.connect((fs_check, 0), self._eqin_v2s, self._eqin_sink)
            # COMPLEX matched-filter output (pre-FPLL, pre-VSB->real). This is the
            # ONLY complex point in the chain — the FPLL discards the imaginary
            # part. Needed to test the WIDELY-LINEAR equalizer on real captures:
            # carrier-correct + symbol-time it offline (align to eq_in), keep the
            # imaginary part = the vestigial-sideband info WL exploits (2026-07-26).
            self._mf_sink = blocks.file_sink(gr.sizeof_gr_complex,
                                             f"{diag_dir}/mf_complex.cf32")
            self._mf_sink.set_unbuffered(False)
            self.connect((rxf, 0), self._mf_sink)
            # Post-deinterleaver (RS input, 207-byte blocks)
            self._dei_v2s  = blocks.vector_to_stream(gr.sizeof_char, 207)
            self._dei_sink = blocks.file_sink(gr.sizeof_char, f"{diag_dir}/dei_out.bin")
            self._dei_sink.set_unbuffered(True)
            self.connect((deinterleaver, 0), self._dei_v2s, self._dei_sink)
            # RS output (188-byte packets, corrected)
            self._rs_v2s  = blocks.vector_to_stream(gr.sizeof_char, 188)
            self._rs_sink = blocks.file_sink(gr.sizeof_char, f"{diag_dir}/rs_out.bin")
            self._rs_sink.set_unbuffered(True)
            self.connect((rs, 0), self._rs_v2s, self._rs_sink)
            # Derand output (final TS, pre-TEIscrub) — 188-byte vector items.
            self._derand_v2s  = blocks.vector_to_stream(gr.sizeof_char, 188)
            self._derand_sink = blocks.file_sink(gr.sizeof_char, f"{diag_dir}/derand_out.bin")
            self._derand_sink.set_unbuffered(True)
            self.connect(derand, self._derand_v2s, self._derand_sink)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iq", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--repeat", action="store_true")
    ap.add_argument("--diag-dir", default=os.environ.get("STVT_DIAG_DIR",
                                                          "/home/user/diag_taps"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    iq = Path(args.iq).resolve()
    if not iq.exists():
        print(f"IQ not found: {iq}", file=sys.stderr); sys.exit(2)

    log_fd = os.open(args.log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(log_fd, 1); os.dup2(log_fd, 2); os.close(log_fd)

    sz = iq.stat().st_size; samples = sz // 8
    print(f"[tv_replay] iq={iq} {sz} bytes {samples} samples "
          f"~{samples/ATSC_NATIVE_SAMPLE_RATE:.1f}s @8MS/s", flush=True)

    tb = ReplayTopBlock(iq, Path(args.out).resolve(), args.repeat, args.diag_dir)

    def _stop(s, f):
        print("[tv_replay] stop", flush=True); tb.stop(); tb.wait(); sys.exit(0)
    signal.signal(signal.SIGINT, _stop); signal.signal(signal.SIGTERM, _stop)

    print("[tv_replay] start", flush=True)
    t0 = time.time()
    tb.start(); tb.wait()
    print(f"[tv_replay] DONE elapsed={time.time()-t0:.1f}s "
          f"out={Path(args.out).stat().st_size if Path(args.out).exists() else 0}",
          flush=True)


if __name__ == "__main__":
    main()
