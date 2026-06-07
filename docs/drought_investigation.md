# Noise-drought investigation & fix (2026-06-06/07)

## Symptom
On this box the live chain periodically slipped into a **noise drought**: `live.ts`
keeps growing with valid 0x47 sync bytes but the TS carries hundreds–thousands of
random PIDs (a real DC mux is ~25–35), so the picture is garbage. Previously this
either persisted until the watchdog (`stvt_run.sh`) restarted the chain (~30 s blip)
or, unguarded, never recovered.

## Method
`tools/stvt_drought_forensics.sh` runs the chain **unguarded** (no auto-restart) so a
drought persists for inspection, logging unique-PID count + the `[fpll]` carrier line
every 12 s. Combined with two gated telemetry sources:
- `STVT_EQ_TELEM=1` → equalizer `fs_err_rms` / `|taps|` per 8 field syncs
- `ATSCPLUS_FS_TELEM=1` → field-sync gap anomalies, rejects, field-loss, re-acquire

## What it is NOT (each ruled out with live telemetry)
- **Not the carrier.** FPLL stays locked through and well past a drought: `in_rms` ~33,
  `max|x|` ~0.20, NCO steady. (The `max|x|=1.57` "clip" is the OsO discontinuity
  artifact, not RF clipping.)
- **Not the equalizer.** Through a drought the equalizer keeps adapting: `fs_err_rms`
  steady ~0.65, `|taps|` ~1.83, field-sync counter advancing with no gap, zero
  divergence/LKG events. (So an equalizer tap-reset would do nothing — tested.)

## Root cause (proven)
A **framing lockout in `atsc_fs_checker_inst`**:
1. An **OsO** (SDR sample overflow — the single-thread matched filter momentarily
   falls behind real-time → dropped samples) disrupts field-sync spacing.
2. Real field syncs then arrive at gap ≠ 313 segments (measured 179–277, i.e. *too
   short*). The 313-spacing validation (`tol_low=280`, added to reject *false* field
   syncs) **rejects these real ones**.
3. `d_fs_locked` was set `true` on lock and **never cleared during operation**, so the
   rejection was permanent. `segs_since_accepted` climbed into the thousands — one run
   hit **gap=12503 (~40 fields)** — and the output is noise that whole time = the
   drought. Every OsO in the log is immediately followed by reject/field-loss/gap
   anomalies; the correlation is 1:1.

## Fix
`atsc_fs_checker_inst_impl.cc`: if `FS_RELOCK_SEGS` (default **939** = 3 fields)
segments pass without accepting a field sync, the lock is stale — drop `d_fs_locked`
so the next real PN511 field sync re-acquires regardless of spacing (the cold-start
path). The equalizer stays healthy through droughts, so re-acquire locks onto a clean
field sync. **Never fires in normal operation** (gap never exceeds ~626) → never-worse.
Tunable: `ATSCPLUS_FS_RELOCK_SEGS` (0 disables). Committed: `7d79758`.

## Validation (overnight 2026-06-07, tough late-night RF/CPU)
- Mechanism: **max gap 12503 → 626** (bounded); RE-ACQUIRE fires at the threshold and
  recovers; **all droughts now brief (≤24 s) — zero permanent** (the key win).
- Uptime: **~90% unguarded** (vs near-0% unguarded baseline — droughts were permanent).
- Production (fix + watchdog): **90–92%** with all droughts brief.
- Patient watchdog (commit 18a2c67, `DROUGHT_GRACE_LOOPS=2`): restarts **8 → 5** over
  25 min; 3 droughts self-healed with NO restart; the 3 that restarted had each
  persisted ~40 s (the sustained-OsO case below — the in-chain re-acquire can't beat
  ongoing sample loss).

Caveat: uptime numbers are from ~midnight–2 a.m. with unusually frequent sustained OsO;
an earlier-evening baseline was ~97%. Sequential A/Bs share no common RF window, so
treat absolute uptime as indicative — the **mechanism** wins (no permanent droughts,
self-heal, bounded gap) are unambiguous.

## Production config (recommended, all default/committed)
- Re-acquire fix: ON (compiled in, `ATSCPLUS_FS_RELOCK_SEGS=939` default).
- `tol_low=280` (default; `=150` was tried and lost).
- Watchdog: `DROUGHT_GRACE_LOOPS=2` (default; lets the chain self-heal first).
- **Operational note:** never `rm` `/tmp/stvt_run.lock` — `flock` releases on process
  exit; deleting it while held lets a 2nd supervisor lock a fresh inode (dual-supervisor
  fight). Just `pkill -f stvt_run.sh` to stop.

## Tried and rejected
- **`ATSCPLUS_FS_TOL_LOW=150`** (accept the early-but-real field syncs directly):
  uptime *dropped* to ~40% even though REJECT-EARLY went to 0 — accepting mis-spaced
  syncs corrupts segment numbering more than rejecting them. Keep `tol_low=280`.
- **Equalizer quality-aware LKG reset** (`STVT_EQ_QUALITY_BAD_RMS`): can't help — the
  equalizer's error signal stays low during droughts (it's not the equalizer).

## Residual / open
A **sustained** OsO burst (the CPU can't keep up for tens of seconds) still causes a
multi-second drought: the re-acquire keeps re-locking but loses again while samples
keep dropping. The fix bounds and shortens these (and eliminates permanent lockout),
but eliminating them entirely is the **OsO-frequency / CPU-ceiling** problem (faster
single-thread CPU, leaner matched filter, bigger SDR ring buffer). The watchdog
remains the backstop for these.
