"""e9_real_marginal.py — does the selector rule hold on REAL marginal signals?

Every cliff result tonight (E5/E5b/E6/E7) came from adding synthetic impairments
to ONE strong capture. That is controlled but artificial. The 7/25 session left
three captures of genuinely marginal REAL channels, so this is the out-of-sample
test of the rule those experiments produced:

    enable WL when the channel is marginal AND imag_benefit >= ~0.5;
    below ~0.4 imag_benefit, WL is the reversal zone and `long` is safer.

For each capture we get, from ONE sample-aligned run, both equalizers' frames and
the imag_benefit the selector would actually have seen. The rule is CONFIRMED if
WL wins where imag_benefit is mid-range and does NOT win where it is low; it is
REFUTED if the sign of the frame difference is unrelated to imag_benefit.

This is out-of-sample in the strict sense: the thresholds were fixed from the
synthetic sweeps BEFORE these four runs were scored.
"""
import os
import json
import subprocess
from pathlib import Path

PY = os.path.join(os.environ.get("USERPROFILE", ""), "radioconda", "python.exe")
REPO = Path(r"Z:\src\magic-tv-decoder")
IQD = REPO / "lab" / "marginal_iq"
OUT = REPO / "lab" / "night3" / "wl_real_marginal"

CAPS = [("rf34_ctrl", "strong control"),
        ("rf35_marg", "real marginal UHF"),
        ("rf7_marg",  "real marginal VHF-lo"),
        ("rf9_marg",  "real marginal VHF-lo")]


def predict(imag_ben, marginal):
    """The rule, applied BEFORE looking at the frame difference."""
    if imag_ben is None:
        return "n/a"
    if imag_ben >= 0.9 and not marginal:
        return "tie expected (strong: no room)"
    if imag_ben >= 0.5:
        return "WL should WIN"
    if imag_ben < 0.4:
        return "long safer (reversal zone)"
    return "borderline"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print("=== E9 real marginal captures — out-of-sample test of the selector rule ===",
          flush=True)
    print(f"{'capture':<11} {'long fr':>7} {'wl fr':>6} {'dfr':>6} | "
          f"{'long p50':>8} {'wl p50':>7} | {'imag_ben':>8} {'conj_fr':>7} | "
          f"prediction vs outcome", flush=True)
    rows = []
    for name, desc in CAPS:
        iq = IQD / f"{name}.cs16"
        if not iq.exists():
            print(f"{name:<11} MISSING"); continue
        d = OUT / name
        r = subprocess.run(
            [PY, str(REPO / "tools" / "tv_dual.py"), "--iq", str(iq),
             "--outdir", str(d), "--tag", name, "--seed", "42"],
            cwd=str(REPO), capture_output=True, text=True, timeout=5400)
        js, summ = d / f"{name}.json", d / f"{name}.summary.txt"
        rec = {"cap": name, "desc": desc, "rc": r.returncode}
        if js.exists():
            m = json.loads(js.read_text())
            rec["long"] = m.get("long", {}).get("frames")
            rec["wl"] = m.get("wl", {}).get("frames")
        if summ.exists():
            for ln in summ.read_text(errors="ignore").splitlines():
                p = ln.split()
                if len(p) >= 6 and p[0] in ("long", "wl"):
                    rec[f"{p[0]}_p50"] = float(p[5])
                if ln.startswith("imag_benefit "):
                    rec["imag_ben"] = float(ln.split()[2])
                if ln.startswith("conj_frac "):
                    rec["conj_fr"] = float(ln.split()[2])
        L, W = rec.get("long"), rec.get("wl")
        d_fr = (W - L) if (L is not None and W is not None) else None
        # "marginal" = the control decoded near-full and this one did not
        marginal = (L is not None and L < 380)
        pred = predict(rec.get("imag_ben"), marginal)
        if d_fr is None:
            outcome = "no data"
        elif d_fr > 5:
            outcome = "WL WON"
        elif d_fr < -5:
            outcome = "long won"
        else:
            outcome = "tie"
        rec.update({"d_fr": d_fr, "pred": pred, "outcome": outcome})
        print(f"{name:<11} {str(L):>7} {str(W):>6} "
              f"{('' if d_fr is None else f'{d_fr:+d}'):>6} | "
              f"{rec.get('long_p50', 0):>8.2f} {rec.get('wl_p50', 0):>7.2f} | "
              f"{rec.get('imag_ben', 0):>8.3f} {rec.get('conj_fr', 0):>7.3f} | "
              f"{pred}  ->  {outcome}", flush=True)
        if r.returncode:
            print(f"    rc={r.returncode} {(r.stderr or '')[-250:]}", flush=True)
        rows.append(rec)
    (OUT / "real_marginal.json").write_text(json.dumps(rows, indent=2))
    print(f"\nwrote {OUT/'real_marginal.json'}", flush=True)


if __name__ == "__main__":
    main()
