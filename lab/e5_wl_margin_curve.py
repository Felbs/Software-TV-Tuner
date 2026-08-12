"""e5_wl_margin_curve.py — the WL-vs-long MARGIN CURVE (2026-07-30).

Answers what every previous WL test only gestured at: at what SNR does the
widely-linear equalizer start to earn its keep, and by how much?

Why this beats every live gate we have run:
  * tv_dual feeds BOTH equalizers from ONE shared front end, so the two TS files
    come from bit-identical symbols — the only difference is the equalizer.
  * AWGN is added once, upstream of the split, so both legs see the IDENTICAL
    noisy stream. Run-to-run channel variance is what swamped the live RF34/RF36
    gates (2637 vs 2627 frames is inside the noise of two separate runs).
  * We CHOOSE the SNR instead of waiting for propagation to hand us a marginal
    channel — reproducible, and it sweeps the cliff on purpose.

Calibration (measured, not assumed): rf34_ctrl.cs16 raw int16 rms = 13441, and
blocks.interleaved_short_to_complex(..., 32767.0) DIVIDES by 32767 before the
x32768 scaler, so signal rms into the noise adder is 13441.4:
    noise_amp = 13441.4 / 10**(SNR_dB/20)
That puts tv_dual's own docstring example (--noise 2147) at 15.9 dB, right on the
watchability cliff measured 7/09. That agreement IS the calibration check.

Clean-signal smoke run established the shape of the answer: frames tie 403/403
but WL holds +2.454 dB MER, better in 612 of 620 paired fields. So WL's headroom
is real even when frames can't show it; this sweep finds where headroom becomes
frames.
"""
import os
import json
import subprocess
import sys
from pathlib import Path

PY = os.path.join(os.environ.get("USERPROFILE", ""), "radioconda", "python.exe")
REPO = Path(r"Z:\src\magic-tv-decoder")
IQ = REPO / "lab" / "marginal_iq" / "rf34_ctrl.cs16"
OUT = REPO / "lab" / "night3" / "wl_margin"
SIG_RMS = 13441.4

LADDER = [None, 24, 20, 18, 16, 15, 14, 13, 12, 10]


def main():
    assert IQ.exists(), IQ
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"=== E5 WL margin curve — {IQ.name} "
          f"({IQ.stat().st_size/4/8e6:.0f} s), seed 42 ===", flush=True)
    print(f"{'SNR':>6} {'noise':>6} | {'long fr':>7} {'wl fr':>7} {'dfr':>5} "
          f"| {'long p50':>8} {'wl p50':>7} {'dMER':>6} | {'WL/long/tie fields':>18}",
          flush=True)
    rows = []
    for snr in LADDER:
        amp = None if snr is None else SIG_RMS / (10 ** (snr / 20.0))
        tag = "clean" if snr is None else f"snr{snr}"
        d = OUT / tag
        cmd = [PY, str(REPO / "tools" / "tv_dual.py"), "--iq", str(IQ),
               "--outdir", str(d), "--tag", tag, "--seed", "42"]
        if amp is not None:
            cmd += ["--noise", f"{amp:.0f}"]
        r = subprocess.run(cmd, cwd=str(REPO), capture_output=True,
                           text=True, timeout=5400)
        js, summ = d / f"{tag}.json", d / f"{tag}.summary.txt"
        rec = {"snr_db": snr, "noise_amp": amp, "rc": r.returncode}
        if js.exists():
            m = json.loads(js.read_text())
            rec["long_frames"] = m.get("long", {}).get("frames")
            rec["wl_frames"] = m.get("wl", {}).get("frames")
            rec["adv_pct"] = m.get("wl_frame_advantage")
        # MER percentiles + paired-field tally live in the summary table
        if summ.exists():
            for ln in summ.read_text(errors="ignore").splitlines():
                p = ln.split()
                if len(p) >= 6 and p[0] in ("long", "wl"):
                    rec[f"{p[0]}_mer_p5"] = float(p[3])
                    rec[f"{p[0]}_mer_p50"] = float(p[5])
                if "MER advantage" in ln:
                    rec["paired"] = ln.strip()
        dfr = (rec.get("wl_frames") or 0) - (rec.get("long_frames") or 0)
        dmer = (rec.get("wl_mer_p50") or 0) - (rec.get("long_mer_p50") or 0)
        tally = ""
        if rec.get("paired") and "(" in rec["paired"]:
            tally = rec["paired"].split("(")[-1].rstrip(")")
        print(f"{('clean' if snr is None else f'{snr}dB'):>6} "
              f"{('-' if amp is None else f'{amp:.0f}'):>6} | "
              f"{str(rec.get('long_frames')):>7} {str(rec.get('wl_frames')):>7} "
              f"{dfr:>+5} | {rec.get('long_mer_p50', 0):>8.2f} "
              f"{rec.get('wl_mer_p50', 0):>7.2f} {dmer:>+6.2f} | {tally:>18}",
              flush=True)
        if r.returncode != 0:
            print(f"       rc={r.returncode} {(r.stderr or '')[-300:]}", flush=True)
        rows.append(rec)
    (OUT / "margin_curve.json").write_text(json.dumps(rows, indent=2))
    print(f"\nwrote {OUT/'margin_curve.json'}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
