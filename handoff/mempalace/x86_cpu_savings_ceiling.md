---
name: x86-cpu-savings-ceiling
description: "Software CPU savings on the live ATSC chain are HARDWARE-SPECIFIC, not a universal x86 ceiling. FPLL_FOLD wins everywhere (Windows -42%, Ubuntu -6%); BLOCK_NCO wins on Ubuntu (-6 to -8%) but REGRESSES on Windows Threadripper (+35%). Real ~5x cut still needs HW-demod tuner (HDHomeRun). MEASURED 2026-06-13."
metadata:
  node_type: memory
  type: project
  originSessionId: ubuntu-mempalace-import-2026-06-13
---

**UPDATE 2026-06-13 23:30** — the "ceiling" framing in the earlier write-up
below was wrong. Ubuntu's Claude found ANOTHER ~6-8% on top of FPLL_FOLD via
`STVT_FPLL_BLOCK_NCO` (commit 3ef9b31 on linux-port-stvt-v3) — block-rate
NCO recurrence replaces per-sample sincosf. So there is no universal x86
ceiling; there's a **per-hardware** ceiling, and the levers cross-validate
differently per box. Cherry-picked + A/B'd on Windows Threadripper 3970X:
**+35% CPU REGRESSION** (opposite sign — likely Zen 2 sincos is fast enough
that the recurrence overhead exceeds the savings, plus 64-thread cache
behavior). NOT pushed to origin/windows; cherry-pick reverted. Capability
exists in linux-port-stvt-v3 for the 6-core 1600X and presumably for slower
boxes generally.

**Updated take:** the SOFTWARE-CPU question on x86 is now "which combination
of FPLL_FOLD + BLOCK_NCO + (future levers) wins on YOUR specific CPU." Don't
assume Ubuntu wins port to Windows or vice versa — measure with the A/B
harness in `tools/data/ab_test/`. The HARDWARE answer (HDHomeRun) still
trumps everything for ~5x savings.

---

(Below: original 2026-06-13 import from Ubuntu wing — leaving as historical
record. The "no massive software win" claim is hardware-specific to the
Ryzen 1600X.)


This is the **end-state of the CPU-reduction question** on x86. Origin: Ubuntu
session 2026-06-13 measured this exhaustively on the Ryzen 1600X via
deterministic tv_replay A/B + per-block /proc profiling, after first sweeping
every lever on the Pi 4 ([[pi-rig]]'s `pi4_arm_lever_sweep`). The findings
generalise to any x86 box (Threadripper 3970X / N100 / etc.) — the chain
physics is the same.

**Per-block CPU profile (3s replay window):** pfb_arb_resampler 322 (hottest)
· fpll 224 · equalizer 209 · TEIScrub 203 · viterbi 173 · sync 162 ·
rational_resampler 152 · dc_blocker 110 · rs 76 · agc 40. The
**resampling front-end dominates**, not the equalizer.

**Lever A/B (metric = CPU-seconds per 30s TV):**
- **STVT_FPLL_FOLD=1: -6% CPU work, bit-identical TS.** ← THE only safe win.
  Folds dc_blocker+agc into the fpll thread (~½ a core cheaper) at the cost of
  ~7% wall-clock throughput (loses parallelism). On a 64-thread Threadripper
  the throughput cost is meaningless, so this is essentially free here.
- STVT_SPS=1.0: -5% but removes the equalizer's fractional-timing margin →
  risky on marginal RF. Default 1.1 is a deliberate margin; do NOT default.
- STVT_RXF_FUSED=1: neutral on x86 Ryzen (lower instantaneous CPU%, same total
  work, longer wall). Was a Pi win where the arb resampler was disproportionately
  costly.
- STVT_EQ_S16=1: no-op on x86. The int16 NEON equalizer is ARM-only and was
  Phase-4 skipped from the x86 cmake. Even if built, the equalizer isn't the
  LIVE gate (front-end-bound), so it wouldn't help live throughput.
- STVT_TEISCRUB=0: looks -18% but DESTROYS output (clears RS error flags). Off
  the table.

**Why no massive win:** the live throughput gate is the fpll/resampler
**FRONT-END** (sequential feedback loop at the 8 MS/s input rate) —
unparallelizable. OpenMP equalizer fan-out was tried on the Pi: monotonically
WORSE (fork/join 13k×/s at segment granularity). And not cheaply int16-able.
~5 cores of sequential DSP is what software 8-VSB demod costs, full stop.

**The ONE genuine ~5x CPU cut:** a **hardware-demod ATSC tuner**
(HDHomeRun / networked HW tuner). It does 8-VSB in silicon and streams clean
MPEG-TS — the entire ~540% software decode vanishes, mpv plays the TS in ~1
core. ~$100 hardware, not code.

**How to apply:** when the user asks "can we reduce CPU on the chain," cite
this memory + offer the FPLL_FOLD opt-in as the only software win, and
recommend HDHomeRun as the real ~5x cut. Don't promise software wins that
don't exist — Fable-era + Opus measurements both converged on this ceiling.

Related: [[pi5-to-x86-backport]] (FPLL_FOLD landed on linux v3 and Windows),
[[pi-rig]] (the Pi 4 ARM lever sweep that ruled out the parallel paths),
[[linux-chain-stability-recipe]] (Linux-specific tuning).
