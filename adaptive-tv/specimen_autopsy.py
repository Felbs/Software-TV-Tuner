"""specimen_autopsy.py — dissect a Glitch Specimen Recorder capture.

Stage 1 (RF forensics, fast): envelope timeline (10 ms bins) hunting
fades and impulse spikes; spectral-ripple comparison (first vs last 2 s)
hunting multipath changes. Verdict:
    IMPULSE   spikes >6x median envelope
    FADE      envelope dip >3 dB sustained >100 ms
    SHIFT     in-band ripple profile changed >4 dB between ends
    CLEAN-RF  none of the above — the RF was pristine, so the glitch
              was born INSIDE the chain (the most damning verdict)

Stage 2 (--replay): convert cs16 -> cf32 and run tv_replay.py (the
byte-faithful offline chain) with FEC narration, printing the rs-bad
timeline aligned to stage 1's RF events.

    python specimen_autopsy.py <specimen.cs16> [--replay]
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

PY = sys.executable
REPLAY = Path(r"Z:\src\magic-tv-decoder\tools\tv_replay.py")


def load(path):
    raw = np.fromfile(path, dtype=np.int16)
    x = raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)
    meta = {}
    mp = Path(str(path).replace(".cs16", ".json"))
    if mp.exists():
        meta = json.loads(mp.read_text())
    rate = float(meta.get("sample_rate", 8e6))
    return x, rate, meta


def stage1(x, rate):
    bin_n = int(rate * 0.010)                       # 10 ms bins
    nb = len(x) // bin_n
    env = np.sqrt(
        (np.abs(x[:nb * bin_n]) ** 2).reshape(nb, bin_n).mean(axis=1))
    med = float(np.median(env))
    events = []

    # impulses: per-bin peak vs median envelope
    pk = np.abs(x[:nb * bin_n]).reshape(nb, bin_n).max(axis=1)
    for i in np.where(pk > 6 * med)[0]:
        events.append((i * 0.010, "IMPULSE", f"peak {pk[i]/med:.1f}x median"))

    # fades: sustained dip > 3 dB (>= 10 consecutive bins = 100 ms)
    low = env < med * 0.708                          # -3 dB
    run = 0
    for i, v in enumerate(low):
        run = run + 1 if v else 0
        if run == 10:
            events.append(((i - 9) * 0.010, "FADE",
                           f"{20*np.log10(env[i]/med):.1f} dB"))

    # spectral ripple shift: first vs last 2 s in-band PSD profile
    def ripple(seg):
        f = np.fft.fftshift(np.abs(np.fft.fft(seg[:2**20])) ** 2)
        b = f.reshape(256, -1).mean(axis=1)
        band = b[64:192]                             # middle half = in-band
        db = 10 * np.log10(band / band.mean() + 1e-12)
        return db
    r0, r1 = ripple(x[:int(2 * rate)]), ripple(x[-int(2 * rate):])
    shift = float(np.abs(r0 - r1).max())
    if shift > 4.0:
        events.append((None, "SHIFT", f"ripple change {shift:.1f} dB"))

    verdict = ("CLEAN-RF" if not events else
               sorted({e[1] for e in events})[0] if len(
                   {e[1] for e in events}) == 1 else "MIXED")
    return env, med, events, shift, verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("specimen")
    ap.add_argument("--replay", action="store_true")
    args = ap.parse_args()

    x, rate, meta = load(args.specimen)
    print(f"specimen: {Path(args.specimen).name}")
    print(f"  rf {meta.get('rf')} {meta.get('antenna')} "
          f"{len(x)/rate:.1f}s @ {rate/1e6:.1f} MS/s")
    print(f"  reason: {meta.get('reason')}")

    env, med, events, shift, verdict = stage1(x, rate)
    print(f"  envelope median {med:.0f}, ripple shift {shift:.1f} dB")
    for t, kind, det in events[:12]:
        ts = f"t={t:6.3f}s" if t is not None else "whole-file"
        print(f"    {ts}  {kind:8s} {det}")
    if len(events) > 12:
        print(f"    ... {len(events)-12} more events")
    print(f"  STAGE-1 VERDICT: {verdict}")

    if args.replay:
        rf = meta.get("rf", 36)
        with tempfile.TemporaryDirectory() as td:
            cf32 = Path(td) / "specimen.cf32"
            (x / 32768.0).astype(np.complex64).tofile(cf32)
            out = Path(td) / "replay.ts"
            log = Path(td) / "replay.log"
            import os
            env2 = os.environ.copy()
            env2.update({"STVT_RS": "erasure", "STVT_RS_ERASURES": "0",
                         "STVT_EQ_TELEM": "1", "STVT_EQ": "long",
                         "STVT_VITERBI": "soft",
                         "STVT_DABNOTCH": "0" if int(rf) < 14 else "1"})
            subprocess.run([PY, str(REPLAY), "--iq", str(cf32),
                            "--out", str(out), "--log", str(log)],
                           env=env2, timeout=600)
            txt = log.read_text(errors="ignore")
            print("\n  REPLAY rs timeline:")
            for ln in txt.splitlines():
                if "rs_erasure t=" in ln:
                    print("   ", ln.split("]")[0] + "]",
                          ln.split("(last5s:")[-1].rstrip(") "))
            ts_size = out.stat().st_size if out.exists() else 0
            hdrs = out.read_bytes().count(b"\x00\x00\x01\xb3") if ts_size else 0
            print(f"  replay TS: {ts_size} bytes, {hdrs} seq headers")


if __name__ == "__main__":
    main()
