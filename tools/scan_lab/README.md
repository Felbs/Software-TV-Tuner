# Scanner test bench

A replay-based testbed for tuning the SDR channel scanner. Captures
wideband I/Q from your live antenna once, then iterates detection
algorithms against the captures (no SDR, no retunes) until the recipe
matches HDHomeRun-class accuracy.

## Files

| File | Purpose |
|---|---|
| `capture_fixtures.py` | Captures 200 ms of complex IQ at every RF channel in the configured region. Run once. |
| `ground_truth_dc.json` | DC market truth — what HDHomeRun finds (callsign, expected ATSC 1.0 vs 3.0 vs empty). Edit for other markets. |
| `detectors.py` | Six candidate detection algorithms, each scoring an IQ buffer. Pure Python, stdlib + numpy. |
| `harness.py` | Replays every fixture through every detector + AND-combo, sweeps thresholds, ranks by F1 score. |
| `fixtures/` | Captured `.cf32` files (gitignored). |

## Detectors implemented

1. **`pilot_snr`** — peak in pilot bin vs out-of-band noise. Cheap. Catches any narrow CW.
2. **`pilot_sharpness`** — peak / ±100 kHz neighborhood mean. Distinguishes CW from broadband peak.
3. **`vsb_asymmetry`** — power above pilot vs below pilot (3 MHz each band). ATSC's signature shape.
4. **`pn511_corr`** — match-filter against the 511-symbol PN sequence ATSC broadcasts. The "this is real ATSC" gold standard hardware demods use.
5. **`spectral_mask`** — Pearson correlation of captured PSD against the published ATSC envelope (flat top + sharp lower roll-off).
6. **`field_autocorr`** — autocorrelation at the 24.18 ms field period. Pulls a coherent peak out of repeating field-sync structure.

## How to use

```powershell
# 1. Capture fixtures from your live antenna (one-time, ~7 minutes, ~430 MB)
& "$env:USERPROFILE\radioconda\python.exe" tools\scan_lab\capture_fixtures.py

# 2. Run the harness — instant, CPU only, no SDR
python tools\scan_lab\harness.py
```

The harness output:

```
Per-detector best (single-feature gate)
detector            thr    tp  fn  fp  mgnl  prec   rec    f1
pilot_snr         40.00     7   1   0    0  1.00  0.88  0.93
...

AND-combos (up to size 3, sorted by F1)
pilot_snr+pn511_corr+vsb_asymmetry        7   1   0    0  0.93
...
```

Use the top-ranked combo + thresholds to update `sdr_sweep.py` and
`tv_tuner.py`'s `run_scan` defaults.

## Adding a new detector

1. Add the function to `detectors.py` (signature `(samples, sample_rate) → {"score": float}`).
2. Register it in the `DETECTORS` dict.
3. Add a threshold-candidate list to `harness.py`'s `thresholds` dict.
4. Re-run `harness.py`.

## Adding a new market's ground truth

Copy `ground_truth_dc.json` to `ground_truth_<market>.json`, fill in
the channel list from a hardware tuner reference, and point `harness.py`
at it.
