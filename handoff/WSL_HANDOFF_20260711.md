# WSL Port Handoff — 2026-07-11

*You are (probably) a Claude session running in WSL, tasked with the
Linux port. This folder is the Windows session's memory palace,
shipped whole. Read `mempalace/MEMORY.md` first — it is the index;
every entry links a topic file in the same folder.*

## Your mission (user's roadmap, in order)
1. **WSL = build + replay validation ONLY.** WSL2's USB passthrough
   loses ~1.8% of samples — Reed-Solomon cannot survive it; live TV in
   WSL is impossible and this is documented. Your deliverables:
   gr-atscplus builds clean on Linux; `tools/tv_replay.py` decodes the
   specimen library **bit-comparably** to Windows (same bad-packet
   counts within the rail's ±0.14pp noise floor, same frame counts);
   the panel + time_knob + antenna_id import and their selftests pass.
2. Then the human moves it to the **Ubuntu box** (branch `main-linux`,
   synced via the Windows relay — see mempalace) for the LIVE test.
   LAW: never push to a Linux port branch without a live test on that
   branch's own hardware.
3. Then the **Pi** (`pi-port-stvt`), where the FEATURE GOVERNOR must
   be born: start every machine conservative, measure OsO overflow
   headroom, enable turbo/SOVA/etc. only as the hardware proves it can
   afford them. On the Threadripper everything fits; on a Pi it won't.

## The five laws you must not relearn the hard way
1. **Replay can NEVER validate a live promotion** — replay has no
   real-time deadline. Overflow-gate (OsO==0) every live A/B.
2. **Every per-failure rescue path needs a failure-RATE gate** or it
   self-amplifies exactly when the channel is worst (turbo stampede).
3. **Pixels are a measurement layer** TS metrics can't see; the
   player/extractor pipeline can fake or destroy quality on its own
   (datamosh; ghost SAP streams).
4. **Never read live.ts aggressively** — the panel's own pollers once
   caused the very overflows we were hunting (~48 MB reads). Sparse,
   chunked, cached.
5. **The user's eyes outrank every instrument.** Six confirmed times.

## Specimen library (the replay ground truth)
Z:\src\adaptive-tv\lab\captures\ + tools/data/specimens — in WSL:
/mnt/z/src/adaptive-tv/lab/captures. Reference numbers for
philips_rf36_strong, fox_impulse_storm etc. are in
mempalace/speed_and_oracle_71011.md and lab reports beside them.

## Windows-specific things that will differ on Linux
- Build: `_rebuild.bat` → cmake incremental; the DOES-NOT-INSTALL
  gotcha is Windows-flavored but verify `cmake --install` freshness on
  Linux too (import-stale-module class).
- PowerShell-kill law and cp1252 print traps are Windows-only; Linux
  gets its own: the pgrep self-match trap (documented) bites BOTH.
- Paths in panel/tools are Windows-hardcoded in places (PY constant,
  SDRplay PATH injection, LIVE path) — port with a platform layer,
  don't fork.

Good luck. The Windows rig is the reference implementation — when in
doubt, replay the same specimen on both and compare numbers, not
vibes.
