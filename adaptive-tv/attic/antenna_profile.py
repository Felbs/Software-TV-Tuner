"""Auto-characterize an antenna+SDR+port setup for ATSC.

Sweeps a wide band on the SDR, finds peaks, classifies them as:
  - target ATSC channel (the one we want to decode)
  - in-band interferer (within our 6 MHz, would survive bandpass — needs notch)
  - out-of-band interferer (outside 6 MHz but inside SDR bandwidth — bandpass kills it)
  - far interferer (already outside sample rate — already gone)

Output: JSON profile per (antenna, port, target RF). Profile contents:
  {
    "target_rf": 7,
    "target_center_mhz": 177.0,
    "target_bw_mhz": 6.0,
    "ifgr_optimal": 30,
    "rfgain_sel_optimal": 4,
    "interferers": [
      {"center_mhz": 95.5, "power_dbfs": -22, "kind": "out-of-band-fm"},
      {"center_mhz": 178.2, "power_dbfs": -34, "kind": "in-band-noise"},
      ...
    ],
    "hardware_notches": ["rfnotch_ctrl=true"],   # SDRplay hw notches to use
    "fir_taps_path": "fir_RF7_AntC_177MHz.npy",  # digital BP+notch filter
    "snr_db_estimated": 16.2
  }

The FIR taps file is a numpy float32 array of complex filter coefficients
suitable for GR's fir_filter_ccc block.

Usage:
    python antenna_profile.py --rf 7 --antenna "Antenna C"
    python antenna_profile.py --rf 7 --antenna "Antenna C" --ifgr 30 --rfgain 4
    python antenna_profile.py --list                  # show saved profiles
    python antenna_profile.py --show profile.json     # pretty-print profile
"""
import argparse
import json
import os
import sys
import time

# Make Windows console UTF-8 safe (prevents cp1252 crashes on ≈ / → / etc.)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
from scipy import signal as scisig

import SoapySDR
SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX

# ATSC channel center frequencies (MHz). From ATSC tuning table.
ATSC_RF_CENTERS = {2: 57.0, 3: 63.0, 4: 69.0, 5: 79.0, 6: 85.0,
                   7: 177.0, 8: 183.0, 9: 189.0, 10: 195.0, 11: 201.0, 12: 207.0, 13: 213.0,
                   **{ch: 473.0 + (ch - 14) * 6.0 for ch in range(14, 37)}}

ATSC_BW_MHZ = 6.0

# Known interferer bands we should ALWAYS notch
KNOWN_INTERFERER_BANDS = [
    ("FM broadcast", 88.0e6, 108.0e6),
    ("aviation",     108.0e6, 137.0e6),
    ("amateur 2m",   144.0e6, 148.0e6),
    ("DAB",          174.0e6, 240.0e6),     # overlaps US VHF-hi TV but DAB not used in US
    ("GMRS/FRS",     462.0e6, 467.0e6),
    ("cellular",     698.0e6, 798.0e6),
]


@dataclass
class Interferer:
    center_mhz:  float
    power_dbfs:  float
    delta_db:    float       # above noise floor
    kind:        str         # "out-of-band-fm", "in-band-noise", etc.


@dataclass
class AntennaProfile:
    target_rf:           int
    target_center_mhz:   float
    target_bw_mhz:       float
    target_power_dbfs:   float
    noise_floor_dbfs:    float
    snr_db:              float
    ifgr:                int
    rfgain_sel:          int
    antenna:             str
    sample_rate_hz:      int
    interferers:         list
    hardware_notches:    list
    fir_taps_path:       str | None
    timestamp:           str


def classify_interferer(freq_mhz: float, target_center: float, target_bw: float,
                          sample_rate_mhz: float) -> str:
    """Bucket an interferer relative to our target ATSC channel."""
    low  = target_center - target_bw / 2
    high = target_center + target_bw / 2
    if low <= freq_mhz <= high:
        return "in-band"
    half_sr = sample_rate_mhz / 2
    if abs(freq_mhz - target_center) > half_sr:
        return "out-of-sr"   # already filtered out by SDR sample rate
    # Tag the known bands
    for name, lo, hi in KNOWN_INTERFERER_BANDS:
        if lo / 1e6 <= freq_mhz <= hi / 1e6:
            return f"out-of-band-{name.split()[0].lower()}"
    return "out-of-band"


def find_peaks(spec_db: np.ndarray, freqs_mhz: np.ndarray, threshold_db: float,
                 window_bins: int) -> list[Interferer]:
    """Find local maxima above threshold dB over floor."""
    floor = float(np.median(spec_db[spec_db > -150]))
    peaks: list[Interferer] = []
    for i in range(window_bins, len(spec_db) - window_bins):
        v = spec_db[i]
        delta = v - floor
        if delta < threshold_db:
            continue
        if v == np.max(spec_db[i - window_bins:i + window_bins + 1]):
            peaks.append(Interferer(
                center_mhz=float(freqs_mhz[i]),
                power_dbfs=float(v),
                delta_db=float(delta),
                kind="?",
            ))
    return peaks


def sweep_band(sdr, center_mhz: float, sample_rate: int, dwell_sec: float = 2.0,
                fft_size: int = 16384) -> tuple[np.ndarray, np.ndarray, float]:
    """Capture wideband IQ at center freq, return (spec_db, freqs_mhz, noise_floor)."""
    sdr.setSampleRate(SOAPY_SDR_RX, 0, sample_rate)
    sdr.setFrequency(SOAPY_SDR_RX, 0, center_mhz * 1e6)
    rate = sdr.getSampleRate(SOAPY_SDR_RX, 0)
    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    sdr.activateStream(stream)
    warmup = np.empty(65536, dtype=np.complex64)
    try:
        sdr.readStream(stream, [warmup], 65536, timeoutUs=int(0.5e6))
    except Exception:
        pass
    buf = np.empty(fft_size, dtype=np.complex64)
    win = np.hanning(fft_size).astype(np.float32)
    n_avgs = max(8, int(dwell_sec * rate / fft_size))
    acc = np.zeros(fft_size, dtype=np.float64)
    n_real = 0
    for _ in range(n_avgs):
        sr = sdr.readStream(stream, [buf], fft_size, timeoutUs=int(0.5e6))
        if sr.ret < fft_size:
            continue
        x = buf[:fft_size] * win
        spec = np.abs(np.fft.fftshift(np.fft.fft(x))) ** 2
        acc += spec
        n_real += 1
    sdr.deactivateStream(stream)
    sdr.closeStream(stream)
    if n_real == 0:
        return None, None, None
    spec_db = 10 * np.log10(acc / n_real + 1e-20)
    bin_mhz = rate / fft_size / 1e6
    freqs_mhz = center_mhz + (np.arange(fft_size) - fft_size / 2) * bin_mhz
    # Mask DC ±50 kHz
    dc_lo = fft_size // 2 - int(0.05 / bin_mhz)
    dc_hi = fft_size // 2 + int(0.05 / bin_mhz)
    spec_db[dc_lo:dc_hi] = -200
    floor = float(np.median(spec_db[spec_db > -150]))
    return spec_db, freqs_mhz, floor


def design_bandpass_notch(target_center_mhz: float, target_bw_mhz: float,
                          sample_rate: float, interferers: list[Interferer],
                          baseband_taps: int = 65) -> np.ndarray:
    """Design FIR taps for a complex bandpass at baseband (after freq translation
    to target_center) with notches at in-band interferers.

    Returns float32 complex taps for gr.filter.fir_filter_ccc.
    """
    nyq = sample_rate / 2
    # Bandpass at DC (since freq-xlating block translates first)
    bp_low  = -target_bw_mhz / 2 * 1e6
    bp_high = +target_bw_mhz / 2 * 1e6
    # Build a bandpass via complex bandshift of a lowpass
    lp_cutoff = (target_bw_mhz / 2) * 1e6
    transition = 200_000  # 200 kHz transition band (sharp)
    if lp_cutoff + transition >= nyq:
        transition = max(50_000, nyq - lp_cutoff - 1000)
    try:
        lp = scisig.remez(
            baseband_taps,
            [0, lp_cutoff, lp_cutoff + transition, nyq],
            [1, 0], fs=sample_rate,
        )
    except Exception:
        # Fallback to firwin if remez fails
        lp = scisig.firwin(baseband_taps, lp_cutoff, fs=sample_rate, pass_zero=True)

    # Convert real lowpass to complex (already real-only here, good for ccc filter)
    taps = lp.astype(np.float32)

    # In-band notches: cascade simple IIR notches via FIR convolution. Sharp but
    # the FIR truncates them — good enough to dent strong interferers within
    # the channel.
    in_band_interferers = [iv for iv in interferers
                            if abs(iv.center_mhz - target_center_mhz) < target_bw_mhz / 2]
    for itf in in_band_interferers:
        offset_hz = (itf.center_mhz - target_center_mhz) * 1e6
        # 3rd-order notch via cascading bandstop
        w0 = abs(offset_hz) / nyq
        Q = 30
        try:
            b_notch, a_notch = scisig.iirnotch(w0, Q)
            # Convolve our FIR with notch numerator (approximation; ignoring poles
            # makes it stable but the notch is less sharp). For a real impl we'd
            # use the IIR notch as a separate stage.
            taps = np.convolve(taps, b_notch).astype(np.float32)
        except Exception:
            pass

    return taps.astype(np.complex64)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, help="ATSC RF channel number (2-36)")
    ap.add_argument("--antenna", default="Antenna C", help="SDR antenna port")
    ap.add_argument("--ifgr", type=int, default=30)
    ap.add_argument("--rfgain", type=int, default=4)
    ap.add_argument("--sample-rate", type=int, default=8_000_000,
                    help="sweep sample rate Hz (default 8 MS/s)")
    ap.add_argument("--dwell", type=float, default=2.0)
    ap.add_argument("--threshold", type=float, default=10.0,
                    help="dB above noise floor for peak (default 10)")
    ap.add_argument("--out-dir", default="profiles", help="profile output dir")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--show", help="show a saved profile JSON file")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    out_dir = here / args.out_dir
    out_dir.mkdir(exist_ok=True)

    if args.list:
        for p in sorted(out_dir.glob("*.json")):
            print(f"  {p.name}")
        return
    if args.show:
        print(json.dumps(json.loads(Path(args.show).read_text()), indent=2))
        return
    if args.rf is None:
        ap.error("--rf is required (unless using --list/--show)")

    target_center = ATSC_RF_CENTERS.get(args.rf)
    if target_center is None:
        print(f"unknown RF channel {args.rf}", file=sys.stderr)
        sys.exit(2)
    print(f"[profile] target: RF{args.rf} @ {target_center} MHz, BW {ATSC_BW_MHZ} MHz")
    print(f"[profile] antenna: {args.antenna}  IFGR={args.ifgr}  rfgain_sel={args.rfgain}")
    print(f"[profile] sweep rate: {args.sample_rate/1e6} MS/s, dwell {args.dwell}s")

    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setAntenna(SOAPY_SDR_RX, 0, args.antenna)
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception:
        pass
    sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", float(args.ifgr))
    try:
        sdr.writeSetting("rfgain_sel", str(args.rfgain))
    except Exception:
        pass

    # Sweep 1: full SDR bandwidth at target center
    print("[profile] sweeping at target center...")
    spec, freqs, floor = sweep_band(sdr, target_center, args.sample_rate, args.dwell)
    if spec is None:
        print("sweep failed (no samples)", file=sys.stderr)
        sys.exit(1)
    print(f"[profile] noise floor: {floor:.1f} dBFS")

    bin_mhz = args.sample_rate / 16384 / 1e6
    win_bins = max(3, int(0.1 / bin_mhz))
    peaks = find_peaks(spec, freqs, args.threshold, win_bins)
    print(f"[profile] {len(peaks)} peaks > +{args.threshold} dB over floor")

    # Classify interferers
    for p in peaks:
        p.kind = classify_interferer(p.center_mhz, target_center, ATSC_BW_MHZ,
                                       args.sample_rate / 1e6)
    target_band_peaks = [p for p in peaks if abs(p.center_mhz - target_center) < ATSC_BW_MHZ / 2]
    target_power = max((p.power_dbfs for p in target_band_peaks), default=floor)
    snr = target_power - floor
    print(f"[profile] target channel power: {target_power:.1f} dBFS  SNR≈{snr:.1f} dB")

    # Top 10 interferers
    print("[profile] top interferers:")
    for p in sorted(peaks, key=lambda x: -x.delta_db)[:10]:
        marker = "TARGET" if "in-band" in p.kind else "interferer"
        print(f"   {p.center_mhz:>8.2f} MHz  {p.power_dbfs:>+6.1f} dBFS  +{p.delta_db:.1f} dB  {p.kind:<25} {marker}")

    # Recommend hardware notches
    hw_notches = []
    fm_peaks = [p for p in peaks if 88 <= p.center_mhz <= 108 and p.delta_db > 15]
    if fm_peaks:
        hw_notches.append("rfnotch_ctrl=true")
        print(f"[profile] {len(fm_peaks)} strong FM peaks → recommend STVT_RFNOTCH=1")
    # DAB notch overlaps US VHF-hi (174-216), so don't recommend if our target IS VHF-hi
    if 174.0 <= target_center <= 216.0:
        print("[profile] target is VHF-hi (US TV) — do NOT enable DAB notch (would kill target)")
    else:
        dab_peaks = [p for p in peaks if 174 <= p.center_mhz <= 240 and p.delta_db > 15]
        if dab_peaks:
            hw_notches.append("dabnotch_ctrl=true")
            print(f"[profile] {len(dab_peaks)} strong DAB-band peaks → recommend STVT_DABNOTCH=1")

    # Design digital pre-conditioning FIR
    fir_path = out_dir / f"fir_RF{args.rf}_{args.antenna.replace(' ', '')}_{int(target_center)}MHz.npy"
    print(f"[profile] designing FIR pre-conditioner...")
    taps = design_bandpass_notch(target_center, ATSC_BW_MHZ, args.sample_rate, peaks)
    np.save(fir_path, taps)
    print(f"[profile] FIR taps: {len(taps)} complex64 -> {fir_path}")

    # Build + save profile
    profile = AntennaProfile(
        target_rf=args.rf,
        target_center_mhz=target_center,
        target_bw_mhz=ATSC_BW_MHZ,
        target_power_dbfs=target_power,
        noise_floor_dbfs=floor,
        snr_db=snr,
        ifgr=args.ifgr,
        rfgain_sel=args.rfgain,
        antenna=args.antenna,
        sample_rate_hz=args.sample_rate,
        interferers=[asdict(p) for p in sorted(peaks, key=lambda x: -x.delta_db)[:20]],
        hardware_notches=hw_notches,
        fir_taps_path=str(fir_path.relative_to(here)),
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    out_path = out_dir / f"RF{args.rf}_{args.antenna.replace(' ', '')}.json"
    out_path.write_text(json.dumps(asdict(profile), indent=2))
    print(f"[profile] saved -> {out_path}")
    print()
    print(f"To launch tv_live with this profile's settings:")
    cmd = f"STVT_ANTENNA=\"{args.antenna}\" STVT_IFGR={args.ifgr} STVT_RFGAIN_SEL={args.rfgain}"
    for n in hw_notches:
        if n == "rfnotch_ctrl=true":
            cmd += " STVT_RFNOTCH=1"
        if n == "dabnotch_ctrl=true":
            cmd += " STVT_DABNOTCH=1"
    cmd += f" python tools/tv_live.py --rf {args.rf}"
    print(f"  {cmd}")


if __name__ == "__main__":
    main()
