"""Diagnostic dump: per-channel raw scores from every detector + per-detector
CPU cost. Useful for sanity-checking the detector layer before combining."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from detectors import DETECTORS  # noqa: E402

FIXTURE_DIR = HERE / "fixtures"
GROUND_TRUTH = HERE / "ground_truth_dc.json"


def main():
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text())
    g = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    sr = manifest["sample_rate_hz"]

    label = {}
    for ch in g["channels"]:
        label[ch["rf"]] = ch["expected"]
    for rf in g["expected_empty_rfs"]:
        label.setdefault(rf, "empty")

    rfs = sorted({int(k) for k in manifest["captures"]})

    # Header
    names = list(DETECTORS.keys())
    print(f"{'rf':>3} {'kind':<16} " + " ".join(f"{n:>10}" for n in names))

    # Per-detector timing accumulator
    timing = {n: 0.0 for n in names}
    counts = 0

    for rf in rfs:
        info = manifest["captures"][str(rf)]
        path = FIXTURE_DIR / info["file"]
        samples = np.fromfile(path, dtype=np.complex64)
        row_scores = {}
        for name, fn in DETECTORS.items():
            t0 = time.time()
            res = fn(samples, sr)
            timing[name] += time.time() - t0
            row_scores[name] = res.get("score", float("-inf"))
        counts += 1
        kind = label.get(rf, "unknown")
        print(f"{rf:>3} {kind:<16} " +
              " ".join(f"{row_scores[n]:>10.2f}" for n in names))

    print()
    print("Per-detector CPU cost (ms/channel):")
    for n in names:
        print(f"  {n:<18} {timing[n] / counts * 1000:.1f}")
    print(f"  TOTAL              {sum(timing.values()) / counts * 1000:.1f}")


if __name__ == "__main__":
    main()
