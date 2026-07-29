"""Experiment 2:
(a) re-optimize thresholds at each dwell -> the TRUE minimum integration time.
(b) does the recipe survive the "2 pilots in one 8 MHz window" geometry,
    i.e. a pilot parked 3.0 MHz off window center with only +/-2 MHz of
    asymmetry band and a squeezed noise reference?
Read-only on fixtures. No SDR.
"""
import json
import itertools
from pathlib import Path
import numpy as np

FIX = Path(r"Z:\src\magic-tv-decoder\tools\scan_lab\fixtures")
FS = 8_000_000
TRUE = {9, 15, 21, 27, 31, 34, 35, 36}


def metrics(x, fs, n_fft, pilot_off_hz=-2.690e6, asym_mhz=3.0,
            oob_mhz=3.5, nbhd_khz=100.0):
    if x.size < n_fft:
        return None
    win = np.hanning(n_fft).astype(np.float32)
    nseg = x.size // n_fft
    psd = np.zeros(n_fft)
    for k in range(nseg):
        psd += np.abs(np.fft.fftshift(np.fft.fft(x[k*n_fft:(k+1)*n_fft]*win, n_fft)))**2
    psd /= max(1, nseg)
    bh = fs / n_fft
    pb = n_fft//2 + int(round(pilot_off_hz/bh))
    pw = max(1, int(round(2e3/bh)))
    plo, phi = max(0, pb-pw), min(n_fft, pb+pw+1)
    peak = float(np.max(psd[plo:phi]))
    mb = int(round(oob_mhz*1e6/bh))
    oob = np.concatenate([psd[:max(0, n_fft//2-mb)], psd[min(n_fft, n_fft//2+mb):]])
    nf = max(float(np.median(oob)) if oob.size else float(np.median(psd)), 1e-20)
    nw = int(round(nbhd_khz*1e3/bh))
    nlo, nhi = max(0, pb-nw), min(n_fft, pb+nw+1)
    nb = psd[nlo:nhi].copy()
    nb[max(0, plo-nlo):max(max(0, plo-nlo), phi-nlo)] = 0
    nz = nb[nb > 0]
    nm = float(np.mean(nz)) if nz.size else nf
    b = max(1, int(round(asym_mhz*1e6/bh)))
    ab = float(np.mean(psd[pb:min(n_fft, pb+b)]))
    bb = float(np.mean(psd[max(0, pb-b):pb]))
    return (10*np.log10(peak/nf+1e-20), 10*np.log10(peak/nm+1e-20),
            10*np.log10(ab/max(bb, 1e-20)+1e-20))


def score(scores, ts, tsh, ta):
    tp = fp = 0
    mar = []
    for rf, (s, sh, a) in scores.items():
        hit = s >= ts and sh >= tsh and a >= ta
        if rf in TRUE:
            if hit:
                tp += 1
                mar.append(min(s-ts, sh-tsh, a-ta))
        elif hit:
            fp += 1
    return tp, fp, (min(mar) if mar else -99)


def best_recipe(scores):
    """grid search thresholds maximizing (TP - 10*FP, then min margin)"""
    S = sorted({round(v[0], 1) for v in scores.values()})
    SH = sorted({round(v[1], 1) for v in scores.values()})
    A = sorted({round(v[2], 1) for v in scores.values()})
    best = None
    for ts in S:
        for tsh in SH:
            for ta in A:
                tp, fp, m = score(scores, ts, tsh, ta)
                key = (tp - 10*fp, m)
                if best is None or key > best[0]:
                    best = (key, (ts, tsh, ta, tp, fp, m))
    return best[1]


caps = json.load(open(FIX/"manifest.json"))["captures"]
data = {}
for rf, c in caps.items():
    data[int(rf)] = np.fromfile(FIX/c["file"], dtype=np.complex64)

print("=== (a) RE-OPTIMIZED thresholds vs dwell  (8 true channels, 27 empty) ===")
print(f"{'ms':>7} {'nfft':>6} {'thr snr/sharp/asym':>22} {'TP':>3} {'FP':>3} {'minmarg':>8}")
for n, nfft in [(2048, 2048), (4096, 4096), (8192, 8192), (12288, 8192),
                (16384, 16384), (32768, 16384), (163840, 16384), (1600000, 16384)]:
    sc = {}
    for rf, x in data.items():
        m = metrics(x[:n], FS, nfft)
        if m:
            sc[rf] = m
    if len(sc) < 30:
        continue
    ts, tsh, ta, tp, fp, mm = best_recipe(sc)
    print(f"{1000*n/FS:7.2f} {nfft:>6}   {ts:6.1f}/{tsh:5.1f}/{ta:5.1f}  "
          f"{tp:>3} {fp:>3} {mm:8.2f}")

print()
print("=== (b) TWO-PILOTS-PER-TUNE geometry stress: shrink analysis bands ===")
print("shipped recipe thresholds 30.0/26.25/2.4, dwell = 16384 samples (2.05 ms)")
print(f"{'variant':<44} {'TP':>3} {'FP':>3} {'minmarg':>8}")
for label, kw in [
    ("baseline: asym +/-3.0MHz oob 3.5MHz", dict(asym_mhz=3.0, oob_mhz=3.5)),
    ("asym +/-2.0MHz (pilot 3MHz off center)", dict(asym_mhz=2.0, oob_mhz=3.5)),
    ("asym +/-1.5MHz", dict(asym_mhz=1.5, oob_mhz=3.5)),
    ("asym +/-1.0MHz", dict(asym_mhz=1.0, oob_mhz=3.5)),
    ("asym +/-2.0 + oob ref 3.0MHz (squeezed)", dict(asym_mhz=2.0, oob_mhz=3.0)),
    ("asym +/-2.0 + oob ref 2.5MHz (squeezed)", dict(asym_mhz=2.0, oob_mhz=2.5)),
]:
    sc = {rf: metrics(x[:16384], FS, 16384, **kw) for rf, x in data.items()}
    sc = {k: v for k, v in sc.items() if v}
    tp, fp, mm = score(sc, 30.0, 26.25, 2.4)
    print(f"{label:<44} {tp:>3} {fp:>3} {mm:8.2f}")
    ts, tsh, ta, tp2, fp2, mm2 = best_recipe(sc)
    print(f"{'   re-optimized ->':<44} {tp2:>3} {fp2:>3} {mm2:8.2f}"
          f"   ({ts:.1f}/{tsh:.1f}/{ta:.1f})")
