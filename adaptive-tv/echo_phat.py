"""echo_phat.py — GCC-PHAT channel-echo estimator (Echo X-Ray v2 PoC).

Idea from the TDOA literature (2026-07-10): correlate the received
signal against the KNOWN field-sync training sequence, but whiten the
cross-spectrum first (PHAT: keep only phase). Phase-only correlation
deconvolves the pulse shape, so every multipath copy shows as a sharp
spike instead of a smeared lobe — a cleaner echo map than the
equalizer-tap X-ray, from the same signal.

Also implements sub-sample delay interpolation (frequency-domain
zero-padding) for delay resolution far below the 92.9 ns symbol period.

Pipeline: specimen .cs16 -> real front end (rx filter, FPLL, sync,
fs_checker) stopped BEFORE the equalizer (multipath intact) -> find
field-sync segments via plinfo -> correlate the 515 known symbols
(4 seg-sync + PN511; the PN63 triple is field-dependent so excluded)
against a 3-segment window -> average |corr| over all field syncs.

    python echo_phat.py lab/captures/discone_rf7_canyon.cs16
    python echo_phat.py <iq.cs16> --beta 0.7 --secs 3 --json out.json
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SEG = 832                     # symbols per ATSC data segment
SYM_RATE = 10.7622e6          # symbols/s
FL_FS1, FL_FS2 = 0x0002, 0x0004

# ── known training reference (segment sync + PN511), levels ±5 ──────
PN511_TXT = None


def training_reference():
    """First 515 symbols of the field sync: 1001 seg-sync + PN511.
    Read straight from the decoder's own table so there is exactly one
    source of truth (gr-atscplus/lib/atsc_pnXXX_impl.h)."""
    hdr = (Path(r"Z:\src\magic-tv-decoder\gr-atscplus\lib\atsc_pnXXX_impl.h")
           .read_text(encoding="utf-8", errors="replace"))
    start = hdr.index("atsc_pn511[511]")
    body = hdr[hdr.index("{", start) + 1: hdr.index("}", start)]
    bits = [int(tok) for tok in body.replace("\n", " ").split(",")
            if tok.strip() in ("0", "1")]
    assert len(bits) == 511, f"parsed {len(bits)} PN511 bits"
    seg_sync = [1, 0, 0, 1]
    return np.array([5.0 if b else -5.0 for b in seg_sync + bits],
                    dtype=np.float32)


# ── front end: specimen -> pre-equalizer segments + plinfo ──────────
def dump_segments(iq_path, secs, sps=1.1):
    """Run the real chain front end, dump (segments f32[N,832], plinfo
    u16 flags[N]). Uses the chain dialect (SPS 1.1) like tv_live."""
    from gnuradio import gr, blocks, analog
    from gnuradio import filter as gr_filter
    from gnuradio import atscplus
    from gnuradio.dtv.atsc_rx_filter import atsc_rx_filter, ATSC_SYMBOL_RATE

    RESAMP_I, RESAMP_D = 25, 32
    RX_RATE = 6_250_000
    out_dir = Path(tempfile.mkdtemp(prefix="phat_"))
    seg_f = out_dir / "segs.f32"
    pli_f = out_dir / "pli.bin"

    class TB(gr.top_block):
        def __init__(self):
            super().__init__("echo_phat_frontend")
            src = blocks.file_source(gr.sizeof_short, str(iq_path), False)
            s2c = blocks.interleaved_short_to_complex(False, False, 32767.0)
            scaler = blocks.multiply_const_cc(32768.0)
            resamp = gr_filter.rational_resampler_ccc(
                interpolation=RESAMP_I, decimation=RESAMP_D)
            rxf = atsc_rx_filter(RX_RATE, sps)
            fpll = atscplus.atsc_fpll_tight(ATSC_SYMBOL_RATE * sps,
                                            0.001, 25.0)
            dcr = gr_filter.dc_blocker_ff(32)
            agc = analog.agc_ff(1e-6, 4.0)
            sync = atscplus.atsc_sync_soft(ATSC_SYMBOL_RATE * sps)
            fsc = atscplus.atsc_fs_checker_inst()
            # cap the run: secs of segments (12923 segs/s ≈ 1 field/24ms)
            nsegs = int(secs * 12923)
            head_s = blocks.head(gr.sizeof_float * SEG, nsegs)
            head_p = blocks.head(4, nsegs)          # sizeof(plinfo) == 4
            sink_s = blocks.file_sink(gr.sizeof_float * SEG, str(seg_f))
            sink_p = blocks.file_sink(4, str(pli_f))
            self.connect(src, s2c, scaler, resamp, rxf, fpll, dcr, agc,
                         sync, fsc)
            self.connect((fsc, 0), head_s, sink_s)
            self.connect((fsc, 1), head_p, sink_p)

    tb = TB()
    tb.run()
    segs = np.fromfile(seg_f, dtype=np.float32)
    n = len(segs) // SEG
    segs = segs[:n * SEG].reshape(n, SEG)
    raw = np.fromfile(pli_f, dtype=np.uint16)
    flags = raw[0: 2 * n: 2]
    segno = raw[1: 2 * n: 2].astype(np.int16)   # -1 = FIELD SYNC segment
    return segs, flags[:n], segno[:n]


# ── GCC estimators ───────────────────────────────────────────────────
def gcc(window, ref, beta=1.0, upsample=16):
    """Generalized cross-correlation of known ref against window.
    beta=0 -> plain correlation; beta=1 -> full PHAT (phase only).
    Zero-pads the cross-spectrum by `upsample` for sub-sample delays.
    Returns (delays_in_symbols, |corr| normalized to its max)."""
    n = len(window)
    nfft = 1
    while nfft < n * 2:
        nfft *= 2
    X = np.fft.rfft(window, nfft)
    P = np.fft.rfft(ref, nfft)
    S = X * np.conj(P)
    if beta > 0:
        mag = np.abs(S)
        floor = np.median(mag) * 1e-3 + 1e-12
        S = S / np.maximum(mag, floor) ** beta
    # sub-sample: zero-pad the spectrum before inverting
    S_up = np.zeros(nfft * upsample // 2 + 1, dtype=complex)
    S_up[: len(S)] = S
    corr = np.fft.irfft(S_up, nfft * upsample)
    corr = np.abs(corr[: n * upsample])
    m = corr.max()
    return corr / (m if m > 0 else 1.0)


def instrument_floor(ref, window_len, beta, upsample):
    """SELF-CALIBRATION: correlate the reference against an IDEAL
    noiseless field-sync segment (the full 700 known symbols, echo-free).
    The result is the instrument's own point-spread function — its
    structural sidelobes (PN autocorrelation, PN511-vs-PN63 cross
    terms). A measured peak is only believed as a REAL echo if it rises
    clearly above this floor at the same delay. (First attempt blanked
    those delays outright — that erased real near echoes, because PN
    near-sidelobes overlap the physical near-echo region. Comparing
    against the floor keeps both honest.)"""
    hdr = (Path(r"Z:\src\magic-tv-decoder\gr-atscplus\lib\atsc_pnXXX_impl.h")
           .read_text(encoding="utf-8", errors="replace"))
    s = hdr.index("atsc_pn63[63]")
    body = hdr[hdr.index("{", s) + 1: hdr.index("}", s)]
    pn63 = [int(t) for t in body.replace("\n", " ").split(",")
            if t.strip() in ("0", "1")]
    # BOTH field types: field 2 inverts the middle PN63, creating its
    # own PN511-x-PN63 cross terms (a field-2-only sidelobe at +34 µs
    # masqueraded as a real echo on two different antennas until this).
    # The floor is the worst case of the two.
    corr = None
    for mask_bit in (0, 1):
        pn63_mid = [b ^ mask_bit for b in pn63]
        full = (list(ref)
                + [5.0 if b else -5.0 for b in pn63]
                + [5.0 if b else -5.0 for b in pn63_mid]
                + [5.0 if b else -5.0 for b in pn63])
        ideal = np.zeros(window_len, dtype=np.float32)
        ideal[SEG: SEG + len(full)] = full      # middle segment = FS
        c = gcc(ideal, ref, beta=beta, upsample=upsample)
        corr = c if corr is None else np.maximum(corr, c)
    return corr, int(np.argmax(corr))


def peak_table(corr, upsample, main_idx=None, min_db=-30.0, guard_syms=1.0,
               mask=None, noise_db=None):
    """Extract echo peaks relative to the strongest (main) path.
    Returns list of (delay_us_rel_main, dB_rel_main), main first."""
    if main_idx is None:
        main_idx = int(np.argmax(corr))
    sym_ns = 1e9 / SYM_RATE
    guard = int(guard_syms * upsample)
    peaks = []
    c = corr.copy()
    floor_corr, floor_main = mask if mask else (None, None)
    thr = 10 ** (min_db / 20.0)
    for _ in range(12):
        i = int(np.argmax(c))
        v = c[i]
        if v < thr:
            break
        delay_us = ((i - main_idx) / upsample) * sym_ns / 1000.0
        db = round(20 * np.log10(max(v, 1e-9)), 1)
        margin = None
        if floor_corr is not None:
            j = floor_main + (i - main_idx)
            fl = floor_corr[j] if 0 <= j < len(floor_corr) else 0.0
            fl_db = 20 * np.log10(max(fl, 1e-9))
            margin = round(db - fl_db, 1)   # dB above the instrument floor
        if noise_db is not None and margin is not None:
            margin = round(min(margin, db - (noise_db + 0.0)), 1)
        # MODELED-RANGE LIMIT: beyond ~20 us the ideal-FS floor is
        # incomplete (mode/reserved/precode symbols are partly data-
        # dependent and unmodelable), so far peaks can't be verified.
        # Physical aiming echoes live well inside +/-20 us anyway.
        if abs(delay_us) > 20.0 and margin is not None:
            margin = -abs(margin)          # force 'not real', keep info
        peaks.append((delay_us, db, margin))
        lo, hi = max(0, i - guard), min(len(c), i + guard + 1)
        c[lo:hi] = 0.0
    peaks.sort(key=lambda p: -10 ** (p[1] / 20))
    return peaks


def lobe_width(corr, upsample):
    """-3 dB width of the main peak, in nanoseconds (sharpness metric)."""
    i = int(np.argmax(corr))
    half = corr[i] / np.sqrt(2)
    lo = i
    while lo > 0 and corr[lo] > half:
        lo -= 1
    hi = i
    while hi < len(corr) - 1 and corr[hi] > half:
        hi += 1
    return round((hi - lo) / upsample * (1e9 / SYM_RATE), 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("iq")
    ap.add_argument("--secs", type=float, default=2.5,
                    help="seconds of specimen to process (>=2 fields)")
    ap.add_argument("--beta", type=float, default=None,
                    help="single beta; default compares 0 / 0.7 / 1.0")
    ap.add_argument("--upsample", type=int, default=16)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    ref = training_reference()
    print(f"[phat] front end on {Path(args.iq).name} "
          f"({args.secs}s of segments)...", flush=True)
    t0 = time.time()
    segs, flags, segno = dump_segments(args.iq, args.secs)
    # fs_checker emits the FIELD SYNC segment as segno == -1 (the stock
    # equalizer keys on exactly this; flags 0x02/0x04 are never used)
    fs_idx = np.where(segno == -1)[0]
    # need prev+this+next segments for echo tails
    fs_idx = fs_idx[(fs_idx > 0) & (fs_idx < len(segs) - 1)]
    print(f"[phat] {len(segs)} segments, {len(fs_idx)} field syncs, "
          f"front end {time.time()-t0:.1f}s", flush=True)
    if len(fs_idx) == 0:
        print("[phat] NO FIELD SYNCS — specimen too damaged for this "
              "estimator (it needs at least sync lock)")
        sys.exit(1)

    betas = [args.beta] if args.beta is not None else [0.0, 0.7, 1.0]
    out = {"file": Path(args.iq).name, "n_fs": int(len(fs_idx)),
           "upsample": args.upsample, "estimators": {}}
    for beta in betas:
        floor = instrument_floor(ref, 3 * SEG, beta, args.upsample)
        acc = None
        for i in fs_idx:
            window = np.concatenate((segs[i - 1], segs[i], segs[i + 1]))
            corr = gcc(window, ref, beta=beta, upsample=args.upsample)
            acc = corr if acc is None else acc + corr
        acc /= len(fs_idx)
        acc /= acc.max()
        # EMPIRICAL DATA-NOISE FLOOR: the window contains live broadcast
        # data; PN-vs-data correlation forms a stable plateau (~-20 dB)
        # that the noiseless ideal floor can't model — identical "+34 µs
        # echoes" on two different antennas exposed it. A peak must beat
        # BOTH floors by 6 dB to be believed.
        main_i = int(np.argmax(acc))
        far = np.concatenate((acc[: max(0, main_i - 40 * args.upsample)],
                              acc[main_i + 40 * args.upsample:]))
        noise_db = round(20 * np.log10(np.median(far) + 1e-12), 1)
        peaks = peak_table(acc, args.upsample, mask=floor,
                           noise_db=noise_db)
        width = lobe_width(acc, args.upsample)
        name = ("plain-xcorr" if beta == 0 else
                f"PHAT-b{beta:g}" if beta < 1 else "GCC-PHAT")
        out["estimators"][name] = {
            "main_lobe_ns": width,
            "data_noise_floor_db": noise_db,
            "peaks": [{"delay_us": round(d, 3), "db": v,
                       "margin_db": m,
                       "real": bool(m is not None and m >= 6.0)}
                      for d, v, m in peaks[:10]],
        }
        print(f"\n=== {name} (beta={beta}) ===")
        print(f"  main-lobe -3dB width: {width} ns   data-noise floor: {noise_db} dB")
        print(f"  peaks (delay µs, dB, margin-above-instrument-floor):")
        for d, v, m in peaks[:10]:
            if abs(d) < 0.01:
                tag = "MAIN"
            elif m is not None and m >= 6.0:
                tag = ("REAL pre-echo" if d < 0 else "REAL post-echo")
            else:
                tag = "ref-sidelobe (not a real echo)"
            print(f"    {d:+9.3f} µs  {v:6.1f} dB  "
                  f"{'+' + str(m) if m is not None else '  ?':>7s} dB   {tag}")
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1),
                                   encoding="utf-8")
        print(f"\n[phat] wrote {args.json}")


if __name__ == "__main__":
    main()
