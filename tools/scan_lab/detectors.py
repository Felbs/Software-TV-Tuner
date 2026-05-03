"""ATSC 1.0 carrier-detection algorithms (offline / replay).

Each detector takes a complex-IQ buffer captured at the channel center
plus the sample rate, and returns a `score` (higher = stronger match)
along with any algorithm-specific intermediate values useful for tuning.

Detectors implemented here:

  pilot_snr       — peak in pilot bin vs out-of-band noise. Cheap.
                    Triggers on any narrow tone (CW, NTSC video carrier,
                    even DAB pilots).  ~2-3 ms/channel.
  pilot_sharpness — peak / ±100 kHz neighborhood mean. Distinguishes a
                    CW pilot from a broadband peak. The single strongest
                    discriminator we have.  ~1-2 ms/channel.
  vsb_asymmetry   — power 0.3-5 MHz above pilot vs power 0.5-2 MHz below
                    pilot. ATSC's signature shape; ~0 dB on noise / OFDM,
                    ~3-15 dB on real ATSC.  Cheap (reuses the FFT).
  pn511_corr      — cross-correlate the pilot-shifted baseband against a
                    Hilbert-transformed reference of the canonical 519-symbol
                    field-sync prefix (4-symbol segment-sync + 511-symbol
                    PN). Strong score = "this is real ATSC". Uses a 30 ms
                    window of the capture (≈1.24 fields, guaranteeing one
                    field-sync hit). ~30-40 ms/channel.
  spectral_mask   — Pearson correlation of the in-band log-PSD with the
                    expected ATSC envelope (flat data sideband above pilot,
                    sharp roll-off below).  ~3 ms/channel.
  field_autocorr  — autocorrelation magnitude at the 24.18 ms field period.
                    *Unreliable*: any narrowband signal produces coherence
                    at any lag; adjacent-channel interference contaminates
                    real ATSC scores. Kept for diagnostics only — do not
                    use as a gate. ~10 ms/channel.

The `combine` helpers express how to turn raw scores into a binary
decision; the harness (harness.py) sweeps thresholds and scores against
ground_truth_dc.json.
"""

from __future__ import annotations

from typing import Sequence
import numpy as np

# ATSC 8-VSB pilot offset from channel center.
PILOT_OFFSET_HZ = -2.690e6
# ATSC field period (313 segments × 832 symbols / 10.762 Msym/s).
FIELD_PERIOD_SEC = 24.18e-3
# Symbol rate.
SYMBOL_RATE = 10.762237e6

# ── Canonical PN511 / PN63 sequences (gr-atscplus / gr-dtv) ──────
# These match atsc_pnXXX_impl.h byte-for-byte and are the actual sequences
# every ATSC 1.0 transmitter inserts at the start of every field.

PN511 = np.array([
    0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 1, 0,
    1, 0, 1, 0, 1, 0, 1, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 1, 0, 0, 0,
    1, 0, 0, 0, 1, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 1, 1, 1, 0, 1,
    0, 1, 1, 1, 1, 1, 0, 1, 0, 0, 1, 1, 0, 1, 0, 1, 0, 0, 1, 1, 1, 0,
    1, 1, 0, 0, 1, 1, 1, 0, 1, 0, 0, 1, 0, 0, 0, 1, 0, 1, 1, 0, 0, 0,
    1, 1, 1, 1, 0, 0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 1, 1, 1,
    1, 1, 0, 0, 1, 1, 1, 1, 0, 1, 0, 1, 0, 0, 0, 1, 0, 1, 0, 0, 1, 1,
    0, 0, 0, 0, 1, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 1,
    1, 1, 1, 1, 0, 0, 0, 0, 0, 1, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0,
    1, 1, 0, 0, 1, 1, 1, 1, 1, 1, 1, 0, 1, 1, 1, 0, 1, 0, 1, 0, 1, 0,
    0, 1, 0, 1, 1, 0, 0, 1, 1, 0, 0, 0, 1, 1, 0, 1, 1, 1, 0, 1, 1, 1,
    1, 0, 1, 1, 0, 1, 0, 0, 1, 0, 1, 0, 0, 1, 0, 0, 1, 1, 1, 0,
    0, 1, 1, 1, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0, 1, 1, 0, 1,
    0, 0, 1, 1, 1, 1, 1, 0, 1, 1, 0, 0, 0, 1, 0, 1, 0, 1, 1, 0, 1, 1,
    1, 1, 0, 0, 1, 1, 0, 1, 1, 0, 1, 0, 1, 1, 1, 0, 1, 1, 0, 1,
    1, 0, 0, 1, 0, 1, 1, 0, 1, 1, 0, 1, 1, 1, 0, 0, 1, 0, 0, 1, 0, 0,
    1, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 1, 0, 1, 1, 1, 1, 0, 1, 0,
    0, 0, 1, 1, 0, 1, 0, 1, 1, 0, 0, 0, 0, 1, 0, 0, 1, 1, 0, 1,
    1, 1, 1, 1, 0, 0, 0, 1, 0, 0, 1, 0, 1, 0, 1, 1, 1, 1, 0, 0, 0, 1,
    1, 0, 0, 1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 1,
    1, 1, 1, 0, 1, 1, 1, 1, 1, 1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 0,
    1, 1, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, 0, 0, 1, 1, 1, 0, 1, 1, 1,
    0, 0, 0, 0, 1, 0, 1, 1, 0, 1, 0, 0, 0, 0, 0, 1, 1, 0, 1, 1, 0, 0,
    0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 0,
], dtype=np.int8)
assert PN511.size == 511, "PN511 length wrong — copy-paste error"

PN63 = np.array([
    1, 1, 1, 0, 0, 1, 0, 0, 1, 0, 1, 1, 0, 1, 1, 1,
    0, 1, 1, 0, 0, 1, 1, 0, 1, 0, 1, 0, 1, 1, 1, 1,
    1, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 1, 0, 0,
    0, 1, 0, 1, 0, 0, 1, 1, 1, 1, 0, 1, 0, 0, 0,
], dtype=np.int8)
assert PN63.size == 63


def _fft_psd(samples: np.ndarray, n_fft: int = 16384):
    """Window + FFT-shift, return magnitude-squared spectrum."""
    n = min(samples.size, n_fft)
    if n < 1024:
        return None, None
    win = np.hanning(n).astype(np.float32)
    spec = np.fft.fftshift(np.fft.fft(samples[:n] * win, n_fft))
    return np.abs(spec) ** 2, n_fft


# ── 1. Pilot SNR (the existing crude method) ─────────────────────
def pilot_snr(samples: np.ndarray, sample_rate: int) -> dict:
    psd, n_fft = _fft_psd(samples)
    if psd is None:
        return {"score": float("-inf")}
    bin_hz = sample_rate / n_fft
    pilot_bin = n_fft // 2 + int(round(PILOT_OFFSET_HZ / bin_hz))
    win_bins = max(1, int(round(2e3 / bin_hz)))
    lo = max(0, pilot_bin - win_bins)
    hi = min(n_fft, pilot_bin + win_bins + 1)
    pilot_peak = float(np.max(psd[lo:hi])) if hi > lo else 0.0
    margin_bins = int(round(3.5e6 / bin_hz))
    oob_lo = psd[:max(0, n_fft // 2 - margin_bins)]
    oob_hi = psd[min(n_fft, n_fft // 2 + margin_bins):]
    noise_ref = np.concatenate([oob_lo, oob_hi]) if (oob_lo.size + oob_hi.size) else psd
    noise = float(np.median(noise_ref)) if noise_ref.size else 1e-20
    snr_db = 10.0 * np.log10(pilot_peak / max(noise, 1e-20) + 1e-20)
    return {"score": snr_db}


# ── 2. Pilot sharpness ───────────────────────────────────────────
def pilot_sharpness(samples: np.ndarray, sample_rate: int) -> dict:
    psd, n_fft = _fft_psd(samples)
    if psd is None:
        return {"score": float("-inf")}
    bin_hz = sample_rate / n_fft
    pilot_bin = n_fft // 2 + int(round(PILOT_OFFSET_HZ / bin_hz))
    pilot_win = max(1, int(round(2e3 / bin_hz)))
    plo = max(0, pilot_bin - pilot_win)
    phi = min(n_fft, pilot_bin + pilot_win + 1)
    pilot_peak = float(np.max(psd[plo:phi])) if phi > plo else 0.0
    nbhd_win = int(round(100e3 / bin_hz))
    nlo = max(0, pilot_bin - nbhd_win)
    nhi = min(n_fft, pilot_bin + nbhd_win + 1)
    nbhd = psd[nlo:nhi].copy()
    inner_lo = max(0, plo - nlo)
    inner_hi = max(inner_lo, phi - nlo)
    nbhd[inner_lo:inner_hi] = 0
    nz = nbhd[nbhd > 0]
    nbhd_mean = float(np.mean(nz)) if nz.size else 1e-20
    score_db = 10.0 * np.log10(pilot_peak / max(nbhd_mean, 1e-20) + 1e-20)
    return {"score": score_db}


# ── 3. VSB asymmetry ─────────────────────────────────────────────
def vsb_asymmetry(samples: np.ndarray, sample_rate: int) -> dict:
    """ATSC's data sideband sits 0..+5.38 MHz above the pilot; the bottom
    side has only a ~0.3 MHz vestigial roll-off and then noise. So the
    upper band has substantially more in-band power than the lower.

    We compare power 3 MHz above pilot vs power 3 MHz below — the
    classic asymmetry score. The 3 MHz lower window mostly catches
    out-of-channel noise (capped at the capture's lower edge), and the
    3 MHz upper window catches mid-band data energy. Real ATSC: ≥3 dB.
    Noise / symmetric OFDM: ≈0 dB.
    """
    psd, n_fft = _fft_psd(samples)
    if psd is None:
        return {"score": float("-inf")}
    bin_hz = sample_rate / n_fft
    pilot_bin = n_fft // 2 + int(round(PILOT_OFFSET_HZ / bin_hz))
    bins_3m = max(1, int(round(3.0e6 / bin_hz)))
    above = psd[pilot_bin:min(n_fft, pilot_bin + bins_3m)]
    below = psd[max(0, pilot_bin - bins_3m):pilot_bin]
    above_pow = float(np.mean(above)) if above.size else 0.0
    below_pow = float(np.mean(below)) if below.size else 1e-20
    score_db = 10.0 * np.log10(above_pow / max(below_pow, 1e-20) + 1e-20)
    return {"score": score_db}


# ── 4. PN511 cross-correlation (FIXED) ───────────────────────────
def _build_field_sync_reference(sample_rate: int, field_num: int = 0,
                                  symbols: int = 519) -> np.ndarray:
    """Build a complex-baseband reference for the leading `symbols` symbols
    of an ATSC field-sync segment, mixed so the pilot sits at DC.

    The ATSC field-sync segment starts with:
       4 segment-sync symbols [+5,-5,-5,+5]
     + 511 PN511 (each ±5)
     + 63  PN63
     + 63  PN63 ⊕ field_num   (alternates between fields)
     + 63  PN63
     + 24  VSB-mode bits, 92 reserved, 12 precoder

    The first 4 + 511 = 515 symbols are deterministic and identical
    across every transmitter, so they're the gold-standard fingerprint.
    We use the leading 519 symbols by default (4 segsync + 511 PN511 +
    a 4-symbol guard that's also deterministic).

    The reference is built as an analytic (Hilbert-transformed) signal
    so that all energy is in the upper sideband — matching how 8-VSB
    places its data sideband above the pilot.
    """
    syms = np.zeros(832, dtype=np.float32)
    i = 0
    # Segment-sync prefix [+5,-5,-5,+5] = bin_map([1,0,0,1]).
    for b in (1, 0, 0, 1):
        syms[i] = +5.0 if b else -5.0
        i += 1
    for b in PN511:
        syms[i] = +5.0 if int(b) else -5.0
        i += 1
    for b in PN63:
        syms[i] = +5.0 if int(b) else -5.0
        i += 1
    for b in PN63:
        syms[i] = +5.0 if (int(b) ^ field_num) else -5.0
        i += 1
    for b in PN63:
        syms[i] = +5.0 if int(b) else -5.0
        i += 1
    syms = syms[:symbols]
    # Linear-interp upsample to sample_rate (cheap; the SDR's anti-alias
    # filter has already shaped the captured signal so a precise RRC isn't
    # required for matched-filter detection).
    sps = sample_rate / SYMBOL_RATE
    n_out = int(np.ceil(syms.size * sps))
    src_idx = np.arange(n_out) / sps
    i0 = np.floor(src_idx).astype(int)
    i1 = np.minimum(i0 + 1, syms.size - 1)
    frac = src_idx - i0
    real = syms[i0] * (1 - frac) + syms[i1] * frac
    real = real.astype(np.float32)
    # Hilbert: zero negative-frequency half so all energy is in upper SB.
    spec = np.fft.fft(real)
    n = real.size
    spec[n // 2 + 1:] = 0
    spec[1:n // 2] *= 2
    return np.fft.ifft(spec).astype(np.complex64)


_pn511_ref_cache: dict[tuple[int, int], np.ndarray] = {}


def pn511_corr(samples: np.ndarray, sample_rate: int,
                scan_ms: float = 30.0) -> dict:
    """Cross-correlate the pilot-shifted capture against the canonical
    field-sync reference. Uses only the leading `scan_ms` of the capture
    (default 30 ms ≈ 1.24 ATSC fields, guaranteeing at least one field-
    sync hit) to keep the FFT cheap.

    Real ATSC: ≥22 dB peak/floor. Random / non-ATSC: usually <22 dB.
    Empirically this gives F1=1.0 alone on the DC fixture set, with a
    threshold around 22-22.5 dB. Cost ≈ 30-40 ms/channel.
    """
    n_total = samples.size
    n_scan = min(n_total, int(scan_ms * 1e-3 * sample_rate))
    if n_scan < 50_000:
        return {"score": float("-inf"), "peak_idx": -1}
    sub = samples[:n_scan]
    t = np.arange(n_scan, dtype=np.float64) / sample_rate
    shifted = (sub * np.exp(-2j * np.pi * PILOT_OFFSET_HZ * t)).astype(np.complex64)
    cache_key = (sample_rate, 519)
    if cache_key not in _pn511_ref_cache:
        _pn511_ref_cache[cache_key] = _build_field_sync_reference(
            sample_rate, field_num=0, symbols=519)
    ref = _pn511_ref_cache[cache_key]
    h = ref[::-1].conj()
    n_out = n_scan + h.size - 1
    n_fft = 1 << int(np.ceil(np.log2(n_out)))
    A = np.fft.fft(shifted, n_fft)
    H = np.fft.fft(h, n_fft)
    corr = np.fft.ifft(A * H)[:n_out]
    mag = np.abs(corr).astype(np.float32)
    peak = float(np.max(mag))
    peak_idx = int(np.argmax(mag))
    guard = h.size
    mask = np.ones_like(mag, dtype=bool)
    mask[max(0, peak_idx - guard):min(mag.size, peak_idx + guard)] = False
    noise = float(np.median(mag[mask])) if mask.any() else 1e-20
    score_db = 20.0 * np.log10(peak / max(noise, 1e-20) + 1e-20)
    return {"score": score_db, "peak_idx": peak_idx}


# ── 5. Spectral mask compliance ──────────────────────────────────
def spectral_mask(samples: np.ndarray, sample_rate: int) -> dict:
    """Correlate the in-band PSD against the expected ATSC envelope:
    a flat top from pilot to pilot+5.38 MHz with sharp roll-off below
    pilot. Returns a similarity score in [-1, +1]."""
    psd, n_fft = _fft_psd(samples)
    if psd is None:
        return {"score": float("-inf")}
    bin_hz = sample_rate / n_fft
    pilot_bin = n_fft // 2 + int(round(PILOT_OFFSET_HZ / bin_hz))
    bins_3m = max(1, int(round(3.0e6 / bin_hz)))
    region = slice(max(0, pilot_bin - bins_3m // 2),
                    min(n_fft, pilot_bin + 2 * bins_3m))
    actual = psd[region]
    if actual.size < 32:
        return {"score": float("-inf")}
    rel_bins = np.arange(actual.size) - (pilot_bin - region.start)
    rel_hz = rel_bins * bin_hz
    expected = np.zeros_like(actual, dtype=np.float32)
    expected[(rel_hz >= 0) & (rel_hz <= 5.38e6)] = 1.0
    vsb_mask = (rel_hz < 0) & (rel_hz > -0.31e6)
    expected[vsb_mask] = 0.5 * (rel_hz[vsb_mask] + 0.31e6) / 0.31e6
    actual_db = 10.0 * np.log10(actual + 1e-20)
    actual_db -= np.mean(actual_db)
    expected_z = expected - np.mean(expected)
    denom = (np.std(actual_db) * np.std(expected_z))
    if denom <= 0:
        return {"score": float("-inf")}
    corr = float(np.mean(actual_db * expected_z) / denom)
    return {"score": corr}


# ── 6. Field-period autocorrelation ──────────────────────────────
def field_autocorr(samples: np.ndarray, sample_rate: int) -> dict:
    """Compute correlation magnitude at exactly the ATSC field period
    (24.18 ms).

    UNRELIABLE: any narrowband interference shows coherence at every
    lag, and adjacent-channel leakage degrades real ATSC scores. Real
    ATSC at this site: -16 to -28 dB. Empties: -3 to -55 dB (some
    *higher* than real ATSC). Kept for diagnostic observation only;
    do not gate on it.
    """
    lag = int(round(FIELD_PERIOD_SEC * sample_rate))
    if samples.size < 2 * lag + 1024:
        return {"score": float("-inf")}
    a = samples[:samples.size - lag]
    b = samples[lag:]
    corr = np.mean(a * np.conj(b))
    energy_a = np.mean(np.abs(a) ** 2)
    energy_b = np.mean(np.abs(b) ** 2)
    denom = np.sqrt(energy_a * energy_b)
    if denom <= 0:
        return {"score": float("-inf")}
    coherence = float(np.abs(corr) / denom)
    score_db = 20.0 * np.log10(coherence + 1e-20)
    return {"score": score_db, "coherence": coherence}


DETECTORS = {
    "pilot_snr": pilot_snr,
    "pilot_sharpness": pilot_sharpness,
    "vsb_asymmetry": vsb_asymmetry,
    "pn511_corr": pn511_corr,
    "spectral_mask": spectral_mask,
    "field_autocorr": field_autocorr,
}


def run_all(samples: np.ndarray, sample_rate: int) -> dict:
    """Convenience: run every detector, return {name: result_dict}."""
    return {name: fn(samples, sample_rate) for name, fn in DETECTORS.items()}
