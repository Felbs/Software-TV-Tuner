---
name: linux-chain-stability-recipe
description: "What finally got the Ubuntu chain + video to run cleanly — fused-filter gain + patient drought handling + CPU isolation + live.ts rotation fixes. Reported working by user 2026-06-07 on `linux-port-stvt-v3`."
metadata: 
  node_type: memory
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

After weeks of chain fragility on the Ubuntu rig, the user reported "the chain and video plays great now" on 2026-06-07. The fix wasn't one change — it was a stack of related ones landed across 12 commits on `linux-port-stvt-v3` between 733e54a and 39b4301. The headline cure was the fused filter; the rest stopped good chains from being killed by spurious restarts and exposed real failure modes that were hiding inside "alive but useless" processes.

## The DSP fix (the actual cure)
- `STVT_RXF_FUSED` — fuse resampler + matched-filter into one fixed-ratio stage (commit 46042a1)
- `STVT_RXF_FUSED_GAIN` — amplitude fix for the fused filter (commit ec9ba19, message: "cures the fragility")

These two together are what the user identified as the source of long-standing fragility. Any future chain tuning on Linux should start from this config, not the old two-stage pipeline.

## Restart-loop hygiene (stopped killing healthy chains)
- `stvt_run` single-instance lock + close lock fd in spawned chain/player (don't leak)
- Confirm droughts before restarting — patient drought handling lets the chain self-heal
- `fs_checker`: re-acquire after lock-loss to recover from OsO droughts (with telemetry)
- Reset restart budget after a clean stretch (don't give up overnight)
- Kill the spawned chain on SIGTERM (orphan fix)

Pattern: every restart of a chain that was about to self-heal is a regression in uptime, not a save. Default to patience.

## New failure modes detected
- **Wedged chain**: process alive, live.ts not growing. Detect via file-size delta, not process liveness.
- **Live.ts rotation bug**: tv_live was truncating the sink behind ffmpeg's back. Fix is to *reopen* the sink, not truncate in-place.
- **ffmpeg mid-packet start on tail -c**: when feeding `stvt_play_hd` via `tail -c`, force `-f mpegts` so ffmpeg doesn't try to auto-detect from a partial TS packet.

## CPU isolation
- `stvt_cpu_isolate.sh` (#2) protects chain cores from preemption
- `STVT_CPU_ISOLATE` born-pins chain/player at spawn, persists across restarts (don't re-pin after the fact)

## Tooling
- `stvt_drought_forensics.sh` — capture a real drought without auto-restart kicking in. Use this when investigating any future "the chain died" report instead of guessing.

## How to apply

When the user reports chain instability on Linux, check whether they're on the fused-filter config FIRST (`STVT_RXF_FUSED` + GAIN). If yes and droughts are still happening, look at restart-loop aggressiveness before assuming a DSP problem — most of the "chain died" reports turned out to be `stvt_run` killing healthy chains.

When porting any of this to Windows (`windows` branch) or WSL (`wsl-port-stvt-v2`), the DSP fusion is portable; the restart/isolation scripts are Linux-specific (cpu pinning, posix locks, SIGTERM handling differ on Windows).

## Windows port status (2026-06-07, commit 197c0f4 on `windows`)

Ported and live-RF tested on RF34 (WRC NBC HD), pushed to origin/windows:
- `tv_live.py` — fused-filter code path + live.ts rotation fix
- `gr-atscplus/lib/atsc_fs_checker_inst_impl.cc` — re-acquire after lock-loss
- `tools/stvt_multirec.py` — SIGTERM orphan-cleanup (Windows no-op via try/except)
- `tools/sdr_sweep.py` — STVT_SOAPY_ARGS / STVT_STREAM_ARGS

Verified findings on Windows:
- Baseline chain (no fused): healthy. FPLL pinned at -2.69 MHz, in_rms 45.7, 24 Mbps TS.
- fs_checker re-acquire patch: silent on healthy decode. A/B'd CPU 77.0% (without) vs 75.0% (with), within sample noise. Patch is harmless to ship; provides safety net if a drought ever hits Windows.
- **FUSED filter default gain does NOT transfer from Ubuntu to Windows.** With `STVT_RXF_FUSED=1` and default `STVT_RXF_FUSED_GAIN=1.9`, Windows in_rms drops 45→9.7 and FPLL never locks. The gain default was tuned on Ubuntu's SDR+antenna+ifgr stack. Per-site `STVT_RXF_FUSED_GAIN` tuning is required before fused mode is usable on Windows. Estimate: 8-10 would compensate the ~5x amplitude shortfall, but not yet verified.

So the "DSP fusion is portable" claim above is half-true: the code is portable; the gain constant is site-specific and must be re-tuned per setup. Same lesson likely applies for the WSL port.

Related: [[dvr-architecture]], [[scanner-chain-env-gotcha]], [[sdr-single-tuner-rule]], [[ubuntu-rig-workflow]], [[gr-atscplus-build-install-gotcha]].
