"""e6_impropriety_sweep.py — the MIRROR of E5: does WL's win grow with
impropriety? (2026-07-30)

E5 established that WL's advantage DECAYS and then reverses as AWGN rises
(+2.54 dB clean -> -0.49 dB at 10 dB SNR), because AWGN is circular/proper: it
adds no impropriety for WL to exploit while WL still pays for estimating twice
the parameters.

That theory makes a falsifiable prediction in the other direction. Inject the
canonical IMPROPER impairment
        y = x + alpha * conj(x)
(the second-order signature of I/Q gain/phase imbalance and of single-sideband
interference) and WL's advantage should GROW with alpha, while `long` — which
has no conjugate branch and structurally cannot model it — should degrade.

If WL's advantage instead stays flat or shrinks with alpha, the impropriety
explanation for WL's behaviour is WRONG and the real mechanism is something else
(e.g. it is only ever a longer effective filter). Either result is worth having;
this is the experiment that can actually tell them apart.

Held fixed: same control capture, same seed, no AWGN — alpha is the ONLY variable.
Run at a mild AWGN floor too (17 dB, where E5 showed WL's cliff-edge win) to see
whether impropriety and noise interact.
"""
import os
import json
import subprocess
from pathlib import Path

PY = os.path.join(os.environ.get("USERPROFILE", ""), "radioconda", "python.exe")
REPO = Path(r"Z:\src\magic-tv-decoder")
IQ = REPO / "lab" / "marginal_iq" / "rf34_ctrl.cs16"
OUT = REPO / "lab" / "night3" / "wl_improper"
SIG_RMS = 13441.4

ALPHAS = [0.0, 0.05, 0.10, 0.20, 0.35, 0.50]
# arm A = impropriety alone; arm B = impropriety on top of the 17 dB cliff edge
NOISE_ARMS = [("clean", None), ("snr17", SIG_RMS / (10 ** (17 / 20.0)))]


def run(tag, alpha, amp):
    d = OUT / tag
    cmd = [PY, str(REPO / "tools" / "tv_dual.py"), "--iq", str(IQ),
           "--outdir", str(d), "--tag", tag, "--seed", "42"]
    if alpha > 0:
        cmd += ["--conj", str(alpha)]
    if amp is not None:
        cmd += ["--noise", f"{amp:.0f}"]
    r = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                       timeout=5400)
    js, summ = d / f"{tag}.json", d / f"{tag}.summary.txt"
    rec = {"alpha": alpha, "rc": r.returncode}
    if js.exists():
        m = json.loads(js.read_text())
        rec["long_frames"] = m.get("long", {}).get("frames")
        rec["wl_frames"] = m.get("wl", {}).get("frames")
    if summ.exists():
        for ln in summ.read_text(errors="ignore").splitlines():
            p = ln.split()
            if len(p) >= 6 and p[0] in ("long", "wl"):
                rec[f"{p[0]}_mer_p50"] = float(p[5])
            if "MER advantage" in ln and "(" in ln:
                rec["tally"] = ln.split("(")[-1].rstrip(")").strip()
            if ln.startswith("imag_benefit "):
                rec["imag_benefit"] = ln.split()[2]
    if rec.get("rc") != 0:
        rec["stderr_tail"] = (r.stderr or "")[-300:]
    return rec


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print("=== E6 impropriety sweep: y = x + alpha*conj(x) ===", flush=True)
    print("PREDICTION: WL advantage GROWS with alpha (opposite of the AWGN "
          "trend). Falsified if flat/shrinking.\n", flush=True)
    out = {}
    for arm, amp in NOISE_ARMS:
        print(f"-- {arm} " + ("(no AWGN)" if amp is None
                              else f"(AWGN {amp:.0f} = 17 dB)") + " --", flush=True)
        print(f"{'alpha':>6} | {'long fr':>7} {'wl fr':>6} {'dfr':>5} | "
              f"{'long p50':>8} {'wl p50':>7} {'dMER':>6} | {'imag_ben':>8}",
              flush=True)
        rows = []
        for a in ALPHAS:
            rec = run(f"{arm}_a{a}", a, amp)
            dfr = (rec.get("wl_frames") or 0) - (rec.get("long_frames") or 0)
            dm = (rec.get("wl_mer_p50") or 0) - (rec.get("long_mer_p50") or 0)
            print(f"{a:>6.2f} | {str(rec.get('long_frames')):>7} "
                  f"{str(rec.get('wl_frames')):>6} {dfr:>+5} | "
                  f"{rec.get('long_mer_p50', 0):>8.2f} "
                  f"{rec.get('wl_mer_p50', 0):>7.2f} {dm:>+6.2f} | "
                  f"{str(rec.get('imag_benefit', '-')):>8}", flush=True)
            if rec.get("rc"):
                print(f"        rc={rec['rc']} {rec.get('stderr_tail','')}",
                      flush=True)
            rows.append(rec)
        out[arm] = rows
        print(flush=True)
    (OUT / "impropriety.json").write_text(json.dumps(out, indent=2))
    print(f"wrote {OUT/'impropriety.json'}", flush=True)


if __name__ == "__main__":
    main()
