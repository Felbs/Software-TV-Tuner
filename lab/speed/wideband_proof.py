"""Proof: TWO ATSC channels' pilots detected from ONE wide capture.

Synthesizes a wideband window from real OTA fixtures: upsample two adjacent
8 MS/s fixture captures to 16 MS/s, shift them to +/-3 MHz (the geometry a
single LO parked between two 6 MHz channels produces), sum, then run ONE FFT
and read BOTH pilot bins.

Also measures the fast-convolution channelizer (whole_band.py math) cost for
extracting a 6 MHz TV channel from the wide window.
Read-only on fixtures. No SDR.
"""
import json
import time
from pathlib import Path
import numpy as np
from scipy.signal import resample_poly

FIX = Path(r"Z:\src\magic-tv-decoder\tools\scan_lab\fixtures")
FS = 8_000_000
FSW = 16_000_000          # synthetic wide capture rate
TRUE = {9, 15, 21, 27, 31, 34, 35, 36}
PILOT_OFF = -2.690e6      # pilot relative to a channel's own center


def load(rf):
    return np.fromfile(FIX / f"rf{rf:02d}.cf32", dtype=np.complex64)


def make_wide(rf_lo, rf_hi, n=1 << 19):
    """LO parked midway between the two channel centers -> ch_lo center at
    -3 MHz, ch_hi center at +3 MHz in the wide window."""
    a = resample_poly(load(rf_lo)[:n // 2], 2, 1).astype(np.complex64)
    b = resample_poly(load(rf_hi)[:n // 2], 2, 1).astype(np.complex64)
    m = min(len(a), len(b), n)
    t = np.arange(m) / FSW
    a = a[:m] * np.exp(-2j * np.pi * 3.0e6 * t)
    b = b[:m] * np.exp(+2j * np.pi * 3.0e6 * t)
    return (a + b).astype(np.complex64)


def pilots_from_wide(x, fs, offsets_hz, n_fft=32768, asym_mhz=2.0):
    """ONE FFT of the wide window; read pilot metrics at each requested
    offset. offsets_hz = absolute pilot frequency offsets from window center."""
    nseg = max(1, len(x) // n_fft)
    win = np.hanning(n_fft).astype(np.float32)
    psd = np.zeros(n_fft)
    used = 0
    for k in range(nseg):
        s = x[k * n_fft:(k + 1) * n_fft]
        if len(s) < n_fft:
            break
        psd += np.abs(np.fft.fftshift(np.fft.fft(s * win, n_fft))) ** 2
        used += 1
    psd /= max(1, used)
    bh = fs / n_fft
    out = []
    for off in offsets_hz:
        pb = n_fft // 2 + int(round(off / bh))
        pw = max(1, int(round(2e3 / bh)))
        plo, phi = max(0, pb - pw), min(n_fft, pb + pw + 1)
        peak = float(np.max(psd[plo:phi]))
        # noise ref: outer 15% of the window (guaranteed outside both channels)
        edge = int(0.15 * n_fft)
        nf = max(float(np.median(np.concatenate([psd[:edge], psd[-edge:]]))), 1e-20)
        nw = int(round(100e3 / bh))
        nb = psd[max(0, pb - nw):min(n_fft, pb + nw + 1)].copy()
        i0 = max(0, plo - max(0, pb - nw))
        nb[i0:i0 + (phi - plo)] = 0
        nz = nb[nb > 0]
        nm = float(np.mean(nz)) if nz.size else nf
        bb = max(1, int(round(asym_mhz * 1e6 / bh)))
        ab = float(np.mean(psd[pb:min(n_fft, pb + bb)]))
        bl = float(np.mean(psd[max(0, pb - bb):pb]))
        out.append((10 * np.log10(peak / nf + 1e-20),
                    10 * np.log10(peak / nm + 1e-20),
                    10 * np.log10(ab / max(bl, 1e-20) + 1e-20)))
    return out, used


print("=== dual-channel wideband pilot detection (16 MS/s synthetic window) ===")
print("LO parked midway between two 6 MHz channels; channel centers at -/+3 MHz")
print("=> pilot_lo at -5.690 MHz, pilot_hi at +0.310 MHz")
print()
print(f"{'pair':<14} {'ch':<5} {'truth':<7} {'snr':>7} {'sharp':>7} {'asym':>7}")
PAIRS = [(34, 35), (35, 36), (26, 27), (30, 31), (14, 15), (20, 21),
         (8, 9), (18, 19), (28, 29), (32, 33), (22, 23), (24, 25)]
res = {}
for lo, hi in PAIRS:
    try:
        w = make_wide(lo, hi)
    except Exception as e:
        print(f"{lo}/{hi}: {e}")
        continue
    offs = [-3.0e6 + PILOT_OFF, +3.0e6 + PILOT_OFF]   # -5.690, +0.310 MHz
    (mlo, mhi), nseg = pilots_from_wide(w, FSW, offs)
    for rf, m in ((lo, mlo), (hi, mhi)):
        res[(lo, hi, rf)] = m
        print(f"RF{lo}+RF{hi:<9} RF{rf:<3} {'REAL' if rf in TRUE else 'empty':<7} "
              f"{m[0]:7.1f} {m[1]:7.1f} {m[2]:7.1f}")

print()
print("=== separation check: sharpness, REAL vs empty, in the wide window ===")
real = [v[1] for k, v in res.items() if k[2] in TRUE]
emp = [v[1] for k, v in res.items() if k[2] not in TRUE]
print(f"REAL  sharpness: min {min(real):.1f}  median {np.median(real):.1f}  n={len(real)}")
print(f"EMPTY sharpness: max {max(emp):.1f}  median {np.median(emp):.1f}  n={len(emp)}")
print(f"=> separation gap = {min(real) - max(emp):+.1f} dB")

print()
print("=== timing: one 32768-pt FFT over a 2 ms wide window ===")
w = make_wide(34, 35)
n = 32768
t0 = time.perf_counter()
for _ in range(200):
    np.fft.fft(w[:n] * np.hanning(n))
dt = (time.perf_counter() - t0) / 200
print(f"single {n}-pt FFT: {dt*1e3:.2f} ms  "
      f"(covers {n/FSW*1e3:.2f} ms of a 16 MS/s window = 2 TV channels)")

print()
print("=== fast-convolution channelizer cost (whole_band.py math, TV-sized) ===")
# extract a 6 MHz slice from a 16 MS/s window, 1 second of data
sec = np.zeros(FSW, dtype=np.complex64)
sec[:len(w)] = w[:FSW] if len(w) >= FSW else w
t0 = time.perf_counter()
X = np.fft.fft(sec)
t_fft = time.perf_counter() - t0
half = int(8e6 / 2 / FSW * FSW)          # 8 MHz output slice
i0 = FSW // 2 - int(3e6 / FSW * FSW)
t0 = time.perf_counter()
idx = (np.arange(-half, half) + i0) % FSW
y = np.fft.ifft(np.fft.fftshift(X[idx]))
t_slice = time.perf_counter() - t0
print(f"1 s @ 16 MS/s: forward FFT {t_fft*1e3:.0f} ms, per-channel slice+iFFT "
      f"{t_slice*1e3:.0f} ms")
print(f"=> channelizing 2 TV channels from 1 s of 16 MS/s costs "
      f"~{(t_fft + 2*t_slice)*1e3:.0f} ms CPU ({(t_fft+2*t_slice):.2f}x realtime "
      f"budget used: {(t_fft+2*t_slice)*100:.0f}%)")
