"""clarke_fader.py — synthetic breathing channels for the replay rail.

Clarke's sum-of-sinusoids Rayleigh fading model (PySDR multipath ch.):
N random-phase sinusoids at Doppler spread f_D generate a statistically
correct time-varying complex channel gain. Multiply a CLEAN specimen's
IQ by it -> a breathing channel on demand, with known ground truth.
No more waiting for RF9's dawn window to test fade-tracking ideas.

Deterministic by seed (replay law: same inputs -> same specimen).
Modes:
  flat     — one Rayleigh tap (pure breathing, no frequency selectivity)
  ricean   — LOS + Rayleigh (K factor dB): gentler, more like RF9 whose
             median stays healthy while the tail dips
  selective— main tap Ricean + one delayed Rayleigh echo (canyon-style)

    python clarke_fader.py in.cs16 out.cs16 --fd 2.0 --mode ricean --k 8
"""
import argparse
import json
import time
from pathlib import Path

import numpy as np

FS = 8_000_000.0


def clarke_gain(n_samples, fd, fs, n_sin=16, rng=None):
    """Complex Rayleigh gain series, unit mean power (Clarke/Jakes)."""
    rng = rng or np.random.default_rng(1)
    t = np.arange(n_samples, dtype=np.float64) / fs
    g = np.zeros(n_samples, dtype=np.complex128)
    for _ in range(n_sin):
        theta = rng.uniform(0, 2 * np.pi)     # arrival angle
        phi = rng.uniform(0, 2 * np.pi)       # phase
        dopp = fd * np.cos(theta)
        g += np.exp(1j * (2 * np.pi * dopp * t + phi))
    g /= np.sqrt(n_sin)
    return g.astype(np.complex64)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("out")
    ap.add_argument("--fd", type=float, default=2.0,
                    help="Doppler spread Hz (coherence time ~ 0.423/fd)")
    ap.add_argument("--mode", choices=("flat", "ricean", "selective"),
                    default="ricean")
    ap.add_argument("--k", type=float, default=8.0,
                    help="Ricean K factor dB (LOS-to-scatter power)")
    ap.add_argument("--echo-us", type=float, default=2.0,
                    help="selective mode: echo delay in microseconds")
    ap.add_argument("--echo-db", type=float, default=-8.0,
                    help="selective mode: echo level dB below main")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--chunk-mb", type=int, default=256)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    inp, out = Path(args.inp), Path(args.out)
    n_total = inp.stat().st_size // 4          # cs16: 4 bytes/sample
    k_lin = 10 ** (args.k / 10.0)
    echo_n = int(round(args.echo_us * 1e-6 * FS))
    echo_a = 10 ** (args.echo_db / 20.0)

    # generate the gain series at a DECIMATED rate (fading is slow vs
    # 8 MS/s; 1 kHz gain samples then linear-interp keeps RAM sane)
    g_rate = max(200.0, args.fd * 50)
    n_g = int(n_total / FS * g_rate) + 4
    g1 = clarke_gain(n_g, args.fd, g_rate, rng=rng)
    g2 = clarke_gain(n_g, args.fd, g_rate, rng=rng)   # echo's own fading

    def gain_at(idx0, count, g):
        pos = (idx0 + np.arange(count, dtype=np.float64)) / FS * g_rate
        i0 = np.floor(pos).astype(np.int64)
        fr = (pos - i0).astype(np.float32)
        i0 = np.clip(i0, 0, len(g) - 2)
        return (g[i0] * (1 - fr) + g[i0 + 1] * fr)

    chunk = args.chunk_mb * 1024 * 1024 // 4
    tail = np.zeros(echo_n, dtype=np.complex64)
    done = 0
    with open(inp, "rb") as fi, open(out, "wb") as fo:
        while True:
            raw = np.frombuffer(fi.read(chunk * 4), dtype=np.int16)
            if raw.size == 0:
                break
            iq = raw.astype(np.float32).view(np.complex64) \
                if raw.size % 2 == 0 else None
            x = raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)
            n = len(x)
            if args.mode == "flat":
                y = x * gain_at(done, n, g1)
            else:
                los = np.sqrt(k_lin / (k_lin + 1))
                sca = np.sqrt(1.0 / (k_lin + 1))
                main = los + sca * gain_at(done, n, g1)
                y = x * main
                if args.mode == "selective":
                    xd = np.concatenate((tail, x))[:n]     # delayed copy
                    tail = x[-echo_n:].copy() if echo_n else tail
                    y = y + echo_a * xd * gain_at(done, n, g2)
            out_i = np.clip(np.round(y.real), -32767, 32767).astype(np.int16)
            out_q = np.clip(np.round(y.imag), -32767, 32767).astype(np.int16)
            inter = np.empty(2 * n, dtype=np.int16)
            inter[0::2] = out_i
            inter[1::2] = out_q
            fo.write(inter.tobytes())
            done += n
    meta = {"source": inp.name, "fd_hz": args.fd, "mode": args.mode,
            "k_db": args.k, "echo_us": args.echo_us,
            "echo_db": args.echo_db, "seed": args.seed,
            "coherence_time_ms": round(423.0 / args.fd, 1),
            "made": time.strftime("%Y-%m-%dT%H:%M:%S")}
    Path(str(out) + ".json").write_text(json.dumps(meta, indent=1))
    print(f"[fader] {out.name}: fd={args.fd} Hz "
          f"(Tc~{423.0/args.fd:.0f} ms), mode={args.mode}, "
          f"{done/FS:.1f}s written")


if __name__ == "__main__":
    main()
