"""wiener_seed.py — analytic equalizer taps from a measured channel.

The chain's echo X-ray measures the channel impulse response h directly
(signed, coherently averaged, dumped by STVT_EQ_CIR_DUMP). The MMSE/
Wiener equalizer for a measured channel has a CLOSED FORM:

    W(f) = H*(f) / (|H(f)|^2 + 1/SNR)

One FFT, one division, one IFFT — taps in microseconds instead of the
LMS crawl. This tool converts a CIRD dump into a TAPC tap-cache file
that tv_live's existing warm-start loads unchanged.

Alignment between the CIR's delay axis and the equalizer's tap axis is
calibrated EMPIRICALLY (--calibrate): compute Wiener taps, then find
the circular shift maximizing correlation against LMS-converged taps
from the same channel — that shift is a rig constant.

    python wiener_seed.py --cir cir.bin --taps-out taps_X.bin
                          [--snr-db 17] [--calibrate lms_taps.bin]
                          [--shift N]
"""
import argparse
import struct
import sys
from pathlib import Path

import numpy as np

NTAPS = 256
NPRETAPS = int(NTAPS * 0.2)          # 51 — the delta-init reference tap


def read_cird(path):
    b = Path(path).read_bytes()
    magic, n = struct.unpack("<II", b[:8])
    assert magic == 0x43495244, "not a CIRD file"
    h = np.frombuffer(b[8:8 + 4 * n], dtype=np.float32).astype(np.float64)
    return h


def read_tapc(path):
    b = Path(path).read_bytes()
    magic, n = struct.unpack("<II", b[:8])
    assert magic == 0x54415043, "not a TAPC file"
    return np.frombuffer(b[8:8 + 4 * n], dtype=np.float32).astype(np.float64)


def write_tapc(path, taps):
    with open(path, "wb") as f:
        f.write(struct.pack("<II", 0x54415043, len(taps)))
        f.write(taps.astype(np.float32).tobytes())


def wiener_taps(h, snr_db=17.0, nfft=1024):
    """Closed-form MMSE inverse of measured CIR h (real 8-VSB baseband)."""
    # normalize so the main path is unit gain (tap scale convention)
    peak = np.abs(h).max()
    hn = h / (peak + 1e-30)
    # noise floor of the estimate itself: bins < 2% of peak are mostly
    # correlation noise — zero them so we don't invert noise
    hn = np.where(np.abs(hn) > 0.02, hn, 0.0)
    H = np.fft.fft(hn, nfft)
    lam = 10 ** (-snr_db / 10.0)
    W = np.conj(H) / (np.abs(H) ** 2 + lam)
    w = np.real(np.fft.ifft(W))
    return w                          # nfft-long, main response near 0/wrap


def place(w, peak_delay_in_cir, shift, nfft=1024):
    """Rotate the nfft-long inverse so the equalizer's reference tap
    (NPRETAPS) carries the main coefficient, then crop to NTAPS."""
    # main coefficient of w sits at index 0 (inverse of unit-gain path).
    # The empirical constant `shift` absorbs the CIR-vs-tap axis offset.
    rot = np.roll(w, NPRETAPS + shift)
    return rot[:NTAPS].copy()


def best_shift(w, lms, nfft=1024):
    """Calibration: find shift maximizing cosine vs converged LMS taps."""
    best = (-2.0, 0)
    for s in range(-nfft // 2, nfft // 2):
        t = np.roll(w, NPRETAPS + s)[:NTAPS]
        denom = (np.linalg.norm(t) * np.linalg.norm(lms) + 1e-30)
        c = float(np.dot(t, lms) / denom)
        if c > best[0]:
            best = (c, s)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cir", required=True)
    ap.add_argument("--taps-out")
    ap.add_argument("--snr-db", type=float, default=17.0)
    ap.add_argument("--calibrate", help="LMS-converged TAPC file")
    ap.add_argument("--shift", type=int, default=0,
                    help="calibrated axis shift (rig constant)")
    args = ap.parse_args()

    h = read_cird(args.cir)
    dpeak = int(np.argmax(np.abs(h)))
    print(f"CIR: {len(h)} delays, main path at {dpeak}, "
          f"{int((np.abs(h) > 0.02 * np.abs(h).max()).sum())} significant bins")
    echoes = [(d - dpeak, 20 * np.log10(abs(h[d]) / abs(h[dpeak])))
              for d in np.argsort(np.abs(h))[::-1][1:6]
              if abs(h[d]) > 0.02 * abs(h[dpeak])]
    for d, db in echoes:
        print(f"  echo {d:+4d} syms ({d*0.0929:+.2f} us) {db:5.1f} dB")

    w = wiener_taps(h, args.snr_db)

    if args.calibrate:
        lms = read_tapc(args.calibrate)
        cos, s = best_shift(w, lms)
        print(f"CALIBRATION: best shift {s}, cosine similarity {cos:.4f}")
        t = np.roll(w, NPRETAPS + s)[:NTAPS]
        # predicted residual improvement: energy match of tap vectors
        print(f"  |wiener|={np.linalg.norm(t):.3f} |lms|={np.linalg.norm(lms):.3f}")
        args.shift = s

    taps = place(w, dpeak, args.shift)
    if args.taps_out:
        write_tapc(args.taps_out, taps)
        print(f"wrote {args.taps_out} ({NTAPS} taps, shift {args.shift})")


if __name__ == "__main__":
    main()
