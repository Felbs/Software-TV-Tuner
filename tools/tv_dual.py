#!/usr/bin/env python3
"""tv_dual.py — DUAL-DECODE A/B harness: one input stream, two equalizers.

The instrument that turns "is the widely-linear equalizer better?" from a guess
into a measurement.

The problem it solves
---------------------
Every WL-vs-long comparison so far has been TWO SEPARATE RUNS. Live, that means
two different slices of a fading channel (run-to-run channel variance swamps a
few-percent equalizer difference). Offline it means two passes with independent
adaptation trajectories. Either way the human is asked to judge a difference
smaller than the noise in the measurement.

tv_dual runs ONE flowgraph in which a SINGLE shared front end
(file -> resampler -> matched filter -> FPLL(fold) -> fused WL front end)
feeds BOTH equalizers from the SAME sample stream:

    wl_frontend out0 (REAL 832-sym segments) --+--> atsc_equalizer_wl   in0
                                               `--> atsc_equalizer_long in0
    wl_frontend out1 (plinfo)                --+--> both in1
    wl_frontend out2 (IMAG segments)          ---> atsc_equalizer_wl   in2

Each equalizer then drives its OWN backend (viterbi / deinterleaver / RS /
derandomizer / depad / TEI scrub) into its OWN transport stream. The two TS
files are therefore produced from BIT-IDENTICAL symbols: every difference
between them is the equalizer and nothing else.

Because the fused front end is a verbatim port of atsc_sync_soft +
atsc_fs_checker_inst, the `long` leg here is the production v1 decode path
(with STVT_FPLL_FOLD=1) — verifiable by md5 against a plain tv_replay run
(`--fidelity` prints the hash to compare).

Usage
-----
    python tools/tv_dual.py --iq lab/marginal_iq/rf34_ctrl.cs16 \
        --outdir lab/wl_v3/run1 [--noise 2147] [--seed 42] [--tag rf34_k016]

    python tools/tv_dual.py --score lab/wl_v3/run1      # re-score without decoding

Outputs (in --outdir):
    <tag>_long.ts / <tag>_wl.ts      the two transport streams
    <tag>.log                        chain log (both equalizers' telemetry)
    <tag>.json                       the measurement: frames, MER percentiles,
                                     per-field WL advantage, delivered-frames
                                     per MER bin

Env: reads the same STVT_* knobs as tv_replay/tv_live. STVT_FPLL_FOLD=1 is
forced (the complex companion requires it). STVT_EQ is ignored — this harness
runs BOTH equalizers by construction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

LOG = logging.getLogger("tv_dual")

ATSC_NATIVE_SAMPLE_RATE = 8_000_000
ATSC_RX_SAMPLE_RATE = 6_250_000
RESAMP_INTERP = 25
RESAMP_DECIM = 32


# ── decode ────────────────────────────────────────────────────────────────
def _build_topblock(iq_path: Path, out_long: Path, out_wl: Path,
                    repeat: bool, diag_dir: str | None):
    from gnuradio import gr, blocks, analog, dtv
    from gnuradio import filter as gr_filter
    from gnuradio import atscplus
    from gnuradio.dtv.atsc_rx_filter import atsc_rx_filter, ATSC_SYMBOL_RATE

    class DualTopBlock(gr.top_block):
        def __init__(self):
            super().__init__("tv_dual")

            _ext = str(iq_path).lower()
            _fmt = os.environ.get(
                "STVT_IQ_FORMAT",
                "cs16" if _ext.endswith((".cs16", ".sc16")) else "cf32")
            if _fmt == "cs16":
                _fsrc = blocks.file_source(gr.sizeof_short, str(iq_path), repeat)
                _s2c = blocks.interleaved_short_to_complex(False, False, 32767.0)
                self.connect(_fsrc, _s2c)
                src = _s2c
                LOG.info(f"input: CS16 {iq_path}")
            else:
                src = blocks.file_source(gr.sizeof_gr_complex, str(iq_path), repeat)
                LOG.info(f"input: CF32 {iq_path}")

            _skip = int(os.environ.get("STVT_IQ_SKIP", "0"))
            if _skip:
                _sk = blocks.skiphead(gr.sizeof_gr_complex, _skip)
                self.connect(src, _sk)
                src = _sk

            SPS = float(os.environ.get("STVT_SPS", "1.5"))
            output_rate = ATSC_SYMBOL_RATE * SPS

            scaler = blocks.multiply_const_cc(32768.0)

            _noise_amp = float(os.environ.get("STVT_ADD_NOISE", "0"))
            noise_src = noise_add = None
            if _noise_amp > 0:
                noise_src = analog.noise_source_c(
                    analog.GR_GAUSSIAN, _noise_amp,
                    int(os.environ.get("STVT_NOISE_SEED", "42")))
                noise_add = blocks.add_cc()
                LOG.info(f"AWGN: amp={_noise_amp} seed="
                         f"{os.environ.get('STVT_NOISE_SEED', '42')} (SHARED — both "
                         f"equalizers see the identical noisy stream)")

            resamp = gr_filter.rational_resampler_ccc(
                interpolation=RESAMP_INTERP, decimation=RESAMP_DECIM)
            rxf = atsc_rx_filter(ATSC_RX_SAMPLE_RATE, SPS)
            fpll = atscplus.atsc_fpll_tight(
                output_rate,
                float(os.environ.get("STVT_FPLL_ALPHA", "0.001")),
                float(os.environ.get("STVT_FPLL_AFC_TAU", "25")))
            wl_front = atscplus.atsc_wl_frontend(output_rate)

            chain = [src, scaler]
            if noise_add is not None:
                chain.append(noise_add)
            chain += [resamp, rxf, fpll]
            self.connect(*chain)
            if noise_add is not None:
                self.connect(noise_src, (noise_add, 1))

            self.connect((fpll, 0), (wl_front, 0))   # real (folded dcr+agc)
            self.connect((fpll, 1), (wl_front, 1))   # imag companion

            eq_wl = atscplus.atsc_equalizer_wl()
            eq_long = atscplus.atsc_equalizer_long()

            # THE TEE: identical segments + identical plinfo into both.
            self.connect((wl_front, 0), (eq_wl, 0))
            self.connect((wl_front, 1), (eq_wl, 1))
            self.connect((wl_front, 2), (eq_wl, 2))
            self.connect((wl_front, 0), (eq_long, 0))
            self.connect((wl_front, 1), (eq_long, 1))

            self._legs = {}
            for name, eq, ts_path in (("long", eq_long, out_long),
                                      ("wl", eq_wl, out_wl)):
                self._legs[name] = self._backend(name, eq, ts_path)

            if diag_dir:
                os.makedirs(diag_dir, exist_ok=True)
                self._diag = []
                for port, fname in ((0, "eq_in.f32"), (2, "eq_imag.f32")):
                    v2s = blocks.vector_to_stream(gr.sizeof_float, 832)
                    snk = blocks.file_sink(gr.sizeof_float, f"{diag_dir}/{fname}")
                    snk.set_unbuffered(False)   # GB-scale dumps: buffered
                    self.connect((wl_front, port), v2s, snk)
                    self._diag += [v2s, snk]
                for eq, fname in ((eq_wl, "eq_out_wl.f32"),
                                  (eq_long, "eq_out_long.f32")):
                    v2s = blocks.vector_to_stream(gr.sizeof_float, 832)
                    snk = blocks.file_sink(gr.sizeof_float, f"{diag_dir}/{fname}")
                    snk.set_unbuffered(False)   # GB-scale dumps: buffered
                    self.connect((eq, 0), v2s, snk)
                    self._diag += [v2s, snk]
                LOG.info(f"diag taps -> {diag_dir}")

        def _backend(self, name, equalizer, ts_path: Path):
            """One complete post-equalizer decode leg (its own blocks)."""
            import numpy as np

            _vit = os.environ.get("STVT_VITERBI", "hard")
            viterbi = (atscplus.atsc_viterbi_soft() if _vit == "soft"
                       else dtv.atsc_viterbi_decoder())
            _rs_kind = os.environ.get("STVT_RS", "stock")
            _use_tagged_dei = (_vit == "soft" and _rs_kind == "erasure")
            deinterleaver = (atscplus.atsc_deinterleaver() if _use_tagged_dei
                             else dtv.atsc_deinterleaver())
            if _rs_kind == "erasure":
                rs = atscplus.atsc_rs_decoder_erasure(
                    int(os.environ.get("STVT_RS_ERASURES", "14")))
            else:
                rs = dtv.atsc_rs_decoder()
            derand = dtv.atsc_derandomizer()
            depad = dtv.atsc_depad()

            ts_file = blocks.file_sink(gr.sizeof_char, str(ts_path))
            ts_file.set_unbuffered(True)

            for a, b in [(equalizer, viterbi), (viterbi, deinterleaver),
                         (deinterleaver, rs), (rs, derand)]:
                self.connect((a, 0), (b, 0))
                self.connect((a, 1), (b, 1))

            extra = []
            if (int(os.environ.get("STVT_SOVA", "0")) and _vit == "soft"
                    and _rs_kind == "erasure"):
                dei_rel = atscplus.atsc_deinterleaver()
                self.connect((viterbi, 2), (dei_rel, 0))
                self.connect((viterbi, 1), (dei_rel, 1))
                self.connect((dei_rel, 0), (rs, 2))
                rel_pl_sink = blocks.null_sink(gr.sizeof_char * 4)
                self.connect((dei_rel, 1), rel_pl_sink)
                extra += [dei_rel, rel_pl_sink]
                if int(os.environ.get("STVT_TURBO", "0")):
                    self.connect((equalizer, 0), (rs, 3))
                    equalizer.set_min_output_buffer(512)

            self.connect(derand, depad)
            if os.environ.get("STVT_TEISCRUB", "1") == "1":
                class _TEIScrub(gr.sync_block):
                    def __init__(self):
                        gr.sync_block.__init__(
                            self, name=f"TEIScrub_{name}",
                            in_sig=[(np.uint8, 188)], out_sig=[(np.uint8, 188)])

                    def work(self, input_items, output_items):
                        inp = input_items[0]
                        out = output_items[0]
                        n = len(inp)
                        for i in range(n):
                            pkt = inp[i]
                            out[i] = pkt
                            if pkt[1] & 0x80:
                                out[i][0] = 0x47
                                out[i][1] = 0x1F
                                out[i][2] = 0xFF
                                out[i][3] = pkt[3] & 0x0F
                        return n

                v2s_in = blocks.stream_to_vector(gr.sizeof_char, 188)
                scrub = _TEIScrub()
                v2s_out = blocks.vector_to_stream(gr.sizeof_char, 188)
                self.connect(depad, v2s_in, scrub, v2s_out, ts_file)
                extra += [v2s_in, scrub, v2s_out]
            else:
                self.connect(depad, ts_file)

            return dict(equalizer=equalizer, viterbi=viterbi, rs=rs,
                        derand=derand, depad=depad, sink=ts_file, extra=extra)

    return DualTopBlock()


# ── scoring ───────────────────────────────────────────────────────────────
def ffmpeg_frames(ts: Path) -> int:
    """Delivered VIDEO frames via the ffmpeg null-sink (the ONLY honest metric —
    ffprobe/-count_frames lies on multi-program TS; -v error suppresses the
    stats line we parse)."""
    if not ts.exists() or ts.stat().st_size < 1_000_000:
        return 0
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
         "-err_detect", "ignore_err",
         "-analyzeduration", "100M", "-probesize", "100M",
         "-i", str(ts), "-map", "0:v", "-f", "null", "-"],
        capture_output=True, text=True)
    m = re.findall(r"frame=\s*(\d+)", r.stderr)
    return int(m[-1]) if m else 0


_RE_LONG = re.compile(
    r"\[eq-long t=\s*([\d.]+)s\] fs=(\d+) fs_err_rms=([\d.]+)")
_RE_WL = re.compile(
    r"\[eq-wl t=\s*([\d.]+)s\] fs=(\d+) fs_err_rms=([\d.]+)"
    r"(?: conj=([\d.]+))?(?: imag=([\d.]+))?"
    r"(?: ben=([-\d.]+))?(?: kap=([\d.]+))?(?: beni=([-\d.]+))?")
_RE_WL_V2 = re.compile(r"\[eq-wl\] conj_frac=([\d.]+)")


def mer_from_rms(rms: float) -> float:
    """MER dial: fs_err_rms IS the live MER (20*log10(5/err)); cliff ~15.2 dB."""
    import math
    return math.nan if rms <= 0 else 20.0 * math.log10(5.0 / rms)


def parse_telemetry(log_path: Path) -> dict:
    """Pull the per-field-sync equalizer telemetry out of the chain log."""
    long_mer, wl_mer, conj, imag, ben, kap, beni = [], [], [], [], [], [], []
    long_fs, wl_fs = [], []
    if not log_path.exists():
        return {}
    for line in log_path.read_text(errors="ignore").splitlines():
        m = _RE_LONG.search(line)
        if m:
            r = float(m.group(3))
            if r > 0:
                long_fs.append(int(m.group(2)))
                long_mer.append(mer_from_rms(r))
            continue
        m = _RE_WL.search(line)
        if m:
            r = float(m.group(3))
            if r > 0:
                wl_fs.append(int(m.group(2)))
                wl_mer.append(mer_from_rms(r))
            for grp, sink in ((4, conj), (5, imag), (6, ben), (7, kap), (8, beni)):
                if m.group(grp):
                    sink.append(float(m.group(grp)))
            continue
        m = _RE_WL_V2.search(line)
        if m:
            conj.append(float(m.group(1)))

    def pct(xs, q):
        if not xs:
            return None
        s = sorted(xs)
        i = max(0, min(len(s) - 1, int(round(q / 100.0 * (len(s) - 1)))))
        return round(s[i], 3)

    def summ(xs):
        if not xs:
            return None
        return dict(n=len(xs), p5=pct(xs, 5), p10=pct(xs, 10), p50=pct(xs, 50),
                    p90=pct(xs, 90), mean=round(statistics.fmean(xs), 3),
                    min=round(min(xs), 3), max=round(max(xs), 3))

    out = {"mer_long": summ(long_mer), "mer_wl": summ(wl_mer)}
    # PAIRED per-field advantage — only valid because both equalizers see the
    # SAME field syncs from the shared front end.
    n = min(len(long_mer), len(wl_mer))
    if n:
        d = [wl_mer[i] - long_mer[i] for i in range(n)]
        wins = sum(1 for x in d if x > 0.05)
        losses = sum(1 for x in d if x < -0.05)
        out["paired"] = dict(
            n=n, wl_mer_advantage_db=round(statistics.fmean(d), 3),
            wl_wins=wins, long_wins=losses, ties=n - wins - losses,
            worst_field_delta=round(min(d), 3), best_field_delta=round(max(d), 3))
        # conj_frac vs MER scatter (where WL earns its keep)
        if len(conj) >= n:
            out["conj_vs_mer"] = [
                [round(conj[i], 4), round(long_mer[i], 2), round(d[i], 3)]
                for i in range(0, n, max(1, n // 200))]
        # delivered-MER histogram (the watchability curve input)
        bins = {}
        for i in range(n):
            b = int(long_mer[i] // 1)
            e = bins.setdefault(b, [0, 0, 0])
            e[0] += 1
            e[1] += long_mer[i]
            e[2] += wl_mer[i]
        out["mer_bins"] = {
            str(k): dict(fields=v[0], long_mer=round(v[1] / v[0], 2),
                         wl_mer=round(v[2] / v[0], 2))
            for k, v in sorted(bins.items())}
    for key, xs in (("conj_frac", conj), ("imag_frac", imag),
                    ("imag_benefit", ben), ("kappa", kap),
                    ("imag_benefit_insample", beni)):
        if xs:
            out[key] = summ(xs)
    return out


_VSB_LEVELS = (-7.0, -5.0, -3.0, -1.0, 1.0, 3.0, 5.0, 7.0)
_VSB_RMS = 4.5825756949558398  # sqrt((1+9+25+49)/4)


def dd_mer_series(f32: Path, max_segments: int = 200_000) -> list[float]:
    """UNBIASED per-segment MER from an equalizer-output dump.

    The equalizers' own fs_err_rms is a TRAINING error measured on the field
    sync while adapting, with different adaptation laws (long: fixed-beta LMS;
    wl: mu=0.5 NLMS) — so the two are NOT comparable in absolute terms. This
    is: slice the 8-VSB data symbols to the nearest legal level and take the
    residual. Identical definition for both legs, measured on DELIVERED data
    segments, which is what a real MER meter reads.
    """
    import math
    import numpy as np
    if not f32.exists() or f32.stat().st_size < 832 * 4:
        return []
    out = []
    seg = 832
    chunk = seg * 4096
    with open(f32, "rb") as fh:
        while len(out) < max_segments:
            buf = np.fromfile(fh, dtype=np.float32, count=chunk)
            if buf.size < seg:
                break
            n = buf.size // seg
            m = buf[: n * seg].reshape(n, seg)[:, 4:]  # drop the 4 sync symbols
            lvl = np.asarray(_VSB_LEVELS, dtype=np.float32)
            idx = np.abs(m[:, :, None] - lvl[None, None, :]).argmin(axis=2)
            err = m - lvl[idx]
            rms = np.sqrt((err.astype(np.float64) ** 2).mean(axis=1))
            for r in rms:
                out.append(20.0 * math.log10(_VSB_RMS / r) if r > 0 else float("nan"))
    return out


def score(outdir: Path, tag: str, diag_dir: str | None = None) -> dict:
    ts_long = outdir / f"{tag}_long.ts"
    ts_wl = outdir / f"{tag}_wl.ts"
    log = outdir / f"{tag}.log"
    res = {
        "tag": tag,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "env": {k: v for k, v in os.environ.items() if k.startswith("STVT_")},
        "long": {"ts": str(ts_long),
                 "bytes": ts_long.stat().st_size if ts_long.exists() else 0,
                 "frames": ffmpeg_frames(ts_long)},
        "wl": {"ts": str(ts_wl),
               "bytes": ts_wl.stat().st_size if ts_wl.exists() else 0,
               "frames": ffmpeg_frames(ts_wl)},
    }
    for leg in ("long", "wl"):
        p = outdir / f"{tag}_{leg}.ts"
        if p.exists() and p.stat().st_size:
            h = hashlib.md5()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            res[leg]["md5"] = h.hexdigest()
    lf, wf = res["long"]["frames"], res["wl"]["frames"]
    res["wl_frame_advantage"] = (round((wf - lf) / lf * 100.0, 2) if lf else
                                 (None if wf == 0 else float("inf")))
    res["telemetry"] = parse_telemetry(log)

    if diag_dir:
        dd = {}
        for leg in ("long", "wl"):
            s = dd_mer_series(Path(diag_dir) / f"eq_out_{leg}.f32")
            if s:
                import math
                s = [x for x in s if not math.isnan(x)]
                ss = sorted(s)

                def q(p):
                    return round(ss[max(0, min(len(ss) - 1,
                                               int(round(p / 100 * (len(ss) - 1)))))], 3)
                dd[leg] = dict(n=len(s), p5=q(5), p10=q(10), p50=q(50), p90=q(90),
                               mean=round(statistics.fmean(s), 3))
                # the watchability curve input: how many segments land per MER bin
                hist = {}
                for x in s:
                    hist[str(int(x // 1))] = hist.get(str(int(x // 1)), 0) + 1
                dd[leg]["hist_1db"] = dict(sorted(hist.items(), key=lambda kv: int(kv[0])))
        if dd:
            res["dd_mer"] = dd
    (outdir / f"{tag}.json").write_text(json.dumps(res, indent=2))
    return res


def print_table(res: dict):
    lf, wf = res["long"]["frames"], res["wl"]["frames"]
    print(f"\n=== tv_dual A/B  [{res['tag']}] — sample-aligned, one input stream ===")
    print(f"{'leg':<6}{'frames':>9}{'TS MB':>10}{'MER p5':>9}{'p10':>8}"
          f"{'p50':>8}")
    t = res.get("telemetry", {})
    for leg, key in (("long", "mer_long"), ("wl", "mer_wl")):
        m = t.get(key) or {}
        print(f"{leg:<6}{res[leg]['frames']:>9}"
              f"{res[leg]['bytes']/1e6:>10.1f}"
              f"{str(m.get('p5', '-')):>9}{str(m.get('p10', '-')):>8}"
              f"{str(m.get('p50', '-')):>8}")
    dd = res.get("dd_mer")
    if dd:
        print("  slicer (decision-directed) MER — identical definition both legs:")
        for leg in ("long", "wl"):
            if leg in dd:
                m = dd[leg]
                print(f"    {leg:<5} n={m['n']:<7} p5 {m['p5']:>7} p10 {m['p10']:>7} "
                      f"p50 {m['p50']:>7} p90 {m['p90']:>7}")
    adv = res.get("wl_frame_advantage")
    print(f"WL frame advantage: {adv}%" if adv is not None else
          "WL frame advantage: n/a (long delivered 0)")
    p = t.get("paired")
    if p:
        print(f"paired fields n={p['n']}  WL MER advantage "
              f"{p['wl_mer_advantage_db']:+.3f} dB  "
              f"(WL better {p['wl_wins']} / long better {p['long_wins']} / "
              f"tie {p['ties']})")
    for k in ("conj_frac", "imag_frac", "imag_benefit",
              "imag_benefit_insample", "kappa"):
        if t.get(k):
            s = t[k]
            print(f"{k:<13} mean {s['mean']}  p10 {s['p10']}  p90 {s['p90']}")
    print()


# ── main ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iq")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--noise", type=float, default=None,
                    help="AWGN amplitude (shared by both legs) — cliff sweeps")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--repeat", action="store_true")
    ap.add_argument("--diag-dir", default=None)
    ap.add_argument("--score", action="store_true",
                    help="score an existing run in --outdir (no decode)")
    args = ap.parse_args()

    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    tag = args.tag or (Path(args.iq).stem if args.iq else "run")

    if args.score:
        print_table(score(outdir, tag, args.diag_dir))
        return

    if not args.iq:
        ap.error("--iq required unless --score")
    iq = Path(args.iq).resolve()
    if not iq.exists():
        print(f"IQ not found: {iq}", file=sys.stderr)
        sys.exit(2)

    if args.noise is not None:
        os.environ["STVT_ADD_NOISE"] = str(args.noise)
    if args.seed is not None:
        os.environ["STVT_NOISE_SEED"] = str(args.seed)
    # The complex companion only matches the real path when the FPLL folds the
    # dc-blocker + AGC into its own loop.
    os.environ["STVT_FPLL_FOLD"] = "1"
    os.environ.setdefault("STVT_EQ_TELEM", "1")
    os.environ.setdefault("STVT_EQ_TELEM_EVERY", "1")
    os.environ.pop("STVT_EQ", None)

    logfile = outdir / f"{tag}.log"
    log_fd = os.open(str(logfile), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    print(f"[tv_dual] iq={iq} tag={tag}", flush=True)
    tb = _build_topblock(iq, outdir / f"{tag}_long.ts", outdir / f"{tag}_wl.ts",
                         args.repeat, args.diag_dir)

    def _stop(s, f):
        print("[tv_dual] stop", flush=True)
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    t0 = time.time()
    tb.start()
    tb.wait()
    wall = time.time() - t0
    print(f"[tv_dual] DONE elapsed={wall:.1f}s", flush=True)

    res = score(outdir, tag, args.diag_dir)
    res["wall_s"] = round(wall, 1)
    (outdir / f"{tag}.json").write_text(json.dumps(res, indent=2))
    # stdout is the chain log here; also drop a standalone summary next to it
    with open(outdir / f"{tag}.summary.txt", "w") as fh:
        old = sys.stdout
        sys.stdout = fh
        print_table(res)
        sys.stdout = old
    print_table(res)


if __name__ == "__main__":
    main()
