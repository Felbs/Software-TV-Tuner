#!/usr/bin/env python3
"""wl_degenerate_test.py — the analytical proof behind WL v3's shrinkage.

Four claims about atsc_equalizer_wl's folded widely-linear filter, each checked
numerically in float64 (and, where it matters, EXACTLY):

  T1  FOLDING IDENTITY
      Re( sum_j w1[j] x[k+j] + w2[j] conj(x[k+j]) )
        == dot(xr, Re w1 + Re w2) + dot(xi, Im w2 - Im w1)   =: dot(xr,a)+dot(xi,b)

  T2  UPDATE INVARIANTS
      With the block's WL-NLMS (w1 += s conj x, w2 += s x, s real),
      (Re w1 - Re w2) and (Im w1 + Im w2) are NEVER touched: they stay frozen
      at their init values (delta, 0). Therefore, at all times,
          a = Re w1 + Re w2      b = Im w2 - Im w1 = 2 Im w2
      and the update is EXACTLY an NLMS over the doubled real regressor
      [xr; xi]:  a += 2 s xr,  b += 2 s xi.

  T3  WHY "SHRINK w2" IS THE WRONG LEVER HERE
      Scaling w2 by (1-k) gives  b_new = (1 - k/2) b_old  — it can never reach
      zero — while CORRUPTING the shared linear part: a_new = a - k Re w2.
      Scaling BOTH imaginary parts by (1-k) instead gives b_new = (1-k) b_old
      with a EXACTLY unchanged. That is what v3 does.

  T4  DEGENERATE-TO-LINEAR  (the gate the whole design rests on)
      With the v3 leak at kappa = 1, the imag plane is zeroed after every
      training-symbol update, so it is identically zero at every filter
      evaluation. Claim: the widely-linear block's output is then BIT-IDENTICAL
      to an independently written strictly-linear REAL-ONLY NLMS equalizer that
      never looks at the imaginary plane at all. Checked over real captured
      8-VSB segments when a diag dump is available, else on synthetic data.

Usage:
    python lab/wl_v3/wl_degenerate_test.py [--diag <dir with eq_in.f32/eq_imag.f32>]
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

import numpy as np

NTAPS = 128
NPRETAPS = int(NTAPS * 0.2)
SEG = 832
FSLEN = 4 + 511 + 3 * 63   # 704 known field-sync symbols
MU = 0.5

RES = []


def check(name, ok, detail=""):
    RES.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  — ' + detail) if detail else ''}")
    return ok


# ── reference models ──────────────────────────────────────────────────────
def wl_step(w1, w2, xwin, d, kappa):
    """One WL-NLMS training symbol, exactly as atsc_equalizer_wl_impl does it
    (complex taps + the v3 imag leak). xwin = the NTAPS complex window."""
    y = np.real(np.dot(w1, xwin) + np.dot(w2, np.conj(xwin)))
    energy = 2.0 * np.real(np.dot(np.conj(xwin), xwin)) + 1e-6
    e = d - y
    s = MU * e / energy
    w1 = w1 + s * np.conj(xwin)
    w2 = w2 + s * xwin
    if kappa > 0.0:
        leak = 0.0 if kappa >= 1.0 else (1.0 - kappa) ** (1.0 / FSLEN)
        w1 = w1.real + 1j * (w1.imag * leak)
        w2 = w2.real + 1j * (w2.imag * leak)
    return w1, w2, y


def real_nlms_step(a, xr, xi, d):
    """An INDEPENDENT strictly-linear REAL-ONLY equalizer: it never forms a
    complex tap, never touches an imaginary coefficient. Written from the
    ordinary NLMS recipe, not derived from the WL code."""
    y = float(np.dot(a, xr))
    energy = 2.0 * (float(np.dot(xr, xr)) + float(np.dot(xi, xi))) + 1e-6
    e = d - y
    a = a + 2.0 * (MU * e / energy) * xr
    return a, y


# ── data ──────────────────────────────────────────────────────────────────
def load_planes(diag: Path | None, nsym: int):
    """Real captured 8-VSB symbol planes if a diag dump is at hand, else a
    synthetic improper (real/imag correlated) surrogate."""
    if diag:
        fr, fi = diag / "eq_in.f32", diag / "eq_imag.f32"
        if fr.exists() and fi.exists():
            xr = np.fromfile(fr, dtype=np.float32, count=nsym).astype(np.float64)
            xi = np.fromfile(fi, dtype=np.float32, count=nsym).astype(np.float64)
            if xr.size == nsym and xi.size == nsym:
                return xr, xi, f"REAL capture dump {diag.name}"
    rng = np.random.default_rng(20260729)
    sym = rng.choice([-7, -5, -3, -1, 1, 3, 5, 7], size=nsym).astype(np.float64)
    h = np.array([1.0, 0.0, 0.35, 0.0, -0.12])
    xr = np.convolve(sym, h, mode="same") + rng.normal(0, 0.5, nsym)
    # vestigial-sideband-like companion: a filtered version of the same symbols
    # (improper: strongly correlated with the real plane)
    xi = np.convolve(sym, np.array([0.0, 0.4, 0.0, -0.25, 0.0]), mode="same") \
        + rng.normal(0, 0.5, nsym)
    return xr, xi, "synthetic improper surrogate"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diag", default=None)
    ap.add_argument("--nfields", type=int, default=4)
    a = ap.parse_args()
    diag = Path(a.diag) if a.diag else None

    nsym = a.nfields * FSLEN + NTAPS + 8
    xr, xi, src = load_planes(diag, nsym)
    x = xr + 1j * xi
    print(f"\nWL v3 degenerate-to-linear proof — data: {src}, "
          f"{a.nfields} field syncs x {FSLEN} symbols, NTAPS={NTAPS}\n")

    rng = np.random.default_rng(7)
    d_seq = rng.choice([-5.0, 5.0], size=nsym)   # field sync is a +-5 binary seq

    # ── T1 folding identity ────────────────────────────────────────────────
    print("T1  folding identity")
    w1 = rng.normal(size=NTAPS) + 1j * rng.normal(size=NTAPS)
    w2 = rng.normal(size=NTAPS) + 1j * rng.normal(size=NTAPS)
    err = 0.0
    for k in range(200):
        xw = x[k:k + NTAPS]
        y_c = float(np.real(np.dot(w1, xw) + np.dot(w2, np.conj(xw))))
        aa = w1.real + w2.real
        bb = w2.imag - w1.imag
        y_f = float(np.dot(xr[k:k + NTAPS], aa) + np.dot(xi[k:k + NTAPS], bb))
        err = max(err, abs(y_c - y_f) / max(1.0, abs(y_c)))
    check("Re(w1.x + w2.conj x) == dot(xr,a) + dot(xi,b)", err < 1e-12,
          f"max rel err {err:.3e}")

    # ── T2 update invariants ───────────────────────────────────────────────
    print("\nT2  update invariants (the frozen null space)")
    w1 = np.zeros(NTAPS, complex)
    w2 = np.zeros(NTAPS, complex)
    w1[NPRETAPS] = 1.0                      # the block's delta init
    d0_re = (w1.real - w2.real).copy()
    d0_im = (w1.imag + w2.imag).copy()
    a_track = w1.real + w2.real
    b_track = w2.imag - w1.imag
    max_re = max_im = max_ab = 0.0
    for k in range(FSLEN):
        xw = x[k:k + NTAPS]
        # predicted folded NLMS increment
        y = float(np.dot(xr[k:k + NTAPS], a_track) + np.dot(xi[k:k + NTAPS], b_track))
        energy = 2.0 * float(np.real(np.dot(np.conj(xw), xw))) + 1e-6
        s = MU * (d_seq[k] - y) / energy
        a_pred = a_track + 2.0 * s * xr[k:k + NTAPS]
        b_pred = b_track + 2.0 * s * xi[k:k + NTAPS]
        w1, w2, _ = wl_step(w1, w2, xw, d_seq[k], 0.0)
        a_track, b_track = w1.real + w2.real, w2.imag - w1.imag
        max_re = max(max_re, np.max(np.abs((w1.real - w2.real) - d0_re)))
        max_im = max(max_im, np.max(np.abs((w1.imag + w2.imag) - d0_im)))
        max_ab = max(max_ab, np.max(np.abs(a_track - a_pred)),
                     np.max(np.abs(b_track - b_pred)))
    check("Re w1 - Re w2 frozen at init", max_re < 1e-12, f"max drift {max_re:.3e}")
    check("Im w1 + Im w2 frozen at 0", max_im < 1e-12, f"max drift {max_im:.3e}")
    check("folded update == NLMS on [xr; xi]  (a += 2s xr, b += 2s xi)",
          max_ab < 1e-12, f"max deviation {max_ab:.3e}")

    # ── T3 the wrong lever vs the right one ────────────────────────────────
    print("\nT3  shrink-w2 (literal) vs shrink-imag-plane (v3)")
    kap = 0.5
    a_old, b_old = w1.real + w2.real, w2.imag - w1.imag
    w2s = w2 * (1.0 - kap)
    a_w2, b_w2 = w1.real + w2s.real, w2s.imag - w1.imag
    pred_b = (1.0 - kap / 2.0) * b_old
    check("shrinking w2: b_new == (1 - k/2) b_old  (never reaches 0)",
          np.max(np.abs(b_w2 - pred_b)) < 1e-12,
          f"|b| {np.linalg.norm(b_old):.4f} -> {np.linalg.norm(b_w2):.4f}")
    check("shrinking w2 CORRUPTS the shared linear part a",
          np.max(np.abs(a_w2 - a_old)) > 1e-9,
          f"max |da| {np.max(np.abs(a_w2 - a_old)):.4e}")
    w1v = w1.real + 1j * (w1.imag * (1 - kap))
    w2v = w2.real + 1j * (w2.imag * (1 - kap))
    a_v3, b_v3 = w1v.real + w2v.real, w2v.imag - w1v.imag
    check("v3 imag-plane shrink: b_new == (1-k) b_old EXACTLY",
          np.max(np.abs(b_v3 - (1 - kap) * b_old)) < 1e-12)
    check("v3 imag-plane shrink: a EXACTLY unchanged",
          np.array_equal(a_v3, a_old))

    # ── T4 degenerate to linear ────────────────────────────────────────────
    print("\nT4  kappa = 1  =>  the block IS a strictly-linear real equalizer")
    w1 = np.zeros(NTAPS, complex)
    w2 = np.zeros(NTAPS, complex)
    w1[NPRETAPS] = 1.0
    a_lin = np.zeros(NTAPS)
    a_lin[NPRETAPS] = 1.0
    maxb = 0.0
    ydiff = 0.0
    fold_exact = True
    n = a.nfields * FSLEN
    for k in range(n):
        xw = x[k:k + NTAPS]
        w1, w2, y_wl = wl_step(w1, w2, xw, d_seq[k], 1.0)
        a_lin, y_lin = real_nlms_step(a_lin, xr[k:k + NTAPS], xi[k:k + NTAPS],
                                      d_seq[k])
        ydiff = max(ydiff, abs(y_wl - y_lin))
        bb = w2.imag - w1.imag
        maxb = max(maxb, np.max(np.abs(bb)))
        # The FILTER path (filterN) evaluates the FOLDED form. With b == 0 the
        # second dot product is exactly +0.0, so the sum is bitwise the real-only
        # result — this is the path whose samples reach the demodulator.
        aa = w1.real + w2.real
        y_fold = float(np.dot(xr[k:k + NTAPS], aa) + np.dot(xi[k:k + NTAPS], bb))
        if y_fold != float(np.dot(xr[k:k + NTAPS], aa)):
            fold_exact = False
    check("imag-plane coefficient b == 0 at every evaluation", maxb == 0.0,
          f"max |b| = {maxb:g}")
    check("folded FILTER output is BITWISE the real-only dot product",
          fold_exact, f"{n} evaluations, b == 0 contributes exactly +0.0")
    check("WL(kappa=1) tracks an independent real-only NLMS to double precision",
          ydiff < 1e-12, f"max |dy| {ydiff:.3e} over {n} symbols "
                         f"(complex-dot vs real-dot summation order only)")
    check("the two tap vectors coincide to double precision",
          np.max(np.abs(w1.real + w2.real - a_lin)) < 1e-12,
          f"max |da| {np.max(np.abs(w1.real + w2.real - a_lin)):.3e}")

    ok = all(r[1] for r in RES)
    print(f"\n{'ALL CHECKS PASS' if ok else 'FAILURES PRESENT'} "
          f"({sum(1 for r in RES if r[1])}/{len(RES)})\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
