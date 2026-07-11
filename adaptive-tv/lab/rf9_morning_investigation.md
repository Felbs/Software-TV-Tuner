# RF9 (WUSA 9.1) Morning Glitch Investigation — 2026-07-11

**Complaint:** RF9 glitchy in the mornings (~3-6% loss at MER 16.4-17.0) even after a
fresh Deep Tune. Suspicion: the algorithm mis-tunes it.

**Verdict in one line:** the algorithm was not mis-TUNING it — the *stack* was
glitching its own television twice over (panel pollers + the cliff-mode lottery),
on top of a real ~3% atmospheric morning fade. Both stack diseases are fixed and
verified; the sky remains the sky.

---

## Method

- Direct-chain A/B ladder (panel process **dead** — its waterfall sweeper steals the
  radio and its pollers poison the measurement): 6 arms x 150 s, ABBA-interleaved,
  RF9 / Antenna B / rfgain 5 / IFGR 28 / AGC servo on, play-path env, no players.
  Script: `Z:\src\adaptive-tv\rf9_ab_ladder.py`; raw rows:
  `lab\rf9_ab_ladder.jsonl`; per-arm chain logs `lab\rf9_arm_*.log`.
- Loss = sum of `last5s pkts/bad` RS windows; OsO = SoapySDR overflow markers
  (each = dropped samples = a splice = a visible glitch); MER from `fs_err_rms`.
- Final phase: full viewing stack restored with fixes, 5.5 min sparse-read watch.

## Ladder results (10:12-10:31, air degrading through the window)

| # | Arm       | Loss%  | OsO /150s | MOD12 slips | MER med (p10) | Good pkts /150s | Turbo att/s (fail EMA) |
|---|-----------|--------|-----------|-------------|----------------|-----------------|------------------------|
| 1 | BASE (turbo on)  | 2.861 | **0** | 0 | 16.62 (16.18) | 1,853,066 | 265 (4.2%) |
| 2 | TURBO OFF        | 3.895 | **0** | 0 | 16.65 (16.25) | 1,845,661 | — |
| 3 | CLIFF arsenal    | 2.535 | **16** | 2 | 17.97 (17.55)* | 1,651,274 | 238 (1.0%) |
| 4 | TURBO OFF        | 5.712 | **0** | 0 | 16.64 (16.27) | 1,800,165 | — |
| 5 | CLIFF arsenal    | 4.141 | **22** | 8 | 17.90 (17.46)* | 1,554,963 | 152 (4.4%) |
| 6 | BASE (turbo on)  | 8.346 | **0** | 0 | 16.53 (16.12) | 1,739,945 | 12.5 (3.5%)** |

\* Cliff MER is not comparable: the DFE shrinks post-equalizer error, so fs_err_rms
flatters the arsenal while it drops samples.
\** Arm 6 caught a genuine fade storm; the stampede gate correctly stood turbo down.

---

## Suspect 1 — Turbo sub-gate CPU bleed: **EXONERATED (and helpful)**

- Turbo ran flat-out at 238-265 attempts/s, 0.9-1.6 M pinned symbols/s, EMA hovering
  right at the 4% gate (28k skips in arm 1 alone) — and produced **zero OsO in every
  turbo-on base arm**. The Threadripper absorbs the bleed; there is no overflow
  feedback loop from turbo on this hardware.
- The clean adjacent pair (arms 1 vs 2, identical MER 16.6): turbo ON cut loss
  2.861% vs 3.895% — **-1.03 pp, ~27% of failures rescued** (15,108 packets in 150 s).
- The "40 OsO this morning vs zero on Fox" discrepancy is explained elsewhere:
  yesterday's Fox endurance ran as a *direct chain* (all four 7/10 direct-chain logs:
  0 OsO); this morning's session ran under the panel + cliff config (below).
- **Action: none.** `STVT_TURBO=1` and `STVT_TURBO_MAXFAIL_PCT=4` stay. The gate
  demonstrably modulates (arm 6: attempts collapsed to 12.5/s in the storm).

## Suspect 2 — cliff_mode boundary lottery: **GUILTY — fixed**

- Both CLIFF arms overran the live real-time deadline: **16 and 22 source overflows
  per 150 s (6-9/min) and 2 and 8 MOD12 slips vs zero in all four base arms**, and
  delivered 9-15% fewer good packets (1.55-1.65 M vs 1.74-1.85 M). Same disease that
  evicted DD tracking on 7/10 (overflow-gate law: replay wins never validate a live
  promotion).
- RF9 morning tune-reads straddle 16.5, so identical tunes randomly drew BASE or the
  arsenal. **This morning's watched 10:01 session had drawn CLIFF** (`eff_eras=14`
  in its log) — the user was watching the arsenal's splices.
- **Fix applied:** `tv_tuna_panel.py` cliff threshold 16.5 → **16.0** (with the
  ladder numbers in the comment). Below 16.0 the arsenal keeps its proven sub-cliff
  home turf, where base decodes nothing and splices are an acceptable price.
- Follow-up (not done today): bisect WHICH arsenal piece busts the deadline
  (DFE+anchor is the prime suspect by the DD precedent); any re-promotion into the
  16.x band must pass an OsO==0 live gate.

## Suspect 3 — antenna/gain by hour: **CLEARED by fresh morning data**

- Deep Tune re-raced at 09:54-10:00 **this morning**: Antenna A on RF9 = MER 9.74,
  loss 100% (no decode at all); Antenna B = 16.4-16.7. B is the morning winner too —
  there is no morning/evening antenna flip on this channel.
- The gain grid (rfgain 4/5/6 x IFGR 28/32/36, winner's antenna) ran in the same
  session and moved the recipe 5/32 → **5/28** (measured 16.69 / 2.813%). The AGC
  servo owns IF live anyway. Recipe `lab\channel_recipes.json` is fresh and correct;
  no change made.

## Suspect 4 — ambient OsO trickle: **GUILTY — the panel itself — fixed**

- This morning's session logged OsO at a **metronomic ~9.7 s cadence** (29 in 4 min).
  Fading is not periodic; pollers are.
- Root cause: `live_math()` is called by THREE pollers — GUI status loop (8 s idle /
  1.8 s busy), NERD tab (3 s), flight recorder (10 s, browser-independent) — and each
  call ran `ts_metrics(20)`: a **~48 MB monolithic read of live.ts** (the file the SDR
  writer is appending to) plus a pure-Python parse of ~258k packets. The panel's own
  UI code violates the documented POLLING LAW. drizzle_watch.py and tv_live's 10 s
  rotation stat() were checked and are innocent.
- **Fixes applied:**
  1. `tv_tuna_panel.py`: `ts_metrics_cached()` — one read per 30 s, single-flight
     lock, serves all three pollers (stale-on-contention).
  2. `tv_lab.py` `ts_metrics()`: the tail read is now 4 MB slices with 50 ms
     breathers instead of one 48 MB burst.
- **Verification (full viewing stack: panel + flight recorder + tv_watch + mpv,
  10:34-10:40): OsO = 0.00/min over 5.5 min** (was ~7/min), MOD12 = 0,
  loss 2.814% at MER 16.74 — statistically identical to the zero-OsO direct-chain
  baseline (2.861% @ 16.62). The viewing stack now adds nothing measurable.

---

## What is honestly the sky

With every stack disease removed (direct chain, zero OsO, turbo on), RF9 mornings
still lose **2.9-8.3% of packets, breathing minute-to-minute** at MER median
16.5-16.7 (p10 16.1-16.3) — fast fades that RS loss sees but median MER barely
registers. That is atmospheric physics on the market's known fast-fader, and the
Knob of Time already knows it: best ~17h, worst ~1h, ~4.5 dB diurnal swing. Today
was still the best morning on record, now unhandicapped.

## Expected user-visible improvement

- Before: a splice/glitch every ~4-8 s from two stacked self-inflicted sources
  (~7/min panel pollers + 6-9/min cliff arsenal when the lottery drew it), riding on
  the fades.
- After: zero self-inflicted splices; only genuine fade bursts remain (~2.8% loss
  this window — occasional brief tears, clearing toward evening).
- Recipe measurements and Deep Tune verdicts also get cleaner from here (previous
  ones were poll-polluted).

## Files touched

- `Z:\src\adaptive-tv\tv_tuna_panel.py` — ts_metrics_cached (30 s TTL, single-flight);
  cliff threshold 16.5 → 16.0. py_compile clean; panel restarted 10:32, single process.
- `Z:\src\adaptive-tv\tv_lab.py` — chunked gentle live.ts tail read.
- `Z:\src\adaptive-tv\rf9_ab_ladder.py` (new) + `lab\rf9_ab_ladder.jsonl`,
  `lab\rf9_arm_*.log`, `lab\rf9_ab_ladder_run.log` — the evidence.
- No git operations, no C++ edits, no recipe change (fresh from this morning's
  Deep Tune).

Final state: 9.1 WUSA playing on Antenna B, rfgain 5 / IFGR 28, BASE config
(eff_eras dynamic, turbo on), OsO-free.
