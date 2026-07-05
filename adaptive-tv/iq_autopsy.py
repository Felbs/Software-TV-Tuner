"""iq_autopsy.py — offline deep analysis of a raw IQ capture. No radio
needed: runs anytime, including while live trials own the tuner.

v1 answers the questions live probes smear over (they average away time):
  1  PILOT DYNAMICS — phase and frequency of the pilot tone vs time in
     10 ms windows: is the pilot steady (FPLL should lock) or churning
     (flutter/doppler — the loop's real enemy)?
  2  RIPPLE DYNAMICS — in-band frequency-selective fading vs time:
     ripple depth per 100 ms slice. Static ripple = fixed echoes
     (equalizer's job); breathing ripple = dynamic multipath.
  3  AM ON THE PILOT — amplitude envelope of the pilot band: fade rate
     and depth.

Usage: python iq_autopsy.py lab/iq_rf7_*.ciq
"""
import glob
import json
import sys
from pathlib import Path

import numpy as np

PILOT_OFF = 309_440.0        # pilot sits lower-edge + 309.44 kHz


def main():
    pat = sys.argv[1] if len(sys.argv) > 1 else r"Z:\src\adaptive-tv\lab\iq_*.ciq"
    files = sorted(glob.glob(pat))
    if not files:
        print("no capture matches", pat)
        return
    path = Path(files[-1])
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    rate = meta["rate"]
    center = meta["center_hz"]
    print(f"=== IQ AUTOPSY: {path.name} ===")
    print(f"    {meta['secs']} s @ {rate/1e6:.1f} MS/s, center "
          f"{center/1e6:.3f} MHz, continuity {meta['continuity_pct']}%")

    raw = np.fromfile(path, dtype=np.int16)
    x = (raw[0::2].astype(np.float32)
         + 1j * raw[1::2].astype(np.float32)) / 32768.0
    n = len(x)
    secs = n / rate

    # pilot baseband offset: pilot_abs - center  (center = lower edge + 3 MHz)
    pilot_bb = PILOT_OFF - 3e6
    t = np.arange(n, dtype=np.float64) / rate
    mixed = x * np.exp(-2j * np.pi * pilot_bb * t).astype(np.complex64)
    # decimate hard around the pilot: 8 MS/s -> 15.6 kS/s (512:1) via 2-stage
    d1 = mixed.reshape(-1, 32).mean(axis=1)          # -> 250 kS/s
    d2 = d1.reshape(-1, 16).mean(axis=1)             # -> 15.6 kS/s
    prate = rate / 512.0

    # 1: pilot frequency + phase churn in 10 ms windows
    ph = np.unwrap(np.angle(d2))
    inst_f = np.diff(ph) * prate / (2 * np.pi)
    win = max(1, int(0.010 * prate))
    nw = len(inst_f) // win
    fwin = inst_f[:nw * win].reshape(nw, win).mean(axis=1)
    # phase deviation around each window's own linear trend
    phw = ph[:nw * win].reshape(nw, win)
    detr = phw - np.polyval(np.polyfit(np.arange(win), phw.T, 1),
                            np.arange(win)[:, None]).T
    ph_rms_deg = np.degrees(np.sqrt((detr ** 2).mean(axis=1)))
    print("\n[1] PILOT DYNAMICS (10 ms windows)")
    print(f"    mean offset  {fwin.mean():+8.1f} Hz")
    print(f"    freq wander  p5 {np.percentile(fwin,5):+.1f} / "
          f"p95 {np.percentile(fwin,95):+.1f} Hz  "
          f"(span {np.percentile(fwin,95)-np.percentile(fwin,5):.1f} Hz)")
    print(f"    phase churn  median {np.median(ph_rms_deg):.1f}° / "
          f"p95 {np.percentile(ph_rms_deg,95):.1f}° per window")
    print(f"    verdict: {'STEADY — FPLL should lock; suspect elsewhere' if np.percentile(ph_rms_deg,95) < 15 and (np.percentile(fwin,95)-np.percentile(fwin,5)) < 60 else 'CHURNING — dynamic flutter faster than the loop; this IS the assassin'}")

    # 2: in-band ripple vs time (100 ms slices, 4 kHz-resolution FFTs)
    NF = 2048
    hop = int(0.100 * rate)
    slices = int((n - NF) / hop)
    ripples = []
    inband = None
    for s in range(min(slices, 200)):
        seg = x[s * hop: s * hop + NF] * np.hanning(NF)
        psd = np.abs(np.fft.fftshift(np.fft.fft(seg))) ** 2
        fax = np.fft.fftshift(np.fft.fftfreq(NF, 1 / rate))
        if inband is None:
            inband = np.abs(fax) < 2.5e6        # central 5 MHz
        p = 10 * np.log10(psd[inband] + 1e-20)
        # smooth 5 bins to ignore noise texture, measure shape
        k = np.convolve(p, np.ones(5) / 5, mode="valid")
        ripples.append(k.max() - k.min())
    ripples = np.array(ripples)
    print("\n[2] IN-BAND RIPPLE (100 ms slices, central 5 MHz)")
    print(f"    depth median {np.median(ripples):.1f} dB / "
          f"p95 {np.percentile(ripples,95):.1f} dB")
    print(f"    breathing (p95-p5 across slices): "
          f"{np.percentile(ripples,95)-np.percentile(ripples,5):.1f} dB")
    print(f"    verdict: {'FLAT-ish channel' if np.median(ripples) < 8 else ('STATIC deep ripple — fixed echoes, an equalizer problem' if (np.percentile(ripples,95)-np.percentile(ripples,5)) < 4 else 'BREATHING deep ripple — dynamic multipath')}")

    # 3: pilot amplitude fading
    env = np.abs(d2)
    ewin = env[:nw * win].reshape(nw, win).mean(axis=1)
    fade_db = 20 * np.log10((ewin + 1e-12) / np.median(ewin))
    print("\n[3] PILOT AMPLITUDE (10 ms windows)")
    print(f"    fade depth p5 {np.percentile(fade_db,5):+.1f} dB / "
          f"p95 {np.percentile(fade_db,95):+.1f} dB")
    print(f"    fades below -6 dB: "
          f"{100*(fade_db < -6).mean():.1f}% of windows")
    print(f"\ncapture length analyzed: {secs:.2f} s")


if __name__ == "__main__":
    main()
