"""Test harness: replay captured I/Q fixtures against every combination
of detection algorithms and rank by precision / recall / F1 vs the
ground-truth manifest.

Workflow:
  1) Run capture_fixtures.py once against the live SDR. That writes
     fixtures/rf*.cf32 + fixtures/manifest.json.
  2) Run this harness. It loads every fixture, runs every detector, and
     for each detector (and each pair / triple / full set) sweeps a
     threshold to find the best F1 against ground_truth_dc.json.
  3) Print a scoreboard: which combo + thresholds caught the most real
     ATSC channels with the fewest false positives, and how fast.

Goal: identify the most accurate AND lightweight detection recipe to
bake into sdr_sweep.py. The harness uses CPU only — no SDR — so it
runs in seconds and is easy to iterate on.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from detectors import DETECTORS, run_all  # noqa: E402

FIXTURE_DIR = HERE / "fixtures"
GROUND_TRUTH = HERE / "ground_truth_dc.json"


def load_fixture(rf: int, manifest: dict) -> np.ndarray | None:
    info = manifest["captures"].get(str(rf))
    if info is None:
        return None
    path = FIXTURE_DIR / info["file"]
    if not path.exists():
        return None
    return np.fromfile(path, dtype=np.complex64)


def load_ground_truth() -> tuple[set[int], set[int], set[int]]:
    """Return (atsc1_must_detect, atsc1_marginal, atsc1_must_reject)."""
    g = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    must_detect = set()
    marginal = set()
    for ch in g["channels"]:
        if ch["expected"] == "atsc1":
            must_detect.add(ch["rf"])
        elif ch["expected"] == "atsc1_marginal":
            marginal.add(ch["rf"])
    must_reject = set(g.get("expected_empty_rfs", []))
    return must_detect, marginal, must_reject


def evaluate(decisions: dict[int, bool],
              must_detect: set[int],
              marginal: set[int],
              must_reject: set[int]) -> dict:
    """Compute precision/recall/F1 against ground truth.

    Marginal channels are not penalized either way — they're 'allowed
    but optional'. Hits on them count as bonus, misses don't hurt."""
    tp = sum(1 for rf in must_detect if decisions.get(rf, False))
    fn = sum(1 for rf in must_detect if not decisions.get(rf, False))
    fp = sum(1 for rf in must_reject if decisions.get(rf, False))
    bonus = sum(1 for rf in marginal if decisions.get(rf, False))
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) else 0.0)
    return {
        "tp": tp, "fn": fn, "fp": fp, "marginal_hits": bonus,
        "precision": precision, "recall": recall, "f1": f1,
    }


def run_detectors_on_all(manifest: dict, rfs: list[int],
                          which: list[str]) -> tuple[dict, float]:
    """For each rf in `rfs`, run each detector in `which`. Returns a
    {rf: {detector: score}} dict + total CPU time."""
    sr = manifest["sample_rate_hz"]
    out: dict[int, dict[str, float]] = {}
    t0 = time.time()
    for rf in rfs:
        samples = load_fixture(rf, manifest)
        if samples is None:
            continue
        out[rf] = {}
        for name in which:
            res = DETECTORS[name](samples, sr)
            out[rf][name] = float(res.get("score", float("-inf")))
    return out, time.time() - t0


def sweep_threshold(scores: dict[int, dict[str, float]],
                     detector: str,
                     candidates: list[float],
                     must_detect: set[int],
                     marginal: set[int],
                     must_reject: set[int]) -> dict:
    """Find the threshold for `detector` that maximizes F1."""
    best = {"f1": -1.0}
    for thr in candidates:
        decisions = {rf: scores[rf][detector] >= thr for rf in scores}
        ev = evaluate(decisions, must_detect, marginal, must_reject)
        if ev["f1"] > best.get("f1", -1.0):
            best = {**ev, "threshold": thr, "detector": detector}
    return best


def sweep_combo(scores: dict[int, dict[str, float]],
                 detectors: tuple[str, ...],
                 thresholds_per: dict[str, list[float]],
                 must_detect: set[int],
                 marginal: set[int],
                 must_reject: set[int]) -> dict:
    """For an AND-combo of detectors, find the per-detector thresholds
    that jointly maximize F1. Greedy 1D sweeps so it stays fast."""
    # Start by picking the best individual threshold for each, then
    # iterate one detector at a time refining on the joint decision.
    chosen: dict[str, float] = {}
    for d in detectors:
        single = sweep_threshold(scores, d, thresholds_per[d],
                                  must_detect, marginal, must_reject)
        chosen[d] = single["threshold"]
    # Refine 2 passes.
    for _ in range(2):
        for d in detectors:
            best = {"f1": -1.0}
            for thr in thresholds_per[d]:
                test = dict(chosen)
                test[d] = thr
                decisions = {
                    rf: all(scores[rf][d2] >= test[d2] for d2 in detectors)
                    for rf in scores
                }
                ev = evaluate(decisions, must_detect, marginal, must_reject)
                if ev["f1"] > best.get("f1", -1.0):
                    best = {**ev, "thresholds": dict(test)}
            chosen = best.get("thresholds", chosen)
    decisions = {
        rf: all(scores[rf][d] >= chosen[d] for d in detectors)
        for rf in scores
    }
    ev = evaluate(decisions, must_detect, marginal, must_reject)
    return {**ev, "detectors": detectors, "thresholds": chosen}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-combo-size", type=int, default=3)
    ap.add_argument("--detectors", default=None,
                     help="Comma-list of detectors to consider (default all)")
    args = ap.parse_args()

    if not (FIXTURE_DIR / "manifest.json").exists():
        print("[harness] no fixtures found. Run capture_fixtures.py first.",
              file=sys.stderr)
        return 1
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text())
    must_detect, marginal, must_reject = load_ground_truth()
    rfs = sorted({int(k) for k in manifest["captures"]})

    if args.detectors:
        names = [n.strip() for n in args.detectors.split(",") if n.strip()]
        names = [n for n in names if n in DETECTORS]
    else:
        names = list(DETECTORS.keys())

    # Pre-compute every detector score for every RF (one pass).
    print(f"[harness] running {len(names)} detector(s) over "
          f"{len(rfs)} fixtures...")
    scores, total_time = run_detectors_on_all(manifest, rfs, names)
    if not scores:
        print("[harness] no fixtures could be loaded; aborting.",
              file=sys.stderr)
        return 1
    print(f"[harness] all detector scores computed in {total_time:.2f}s "
          f"({total_time / max(1, len(rfs)) * 1000:.0f} ms/channel)")

    # Threshold candidates per detector — chosen wide enough to bracket
    # both empty and locked channels at this site.
    thresholds = {
        "pilot_snr":       np.linspace(0,  80, 41).tolist(),
        "pilot_sharpness": np.linspace(0,  35, 36).tolist(),
        "vsb_asymmetry":   np.linspace(-3, 12, 31).tolist(),
        "pn511_corr":      np.linspace(0,  30, 31).tolist(),
        "spectral_mask":   np.linspace(-0.2, 1.0, 31).tolist(),
        "field_autocorr":  np.linspace(-30, 0,  31).tolist(),
    }

    # Per-detector best.
    print()
    print("─" * 72)
    print("Per-detector best (single-feature gate)")
    print("─" * 72)
    print(f"{'detector':<18} {'thr':>8}  {'tp':>3} {'fn':>3} {'fp':>3} "
          f"{'mgnl':>4}  {'prec':>5} {'rec':>5} {'f1':>5}")
    rows = []
    for d in names:
        best = sweep_threshold(scores, d, thresholds.get(d, []),
                                must_detect, marginal, must_reject)
        rows.append((d, best))
        print(f"{d:<18} {best['threshold']:>8.2f}  "
              f"{best['tp']:>3} {best['fn']:>3} {best['fp']:>3} "
              f"{best['marginal_hits']:>4}  "
              f"{best['precision']:>5.2f} {best['recall']:>5.2f} "
              f"{best['f1']:>5.2f}")

    # AND-combos up to max_combo_size.
    print()
    print("─" * 72)
    print(f"AND-combos (up to size {args.max_combo_size}, sorted by F1)")
    print("─" * 72)
    print(f"{'detectors':<55}  {'tp':>3} {'fn':>3} {'fp':>3} "
          f"{'mgnl':>4}  {'f1':>5}")
    combo_rows = []
    for size in range(1, args.max_combo_size + 1):
        for combo in itertools.combinations(names, size):
            res = sweep_combo(scores, combo, thresholds,
                               must_detect, marginal, must_reject)
            combo_rows.append(res)
    combo_rows.sort(key=lambda r: -r["f1"])
    for r in combo_rows[:20]:
        det_str = "+".join(r["detectors"])
        print(f"{det_str:<55}  {r['tp']:>3} {r['fn']:>3} {r['fp']:>3} "
              f"{r['marginal_hits']:>4}  {r['f1']:>5.2f}")

    print()
    print("Top recipe thresholds:")
    top = combo_rows[0]
    for d in top["detectors"]:
        print(f"  {d:<18} >= {top['thresholds'][d]:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
