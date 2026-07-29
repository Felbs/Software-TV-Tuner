"""Offline: how short can the per-channel dwell be and still detect the pilot?

Reads the 35 scan_lab fixtures (200 ms @ 8 MS/s, cf32) READ-ONLY and re-runs
the shipped sdr_sweep._analyze recipe on truncated prefixes.
No SDR, no hardware, no writes to the repo.
"""
import json
import sys
from pathlib import Path

import numpy as np

FIX = Path(r"Z:\src\magic-tv-decoder\tools\scan_lab\fixtures")
FS = 8_000_000
# shipped winning_recipe.json thresholds
T_SNR, T_SHARP, T_ASYM = 30.0, 26.25, 2.4


def analyze(samples, sample_rate, n_fft):
    if samples.size < n_fft:
        return None
    power = float(np.mean(np.abs(samples) ** 2))
    n_segments = max(1, samples.size // n_fft)
    win = np.hanning(n_fft).astype(np.float32)
    psd = np.zeros(n_fft, dtype=np.float64)
    used = 0
    for k in range(n_segments):
        seg = samples[k * n_fft:(k + 1) * n_fft]
        if seg.size < n_fft:
            break
        spec = np.fft.fftshift(np.fft.fft(seg * win, n_fft))
        psd += np.abs(spec) ** 2
        used += 1
    psd /= max(1, used)
    bin_hz = sample_rate / n_fft

    pilot_center_bin = n_fft // 2 + int(round(-2.690e6 / bin_hz))
    pilot_win = max(1, int(round(2e3 / bin_hz)))
    pilot_lo = max(0, pilot_center_bin - pilot_win)
    pilot_hi = min(n_fft, pilot_center_bin + pilot_win + 1)
    pilot_peak = float(np.max(psd[pilot_lo:pilot_hi]))

    margin_bins = int(round(3.5e6 / bin_hz))
    oob = np.concatenate([psd[:max(0, n_fft // 2 - margin_bins)],
                          psd[min(n_fft, n_fft // 2 + margin_bins):]])
    noise_floor = float(np.median(oob)) if oob.size else float(np.median(psd))
    noise_floor = max(noise_floor, 1e-20)

    nbhd_win = int(round(100e3 / bin_hz))
    nlo, nhi = max(0, pilot_center_bin - nbhd_win), min(n_fft, pilot_center_bin + nbhd_win + 1)
    nbhd = psd[nlo:nhi].copy()
    ilo = max(0, pilot_lo - nlo)
    ihi = max(ilo, pilot_hi - nlo)
    nbhd[ilo:ihi] = 0
    nz = nbhd[nbhd > 0]
    nbhd_mean = float(np.mean(nz)) if nz.size else noise_floor
    sharp = 10 * np.log10(pilot_peak / nbhd_mean + 1e-20)

    b3 = max(1, int(round(3.0e6 / bin_hz)))
    above = float(np.mean(psd[pilot_center_bin:min(n_fft, pilot_center_bin + b3)]))
    below = float(np.mean(psd[max(0, pilot_center_bin - b3):pilot_center_bin]))
    asym = 10 * np.log10(above / max(below, 1e-20) + 1e-20)
    snr = 10 * np.log10(pilot_peak / noise_floor + 1e-20)
    return dict(rms=10 * np.log10(power + 1e-20), snr=snr, sharp=sharp,
                asym=asym, nseg=used)


def gate(m):
    return m is not None and m["snr"] >= T_SNR and m["sharp"] >= T_SHARP and m["asym"] >= T_ASYM


man = json.load(open(FIX / "manifest.json"))
caps = man["captures"]

# dwell ladder in samples -> (n_fft used)
LADDER = [
    (1024, 1024), (2048, 2048), (4096, 4096), (8192, 8192),
    (16384, 16384), (32768, 16384), (65536, 16384), (131072, 16384),
    (262144, 16384), (524288, 16384), (1048576, 16384), (1600000, 16384),
]

rows = {}
for rf, c in sorted(caps.items(), key=lambda kv: int(kv[0])):
    p = FIX / c["file"]
    x = np.fromfile(p, dtype=np.complex64)
    rows[int(rf)] = {}
    for n, nfft in LADDER:
        if x.size < n:
            continue
        rows[int(rf)][n] = analyze(x[:n], FS, nfft)

# ground truth = full-length gate
truth = {rf: gate(r[1600000]) for rf, r in rows.items() if 1600000 in r}
print("FULL-DWELL (200 ms) DETECTIONS:",
      sorted([rf for rf, v in truth.items() if v]))
print()
print("=== per-channel scores at full 200 ms (nseg=97) ===")
print(f"{'RF':>3} {'snr':>7} {'sharp':>7} {'asym':>7}  gate")
for rf in sorted(rows):
    m = rows[rf].get(1600000)
    if m:
        print(f"{rf:>3} {m['snr']:7.1f} {m['sharp']:7.1f} {m['asym']:7.1f}  "
              f"{'HIT' if gate(m) else '.'}")

print()
print("=== detection vs dwell length ===")
print(f"{'samples':>9} {'ms':>7} {'nfft':>6} {'TP':>3} {'FN':>3} {'FP':>3} "
      f"{'minmargin_dB':>13}  detected")
for n, nfft in LADDER:
    tp = fn = fp = 0
    margins = []
    det = []
    for rf in sorted(rows):
        m = rows[rf].get(n)
        if m is None:
            continue
        g = gate(m)
        if g:
            det.append(rf)
        if truth.get(rf):
            if g:
                tp += 1
                margins.append(min(m["snr"] - T_SNR, m["sharp"] - T_SHARP,
                                   m["asym"] - T_ASYM))
            else:
                fn += 1
        elif g:
            fp += 1
    mm = f"{min(margins):13.2f}" if margins else " " * 13
    print(f"{n:>9} {1000*n/FS:7.2f} {nfft:>6} {tp:>3} {fn:>3} {fp:>3} {mm}  {det}")

print()
print("=== per-channel score decay for the true channels ===")
tchans = [rf for rf, v in truth.items() if v]
for rf in tchans:
    line = f"RF{rf:>2}: "
    for n, _ in LADDER:
        m = rows[rf].get(n)
        if m:
            line += f"{1000*n/FS:.1f}ms[{m['snr']:.0f}/{m['sharp']:.0f}/{m['asym']:.0f}] "
    print(line)

print()
print("=== strongest EMPTY channels (false-positive pressure) ===")
for rf in sorted(rows):
    if truth.get(rf):
        continue
    m = rows[rf].get(1600000)
    if m and m["sharp"] > 10:
        line = f"RF{rf:>2} (empty): "
        for n, _ in LADDER:
            mm = rows[rf].get(n)
            if mm:
                line += f"{1000*n/FS:.1f}ms[{mm['snr']:.0f}/{mm['sharp']:.0f}/{mm['asym']:.0f}] "
        print(line)
