"""gauntlet_wiener.py — 20-min paired verdict: Wiener-seeded vs cold.

Prep: one 30 s CIR capture per cell -> wiener_seed solve -> seed dir.
Then quick_gauntlet cells run ABAB: A = cold start, B = warm-start from
the analytic seed (via the chain's existing STVT_EQ_TAP_CACHE loader).
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import overnight_cube as oc
from quick_gauntlet import CELLS, run_arm, fmt

HERE = Path(__file__).parent
LAB = HERE / "wiener_lab"
SEED = LAB / "gauntlet_seed"
SEED.mkdir(parents=True, exist_ok=True)
PY = sys.executable

t0 = time.time()
print("== PREP: fresh CIR + Wiener solve per cell ==", flush=True)
for name, rf, ant, rfg, ifgr in CELLS:
    cir = LAB / f"cir_g_{rf}.bin"
    os.environ["STVT_EQ_CIR"] = "1"
    os.environ["STVT_EQ_CIR_DUMP"] = str(cir)
    try:
        s = oc.sample(rf, ant, rfg, ifgr, secs=30)
    finally:
        os.environ.pop("STVT_EQ_CIR", None)
        os.environ.pop("STVT_EQ_CIR_DUMP", None)
    tap = SEED / f"taps_{ant.replace(' ', '')}_rf{rf}.bin"
    if cir.exists():
        r = subprocess.run([PY, str(HERE / "wiener_seed.py"),
                            "--cir", str(cir), "--taps-out", str(tap),
                            "--shift", "55", "--snr-db",
                            "16" if (s.get("mer_med") or 15) > 15 else "14"],
                           capture_output=True, text=True)
        ok = tap.exists()
        print(f"  {name}: MER {s.get('mer_med')} -> seed {'OK' if ok else 'FAILED'}",
              flush=True)
    else:
        print(f"  {name}: no CIR (no sync?) — cell will race cold-vs-cold",
              flush=True)

print("\n== GAUNTLET: A=cold  B=wiener-seeded ==", flush=True)
wins = {"A": 0, "B": 0, "tie": 0}
for name, rf, ant, rfg, ifgr in CELLS:
    print(f"== {name}: RF{rf} {ant} ==", flush=True)
    d_hdr, d_mer = [], []
    for r in range(2):
        sa = run_arm({}, rf, ant, rfg, ifgr)
        sb = run_arm({"STVT_EQ_TAP_CACHE": str(SEED)}, rf, ant, rfg, ifgr)
        print(f"  r{r+1} A: {fmt(sa)}")
        print(f"  r{r+1} B: {fmt(sb)}", flush=True)
        if sa.get("hdr") is not None and sb.get("hdr") is not None:
            d_hdr.append(sb["hdr"] - sa["hdr"])
        if sa.get("mer_med") and sb.get("mer_med"):
            d_mer.append(sb["mer_med"] - sa["mer_med"])
    mh = sum(d_hdr) / len(d_hdr) if d_hdr else 0
    mm = sum(d_mer) / len(d_mer) if d_mer else 0
    v = "B" if mh > 5 else ("A" if mh < -5 else "tie")
    wins[v] += 1
    print(f"  -> paired: hdr {mh:+.0f}/sample, MER {mm:+.2f} dB, verdict {v}")

print(f"\nWIENER GAUNTLET ({time.time()-t0:.0f}s): "
      f"B wins {wins['B']}, A wins {wins['A']}, ties {wins['tie']}")
