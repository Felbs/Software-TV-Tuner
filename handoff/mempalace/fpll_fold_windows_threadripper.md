---
name: fpll-fold-windows-threadripper
description: "STVT_FPLL_FOLD=1 on the Windows Threadripper 3970X cuts replay CPU by ~42% (vs Ubuntu's ~6%) with bit-identical TS output and +2.2% wall. The capability is built, installed, A/B-verified, and on origin/windows as of 2026-06-13 — but env-gated (default OFF) since live throughput is paced by the SDR anyway."
metadata:
  node_type: memory
  type: project
  originSessionId: ubuntu-pi-port-2026-06-13
---

2026-06-13 — three commits cherry-picked from `linux-port-stvt-v3` onto
`windows` and pushed to origin (197c0f4..da25b7e):
- `034849f` STVT_FPLL_FOLD C++ (atsc_fpll_tight_impl folds dc_blocker+agc)
- `c6d8372` STVT_MIN_BUF / STVT_MIN_BUF_BYTES knob
- `da25b7e` cs16 IQ capture format

**Deterministic A/B measured on Windows Threadripper 3970X** (4 runs each,
warm cache, 30s RF34 capture via `tools/data/ab_test/ab_rf34_30s.cf32`):

| Mode    | Wall avg | CPU avg     | Cores avg | TS md5      |
|---------|----------|-------------|-----------|-------------|
| FOLD=0  | 28.61 s  | 41.33 CPU-s | 1.44      | ee62f46f... |
| FOLD=1  | 29.24 s  | 23.92 CPU-s | 0.82      | ee62f46f... (identical) |

→ **-42% CPU work, +2.2% wall, bit-identical TS** across all 4 A/B pairs.

The CPU saving on Threadripper is ~7x larger than Ubuntu's ~6% (see
[[x86-cpu-savings-ceiling]]). Most likely explanations: Zen 2 cores + the
GR scheduler thread/context-switch overhead for dc_blocker+agc weighs heavier
relative to their tiny per-sample work on a fast-core box. The wall-clock
penalty (+2.2%) is real but **irrelevant for live operation** — live is
paced by the SDR at 1x real-time, the chain just waits for samples either
way. Only flat-out offline replay sees the throughput cost.

**Build/install state on this box (as of push time):**
- `gnuradio-atscplus.dll` rebuilt with `_rebuild.bat`
- `cmake --install` + xcopy ran (per [[gr-atscplus-build-install-gotcha]])
- Smoke-test: `from gnuradio import atscplus; b = atscplus.atsc_fpll_tight(8e6)`
  with `STVT_FPLL_FOLD=1` logs `[fpll_tight] FOLD active: dc_blocker(D=32) +
  agc(rate=1e-06 ref=4) in-loop` — fresh code is loaded.

**Default still OFF.** The capability ships but doesn't change live default
behavior. Set `STVT_FPLL_FOLD=1` in your stvt_run.sh / shell env to enable.
The Ubuntu rig defaults it ON via `stvt_run.sh` (commit 75b7a1f on
linux-port-stvt-v3) but Windows didn't get that default-flip — left as
opt-in because the Threadripper has so much CPU headroom that the change
isn't urgent, and leaves the door open for a future
`stvt_run.bat` / setting toggle to make the call.

**A/B harness left in tree** at `tools/data/ab_test/`:
- `ab_rf34_30s.cf32` (1.92 GB capture)
- `timed_replay.py` (psutil-based wall+CPU wrapper for tv_replay)
- `out_*.ts` / `log_*.log` / `stderr_*.log` from the runs

If you re-measure later (e.g. after another C++ change to atsc_fpll_tight),
delete the `out_*` files and run `timed_replay.py 0 fold0_N` /
`timed_replay.py 1 fold1_N` from a shell with SDRplay PATH set.

How to apply: when user asks "should I run FPLL_FOLD on Windows," the answer
is **yes, it's free** — set the env var. When proposing future CPU-reduction
work, this is the only confirmed software win; the bigger lever is hardware
(HDHomeRun) per [[x86-cpu-savings-ceiling]].

**Follow-up A/B 2026-06-13 23:30 — BLOCK_NCO=1 is a Windows REGRESSION.**
Same harness, FOLD=1 always, varying STVT_FPLL_BLOCK_NCO (Ubuntu's next
lever, +6-8% there): Windows result was **+35% CPU** (25.65 → 34.49 CPU-s
avg over 4 runs), output non-deterministic (md5 differs run-to-run). Cherry-
pick reverted. The per-hardware-CPU divergence (Windows wins on FOLD, loses
on BLOCK_NCO; Ubuntu opposite) means future CPU-saving cherry-picks should
always be A/B'd before pushing to the Windows branch — see
[[x86-cpu-savings-ceiling]] for the updated framing.

Related: [[x86-cpu-savings-ceiling]], [[pi5-to-x86-backport]],
[[gr-atscplus-build-install-gotcha]], [[no-push-linux-without-test]] (this
push *was* live-A/B'd, satisfying the per-branch verification rule).
