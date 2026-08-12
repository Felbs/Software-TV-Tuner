"""e5b_cliff_seeds.py — is WL's cliff-edge win real? (2026-07-30)

E5's margin curve produced one striking row: at 16 dB SNR, long delivered 150
frames and WL delivered 261 (+74%) — while WL's MEDIAN MER was 0.16 dB WORSE.
Two reasons that single row cannot be trusted as-is:

  1. Frame counts near the cliff are the least reproducible number we measure
     (banked law: +/-3 frames on marginal captures even with no change at all,
     and at a cliff the spread is far wider).
  2. One AWGN seed is one realization of the noise. The cliff is where a single
     unlucky burst decides whether a field survives, so seed choice can dominate.

So: repeat the cliff SNRs across independent noise seeds. Within each run the
comparison is already airtight (both equalizers see bit-identical symbols); the
seeds test whether the WL advantage SURVIVES resampling the noise.

Reports median and full spread per leg, plus how many seeds WL won, so the claim
that comes out of this is about a distribution rather than a lucky run.
"""
import os
import json
import statistics as st
import subprocess
from pathlib import Path

PY = os.path.join(os.environ.get("USERPROFILE", ""), "radioconda", "python.exe")
REPO = Path(r"Z:\src\magic-tv-decoder")
IQ = REPO / "lab" / "marginal_iq" / "rf34_ctrl.cs16"
OUT = REPO / "lab" / "night3" / "wl_cliff"
SIG_RMS = 13441.4
SEEDS = [42, 7, 1234, 99, 2026]
SNRS = [17, 16]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print("=== E5b cliff repeatability: does WL's win survive new noise seeds? ===",
          flush=True)
    allrows = []
    for snr in SNRS:
        amp = SIG_RMS / (10 ** (snr / 20.0))
        L, W = [], []
        print(f"\n-- {snr} dB (noise {amp:.0f}) --", flush=True)
        for sd in SEEDS:
            tag = f"snr{snr}_s{sd}"
            d = OUT / tag
            r = subprocess.run(
                [PY, str(REPO / "tools" / "tv_dual.py"), "--iq", str(IQ),
                 "--outdir", str(d), "--tag", tag, "--noise", f"{amp:.0f}",
                 "--seed", str(sd)],
                cwd=str(REPO), capture_output=True, text=True, timeout=5400)
            js = d / f"{tag}.json"
            fl = fw = None
            if js.exists():
                m = json.loads(js.read_text())
                fl = m.get("long", {}).get("frames")
                fw = m.get("wl", {}).get("frames")
            if fl is not None and fw is not None:
                L.append(fl); W.append(fw)
                print(f"   seed {sd:>5}: long {fl:>4}  wl {fw:>4}  "
                      f"delta {fw-fl:>+5}", flush=True)
            else:
                print(f"   seed {sd:>5}: FAILED rc={r.returncode}", flush=True)
            allrows.append({"snr": snr, "seed": sd, "long": fl, "wl": fw})
        if L:
            wins = sum(1 for a, b in zip(L, W) if b > a)
            print(f"   long  median {st.median(L):>6.0f}  range {min(L)}..{max(L)}",
                  flush=True)
            print(f"   wl    median {st.median(W):>6.0f}  range {min(W)}..{max(W)}",
                  flush=True)
            print(f"   WL won {wins}/{len(L)} seeds; median delta "
                  f"{st.median(W)-st.median(L):>+.0f} frames", flush=True)
    (OUT / "cliff_seeds.json").write_text(json.dumps(allrows, indent=2))
    print(f"\nwrote {OUT/'cliff_seeds.json'}", flush=True)


if __name__ == "__main__":
    main()
