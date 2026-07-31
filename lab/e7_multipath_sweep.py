"""e7_multipath_sweep.py — the stressor that should actually feed WL (2026-07-30).

Story so far tonight, all measured on rf34_ctrl.cs16 through tv_dual (both
equalizers fed bit-identical symbols):

  E5  AWGN (proper): WL's MER edge decays monotonically and goes NEGATIVE below
      ~17 dB. Proper noise adds nothing WL can exploit; WL still pays for twice
      the parameters.
  E5b At the cliff WL's FRAME win is real and large: 10/10 seeds, +45 frames at
      17 dB and +114 at 16 dB, distributions non-overlapping.
  E6  I/Q imbalance (x + alpha*conj(x)) STARVES WL on a clean signal
      (+2.53 -> +0.40 dB as alpha 0 -> .10; imag_benefit 0.932 -> 0.768) because
      the algebra scales Im DOWN by (1-alpha) — and 8-VSB is already essentially
      real, so there is no impropriety left to add. BUT at the 17 dB cliff a mild
      imbalance GREW WL's frame win +54 -> +131.

Synthesis: WL is not a median-MER mechanism, it is a TAIL / robustness mechanism,
and its food is the channel making the IMAGINARY part carry information
INDEPENDENT of the real part. A delayed, COMPLEX-scaled echo is exactly that:

    y[n] = x[n] + g*exp(j*phi)*x[n-D]

PREDICTION: WL's frame advantage GROWS with echo strength g, and grows more for a
complex phase than for a real one (phi=0), because a real echo leaves the channel
response real and gives WL's conjugate branch nothing extra to model.

Falsified if the advantage is flat in g, or if phi makes no difference — that
would mean WL is merely acting as a longer effective filter rather than
exploiting the conjugate structure.
"""
import json
import subprocess
from pathlib import Path

PY = r"C:\Users\user\radioconda\python.exe"
REPO = Path(r"Z:\src\magic-tv-decoder")
IQ = REPO / "lab" / "marginal_iq" / "rf34_ctrl.cs16"
OUT = REPO / "lab" / "night3" / "wl_multipath"
SIG_RMS = 13441.4
SNR17 = SIG_RMS / (10 ** (17 / 20.0))

GAINS = [0.0, 0.15, 0.30, 0.45]
# phase 0.0 = REAL echo (control): if WL's gain is about the conjugate branch,
# the complex echo should help it more than the real one.
ARMS = [("cx_clean", 0.7, None), ("cx_snr17", 0.7, SNR17),
        ("re_snr17", 0.0, SNR17)]


def run(tag, g, phase, amp):
    d = OUT / tag
    cmd = [PY, str(REPO / "tools" / "tv_dual.py"), "--iq", str(IQ),
           "--outdir", str(d), "--tag", tag, "--seed", "42"]
    if g > 0:
        cmd += ["--echo", str(g), "--echo-delay", "8", "--echo-phase", str(phase)]
    if amp is not None:
        cmd += ["--noise", f"{amp:.0f}"]
    r = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                       timeout=5400)
    js, summ = d / f"{tag}.json", d / f"{tag}.summary.txt"
    rec = {"g": g, "phase": phase, "rc": r.returncode}
    if js.exists():
        m = json.loads(js.read_text())
        rec["long_frames"] = m.get("long", {}).get("frames")
        rec["wl_frames"] = m.get("wl", {}).get("frames")
    if summ.exists():
        for ln in summ.read_text(errors="ignore").splitlines():
            p = ln.split()
            if len(p) >= 6 and p[0] in ("long", "wl"):
                rec[f"{p[0]}_mer_p50"] = float(p[5])
            if ln.startswith("imag_benefit "):
                rec["imag_benefit"] = ln.split()[2]
            if ln.startswith("conj_frac "):
                rec["conj_frac"] = ln.split()[2]
    if rec.get("rc"):
        rec["stderr_tail"] = (r.stderr or "")[-300:]
    return rec


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print("=== E7 multipath sweep: y[n] = x[n] + g*exp(j*phi)*x[n-8] ===",
          flush=True)
    print("PREDICTION: WL frame advantage GROWS with g, and more for the COMPLEX "
          "echo (phi=0.7) than the REAL one (phi=0).\n", flush=True)
    out = {}
    for arm, phase, amp in ARMS:
        label = ("complex echo, clean" if arm == "cx_clean" else
                 "complex echo, 17 dB" if arm == "cx_snr17" else
                 "REAL echo (control), 17 dB")
        print(f"-- {arm}: {label} --", flush=True)
        print(f"{'g':>5} | {'long fr':>7} {'wl fr':>6} {'dfr':>5} | "
              f"{'long p50':>8} {'wl p50':>7} {'dMER':>6} | "
              f"{'imag_ben':>8} {'conj_frac':>9}", flush=True)
        rows = []
        for g in GAINS:
            rec = run(f"{arm}_g{g}", g, phase, amp)
            dfr = (rec.get("wl_frames") or 0) - (rec.get("long_frames") or 0)
            dm = (rec.get("wl_mer_p50") or 0) - (rec.get("long_mer_p50") or 0)
            print(f"{g:>5.2f} | {str(rec.get('long_frames')):>7} "
                  f"{str(rec.get('wl_frames')):>6} {dfr:>+5} | "
                  f"{rec.get('long_mer_p50', 0):>8.2f} "
                  f"{rec.get('wl_mer_p50', 0):>7.2f} {dm:>+6.2f} | "
                  f"{str(rec.get('imag_benefit', '-')):>8} "
                  f"{str(rec.get('conj_frac', '-')):>9}", flush=True)
            if rec.get("rc"):
                print(f"      rc={rec['rc']} {rec.get('stderr_tail','')}", flush=True)
            rows.append(rec)
        out[arm] = rows
        print(flush=True)
    (OUT / "multipath.json").write_text(json.dumps(out, indent=2))
    print(f"wrote {OUT/'multipath.json'}", flush=True)


if __name__ == "__main__":
    main()
