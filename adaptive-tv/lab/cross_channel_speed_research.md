# Cross-channel speed research — tower axis + predictive scan/surf
2026-07-11 · OFFLINE (radio untouched; bedtime_discone.py owned the SDR all
night) · analysis: `lab/tower_axis_analysis.py` · prototype:
`scan_planner.py` (imported by NOTHING) · builds on
`lab/antenna_fingerprint_research.md` and `time_knob_prior.py`.

Mission: use cross-channel knowledge — the antenna's carried-over
characteristics, including tower-to-tower — to make scanning and
channel-surfing faster.

---

## TASK A — does the A-vs-B advantage cluster by transmitter SITE?

Site membership is a **stated assumption** (market geography, not measured):
`cluster` = the dominant multi-station site; `site-NE` = RF21
(Baltimore-side MPT decoded here); `site-SW` = RF31. The analysis functions
in `tower_axis_analysis.py` are generic (`{rf: site}` map is an input);
the DC map lives only under `__main__`.

### T1 — the territory table IS site-shaped (merged labels, hour-balanced dB)

| RF | site (assumed) | A−B | per-day deltas | sign-stable |
|---|---|---|---|---|
| 15 | cluster | **−3.5** | −1.8, −2.0 | 2/2 |
| 21 | site-NE | **+3.6** | +2.4, +3.8, +4.8, +4.4 | 4/4 |
| 31 | site-SW | **+2.4** | +1.2 | 1/1 |
| 34 | cluster | **−1.8** | 0.0, −2.3, −2.4, −2.2, −2.5 | 4/5 |
| 36 | cluster | **−3.6** | −0.0, −4.3, −0.3, −1.9 | 4/4 |

The two off-cluster channels are **exactly** the two UHF channels rabbit
ears (A) win; every cluster UHF channel is the Philips' (B). Perfect sign
separation, and day-stable where multi-day data exists (RF21: 4/4 days).
Note the direction geometry: site-NE and site-SW are roughly *opposite*
bearings, so the story that fits is not "A points at Baltimore" but
"**B is aimed at the cluster; A is closer to omni and wins whatever is
off B's boresight**" — a per-antenna bearing-response fingerprint.

### T2 — honest statistics: suggestive, not proven

Exact permutation test (all C(5,2)=10 ways to pick which 2 channels are
"off-cluster"): observed off−on gap **+5.96 dB**, **p = 0.100** — the true
assignment is the single most extreme of 10 possible, and that is *as
significant as 5 channels can ever get*. Live-labels-only rerun: same
perfect separation, p = 0.25 (4 assignments). RF35 can't join (no solid B
cell); RF21's identity is contaminated by the DC/Baltimore co-channel pair.

### T3 — the killer confound: frequency window ≡ site

`freq_window_confound()` = TRUE: RF21 (515 MHz) and RF31 (575 MHz) are
**adjacent in the observed channel list**, so a single contiguous
~515–575 MHz ripple in B's response (or A's) reproduces the "site" split
exactly. On this channel set, **direction and mid-band frequency response
are mathematically indistinguishable**. No cluster channel exists between
515 and 575 MHz to break the tie.

### T4/T5 — dynamic co-movement gives NO support (yet)

If same-site channels share a propagation path, their MER should co-move.
- Sweep-to-sweep (6 A sweeps, 4 B sweeps, scan rows): same-site mean
  r = −0.01 vs cross-site +0.13 on A (perm p = 0.73); B: −0.18 vs −0.05
  (p = 1.0). **Negative.** (Underpowered: 4-6 points per correlation,
  0.1 dB quantization, healthy channels compressed into a 15-20 dB band.)
- Day-to-day (all 14k rows): A same-site +0.31 vs cross +0.05 (p = 0.2,
  right direction, tiny n); B the opposite. **Inconclusive.**

### Task A verdict

**The static advantage pattern is perfectly consistent with a direction
fingerprint (p=0.10, the best 5 channels can do), but it cannot be
distinguished from a 60-MHz frequency ripple on this channel set, and
dynamic co-movement shows nothing at current n.** What 2-3 sites can NEVER
settle: with both off-cluster channels frequency-adjacent, no statistical
test on stored MER separates site from spectrum. What WOULD settle it:
1. **Rotate B a few degrees** (daylight, 10 min): if cluster channels move
   together while RF21/RF31 move differently → direction. A frequency
   notch is rotation-invariant; a boresight is not. This is the cheapest
   decisive experiment and needs no new code.
2. **Tropo co-enhancement**: dawn enhancement is path-specific. If RF21
   (NE path) enhances on mornings when cluster channels don't (the discone
   RF7-at-dawn pattern), that's a path signature. Free — the Knob-of-Time
   history accumulates it; just keep sweeping at dawn.
3. More sweeps: the planner below keeps probing dead pairs, so the sweep
   co-movement matrix grows every night.

### Direction-fingerprint model for multi-tower markets (design, universal)

No city names, no coordinates: **towers are latent clusters the rig
discovers**. PSIP audit (real scan.json): TVCT gives identity only —
short_name, major.minor, source_id; EIT gives titles. **No location field
exists in PSIP/EIT**, and scan.json adds pilot metrics but nothing
geographic. (One usable identity nugget: channel-*sharing* — RF7's TS
carries both WJLA 7.x and WHUT 32.x, proving two *stations* on one
transmitter — but that's within one RF, never across RFs.) So grouping
must be **behavioral**:

- **Feature 1 — territory-sign vector**: per-RF vector of per-antenna
  advantages vs the market mean (T1's columns). Same-tower channels should
  share signs. Cheap, already computable.
- **Feature 2 — sweep co-movement** (T4 matrix): grows nightly.
- **Feature 3 — echo/CIR signature**: the equalizer's impulse response
  (panel's ECHO X-RAY already extracts it) is path geometry; same-tower
  channels seen by the same antenna should share delay-spread structure.
  Strongest proposed discriminator, not yet logged per scan — a future
  wire-in is "stamp top-3 echo delays into scan.json per locked channel".
- Model: `mer(rf, ant) ≈ regime_mean(ant, band) + cluster_offset(ant, T_k)
  + residual`, clusters `T_k` from agglomerative clustering over features
  1-3. The prize: the ±3.5 dB per-channel territory that *refused* to
  transfer channel-to-channel becomes learnable from **one channel per
  tower**.
- Honesty gates (all validated laws carried over): a cluster term
  activates only with ≥2 member channels × ≥2 days; leave-one-out gain
  must beat the plain regime mean before the term is trusted; label
  populations (legacy vs live antenna names) never merge.

---

## TASK B1 — scan_planner.py (prototype, unwired; selftest 16/16 green)

### What phase 2 actually costs (timing model from tv_tuner.py code paths)

| verdict | s/attempt | attempts today | today's total |
|---|---|---|---|
| locked | 26.0 | 1 | 26 s |
| MER floor (9 s early exit) | 13.5 | 2 | 27 s |
| no pilot (NCO wander) | 14.0 | 2 | 28 s |
| pilot, no field sync | 19.5 | 2 | 39 s |
| no live.ts growth | 29.0 | 3 | 87 s |
| weak signal / no lock | 27.0 | 3 | 81 s |

Validated against last night's wall clock: model says the discone sweep's
phase 2 = 299 s; the real one ran 02:00:34 (RF7 row) → 02:05:50 (save),
~316 s including PSIP tails — within ~10 %. The retry ladder is the tax:
**a dead channel costs 2-3× its verdict because the cold-start retry
re-proves physics the memory already knew.**

### Planner design (all in `scan_planner.py`, pure functions)

- **Tiering per (rf, antenna)**: real history cell (recency-weighted,
  hour-balanced) → `productive` (≥16.8) / `likely` / `doubtful` (<14.2);
  no cell → dead-verdict evidence (≥2 dead scan verdicts over ≥2 days,
  reset by any lock) → `doubtful`; else transfer prior; else `unknown`.
- **Ordering**: productive first (est desc) → guide usable fastest.
  Measured evidence for THIS pair always outranks transferred estimates
  (replay found a 20.8 dB *fallback-prior mirage* on discone UHF queueing
  ahead of RF7's real 16.2 dB history — fixed with src-rank ordering).
- **Dwell budgeting**: `doubtful` → PROBE = 1 attempt, retries 0; the
  existing early-verdict ladder is the probe (9-15 s). Everything else
  keeps the full ladder.
- **In-sweep streak rule**: ≥2 consecutive dead verdicts in one band
  regime on this antenna (no lock yet in that regime) demotes the
  regime's remaining `likely/unknown` channels to probes. This catches a
  regime-dead antenna on its FIRST sweep, before any history exists.
  Never demotes `productive` or FULL-LOOK-due entries.
- **No silent skips (project law)**: nothing is ever dropped from the
  plan. Every `doubtful` pair is promoted back to a FULL look every 5th
  sweep, or when its last full look is >7 days old, or implicitly when a
  probe locks (a lock resets dead evidence; a probe that locks costs the
  same as a full lock — verified in selftest).

### Offline replay — minutes saved per sweep per antenna

Replayed against the real saved sweeps (evidence honestly restricted to
what a wired planner would have known):

| antenna / sweep | phase-2 today | sweep #1 (cold) | steady state | time-to-80%-locks |
|---|---|---|---|---|
| **C discone** (7/11 02:06, real) | 299 s | 221 s (−78 s, streak rule) | **162 s (−136 s, −45 %)** | 26 s → 26 s |
| **B philips** (7/11 01:28, real) | 237 s | 237 s (−0 s) | 237 s (−0 s) | **211 s → 130 s (−38 %)** |
| **A rabbit** (modeled¹) | 210 s | 196 s | **~183 s (−27 s)** | 184 s → 130 s |

¹ No saved A-sweep JSON exists (scan.json keeps only the last two sweeps);
modeled from history: RF7/RF9 hot-but-MER-floor on A, 6 UHF locks.

Readings, honestly:
- **Discone: ~2.3 min saved per sweep (45 %)** — last night burned 4.6 min
  re-proving 7 UHF "pilot, no field sync" verdicts twice each. The prior
  CANNOT call these dead (carrier-without-data leaves no MER rows — the
  MER-transfer fallback actually predicts 20 dB there); **the dead-verdict
  log is the load-bearing evidence**, plus the streak rule for sweep #1.
- **Philips: 0 s saved and that is CORRECT** — every candidate on B is
  genuinely productive or near-cliff; a planner that found "savings" there
  would be eating real channels. The B win is ordering: 80 % of the locks
  land 81 s sooner because RF21 (3 × 27 s of weak-no-lock) runs last.
- The bedtime campaign ran 2 sweeps; a planner-wired night saves ~3 min of
  radio time per B+C pair, freeing dwell for the RF7 probes that actually
  cracked the discone.
- Convergence: verdict-dead needs 2 sweeps on 2 days by design (a single
  dud sweep — wedged SDR, mid-aim antenna — must not condemn a channel);
  the streak rule covers sweep #1.

### Gap found during replay (wire-in prerequisite)

`scan.json` records `antenna` **only on locked channels** — a sweep with
zero locks (scan_dud.json, the 6/20 discone scan) is unattributable, and
dead verdicts on a mixed night can't feed the per-antenna verdict log.
Fix is one line in `run_scan()`: stamp `"antenna": os.environ.get("STVT_ANTENNA")`
at the top level of the scan dict. This is wire-in step 1.

---

## TASK B2 — surf accelerator (design; latency budget: same-mux hop 3.1 s,
cross-mux ~9.5 s with persist-retune, first visit ~30 s)

**Lever 1 — seed pid_cache from the scan (the big one).** The first-visit
30 s is mostly full PSI discovery + the 12 s margin measurement. But the
scan already ffprobes every locked mux with `-show_streams` — it simply
*discards* the stream PIDs when building `programs` (tv_tuner.py
`ffprobe_programs()` keeps codecs only). Keep the `id` fields and write
`lab/pid_cache.json` entries for **every (rf, prog) in the market at scan
time** → every first visit becomes a warm visit. Budget effect: first
visit 30 s → ~15 s (memory-tune warm) or ~9.5 s with persist-retune; the
existing PSI-probe + 8 s watchdog self-heal already guards stale seeds.
Zero radio cost — it's data we throw away today.

**Lever 2 — prior pre-qualification of never-visited channels for the
quick margin check.** Rule: qualify iff `prior.mer_estimate −
expected_mae ≥ 15.2 (cliff) + 1.6 = 16.8` with solid confidence — then the
12 s margin stage runs as the 3 s quick check (still measured; still falls
back to the full 12 s below 16.8 — the shipped RF9 safeguard chain is
untouched). **Audit on the real history: this fires for ZERO (rf, ant)
pairs today** — best case is B-UHF 17.0 − 1.6 = 15.4 < 16.8. Honest
verdict: correct-by-construction, ~5 lines to wire, but it pays only when
an antenna's regime pool means ≥ 18.4 dB (the attic-classic class, or
after an antenna upgrade). Saves 9 s of the first-visit tax when it fires.

**Lever 3 — next-channel precompute.** The guide is ordered; while tuned
to entry *i*, precompute for *i±1* (zero radio touch): pid_cache
hit/miss, Knob-of-Time hour bin, prior estimate, recipe/GAINS consult,
DABNOTCH decision. Saves the ~0.5-1 s decision latency per hop and lets
the guide badge the *next* channel's expected quality before the user
commits. Combined with Lever 1, surf-next lands at the 3.1 s / 9.5 s
floors — the remaining tax is pure DSP acquisition, which is the
persistent-SDR-session architecture item, not a planning item.

**Surf ordering (territory/tower-aware).** Order the surf ring same-mux
first (3.1 s hops), then cross-mux by descending expected MER *for the
current antenna and hour*. Doubtful (rf, ant) pairs sort last but are
never removed (same no-silent-caps law; the guide shows "expect nothing
here on this antenna" from the verdict log instead of silently hiding).
If Task A's cluster model is ever validated, "next tower" becomes a
predictable antenna-switch prompt; until then this is just the planner's
ranking reused.

---

## Ranked wire-in plan for daylight (smallest risk first)

1. **Stamp the sweep antenna at scan.json top level** (1 line in
   `run_scan()`). Prerequisite for all verdict learning; zero behavior
   change.
2. **Keep stream PIDs in `ffprobe_programs()` and seed pid_cache at scan
   save** (small patch + one JSON write; the existing PSI-probe/watchdog
   self-heal already defends against stale seeds). Big surf win, no new
   decision logic on the tune path.
3. **Planner ordering only** (no dwell changes): sort phase-2 candidates
   by `scan_planner.plan()` tier/est. Pure reorder — total scan time
   identical, guide usable 38 % sooner on B-class sweeps. Easy A/B: order
   is visible in the scan log.
4. **Probe budgeting + streak rule** (`retries` honored by
   `scan_one_rf_with_retry`, streak demotion in the phase-2 loop): the
   2.3 min/sweep discone win. Slightly more invasive — A/B one night of
   bedtime sweeps (planner on/off alternating) and diff locked-channel
   sets; the FULL-LOOK promotions must be visibly logged.
5. **Prior pre-qualification for the quick margin check** (5 lines in the
   panel's margin stage): zero expected wins today (audit above) but
   activates automatically the day an antenna clears 18.4 dB pools;
   safeguards unchanged.
6. **Direction-fingerprint experiment, not code**: rotate Antenna B a few
   degrees and re-sweep (T2/T3 tie-breaker); start stamping top-3 echo
   delays into scan.json for the cluster model's Feature 3.

## Files
- `Z:\src\adaptive-tv\lab\tower_axis_analysis.py` — Task A tests (T1-T5), rerunnable.
- `Z:\src\adaptive-tv\scan_planner.py` — planner prototype; `selftest` (16 checks) + `replay` CLIs.
- This report: `Z:\src\adaptive-tv\lab\cross_channel_speed_research.md`.
