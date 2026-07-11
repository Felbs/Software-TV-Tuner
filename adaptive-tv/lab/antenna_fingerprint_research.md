# Antenna fingerprint research — can the Knob of Time transfer to unseen channels?

2026-07-11 · offline analysis of `lab/quality_history.csv` (14,220 rows,
2026-07-04 → 07-11) · analysis code: `lab/antenna_fingerprint_analysis.py`
· prototype: `time_knob_prior.py` (imported by nothing)

**Hypothesis under test** (user's): antennas share structure across
channels — frequency response and tower identity — so the time-knob could
predict MER and best-antenna for never-visited channels.

**Verdict: PARTIALLY HOLDS.** The VHF/UHF *regime* fingerprint is real,
big, day-stable, and transfers (LOO MER error 2.0 → 1.6 dB overall,
2.5 → 0.7 dB on VHF). Everything finer-grained **refused to transfer**:
within a regime, per-channel antenna *territories* (±3.5 dB) dominate;
frequency-distance kernels add nothing over the plain regime mean;
hour-curve shapes are channel-specific; and best-antenna prediction for
unseen channels **lost** to "always suggest the globally best antenna"
(2/7 vs 5/7). The shipped prototype is therefore deliberately minimal:
regime-mean transfer with honest error bars and a toss-up-aware suggester.

---

## 0. Data & assumptions

- 13,829 usable MER rows after dropping `mer`-less rows, `ant='?'` (386),
  and RF27 (n=5). Channels: 7, 9, 15, 21, 31, 34, 35, 36. 8 days of data.
- **Label merge assumption:** rabbit≈Antenna A, philips≈Antenna B,
  discone≈Antenna C. Memory notes these are technically separate
  populations (legacy labels came from different physical placements).
  Every analysis was run **merged and unmerged**; the headline
  conclusions are identical in both (§6), so the merge does not drive
  the results. Cell statistic = *hour-balanced* median (median of
  hour-bin medians, bins n≥3) to remove sampled-at-3am bias; solid cell
  = ≥30 rows over ≥2 days.
- **Tower data: none exists in the repo.** The panel's "ALL TOWERS"
  feature treats each RF as its own tower (pilot hopper over scan.json
  carriers); scan.json carries callsigns only. Tower grouping below is a
  **stated assumption from DC-market geography**: Tenleytown/NW-DC
  cluster = RF7 WJLA, 9 WUSA, 15 WETA, 34 WRC, 35 WDCA, 36 WTTG (+ RF21
  WDCW); Fairfax/Manassas = RF31 WPXW. RF21 is *ambiguous* — Baltimore
  MPT is co-channel and has been decoded here on rabbit ears.

## 1. Hour-curve correlations (A1) — NEGATIVE

Pearson r between per-channel hour-curves (median MER per hour, bins
n≥3, ≥6 common bins):

| grouping | mean r | median r | n pairs |
|---|---|---|---|
| same antenna, different channels (merged) | **+0.08** | +0.07 | 43 |
| cross antenna, different channels (merged) | −0.02 | −0.03 | 60 |
| same antenna (unmerged labels) | +0.02 | +0.04 | 50 |
| cross antenna (unmerged) | +0.00 | +0.04 | 150 |

Same-antenna correlation is directionally above cross-antenna but both
are ≈0, with pair-level r scattered −0.68…+0.64 in no stable pattern
(e.g. RF7×RF9 on A: r=−0.49 — *adjacent VHF channels on one antenna
anti-correlate*). **Hour-curve shapes do not transfer between channels.**
This matches the territory/Knob-of-Time findings: diurnal structure is a
per-channel property (RF9 evening-only, RF34 always-safe).

## 2. Frequency smoothness (A2) — WEAK within regime, HUGE across regimes

|ΔMER| vs |Δf| over solid cells, within-regime pairs:

| antenna | Spearman ρ | n pairs |
|---|---|---|
| A (merged) | +0.38 | 16 |
| B (merged) | +0.21 | 11 |
| Antenna A (live only) | −0.22 | 15 |
| philips (legacy) | +0.45 | 7 |
| rabbit (legacy) | +0.27 | 7 |

Weak, sign-unstable across label populations — 6 MHz neighbors can
differ 3-5 dB (RF35–RF36 on A: 3.0 dB) while 90 MHz pairs match to 0.4 dB.
**The regime split is the real structure:** cross-regime (VHF↔UHF)
|ΔMER| on antenna A averages **6.4 dB** (10.4 dB live-labels-only)
vs ~2 dB typical within-regime; on B (philips, VHF-capable) it's only
0.9-2.7 dB. Conclusion: frequency proximity inside a band buys almost
nothing; the VHF/UHF boundary is where antennas actually differ.

## 3. Tower sharing (A3) — INCONCLUSIVE (data nearly degenerate)

With 7 of 8 channels on the assumed Tenleytown cluster, only 3-4
diff-site pairs exist (all involving RF31):

| | mean r | median r | n | mean |Δf| |
|---|---|---|---|---|
| same-site pairs | +0.13 | +0.08 | 18 | 64 MHz |
| diff-site pairs | −0.01 | −0.04 | 4 | 51 MHz |
| same-site (RF21 excluded) | +0.14 | +0.09 | 11 | 60 MHz |
| diff-site (RF21 excluded) | −0.03 | −0.11 | 3 | 48 MHz |

Directionally consistent with the hypothesis (same-site correlates
slightly better at comparable spacing) but n=4 pairs, effect ≈0.15 r —
**not usable evidence**. Behaviorally, RF31 does stand apart (it is the
only channel where A beats B in the UHFhi band, and the known
impulse-interferer/cliff-fade channel), which is *suggestive* of a
site/path effect. A single second-site channel cannot separate
"different tower" from "RF31 is weird". No tower term ships.

## 4. Antenna fingerprints (A4) — REAL at band level, day-stable

Hour-balanced median MER, solid cells (merged):

| RF | MHz | band | A | B | C |
|---|---|---|---|---|---|
| 7 | 177 | VHFhi | 9.7 | **16.0** | 14.7 |
| 9 | 189 | VHFhi | 11.2 | **16.4** | – |
| 15 | 479 | UHFlo | 13.6 | **17.3** | – |
| 21 | 515 | UHFlo | **17.0** | 13.8 | – |
| 31 | 575 | UHFhi | **18.2** | 15.8 | – |
| 34 | 593 | UHFhi | 17.5 | **19.2** | – |
| 35 | 599 | UHFhi | 18.8 | – | – |
| 36 | 605 | UHFhi | 15.8 | **19.3** | – |

Band fingerprint (relative to per-channel cross-antenna mean):

| antenna | VHFhi | UHFlo | UHFhi |
|---|---|---|---|
| A (rabbit) | **−3.2 dB** (sd 0.6) | −0.1 (sd 1.7) | −0.5 (sd 1.2) |
| B (philips) | **+2.6 dB** (sd 0.0) | +0.1 (sd 1.7) | +0.5 (sd 1.2) |
| C (discone) | +1.2 (n=1) | – | – |

**Day stability:** per-day A−B differences are sign-stable on 5 of 6
shared channels (RF7: −7.4±0.3 dB across 4 days; RF21: +3.8±0.9 across
4 days; only RF34 wobbles through 0 once). The fingerprint is a stable
antenna property, not day noise.

**But note the UHF column:** the band means are ≈0 with sd ≥1.2 because
*territories* cancel them out — RF21 is A's by +3.3, RF15 is B's by
+3.7, RF31 A's, RF34/36 B's. Residuals after removing the antenna band
mean run **±3.5 dB**, channel-specific, and (per the day-stability
table) persistent. These are exactly the "antenna territories" already
proven live, and they are *unlearnable for a channel you've never
visited* — likely multipath/aperture geometry per bearing, not smooth
frequency response.

## 5. Leave-one-out scorecard (A5) — THE MONEY TABLE

**LOO-CHANNEL** (all rows of the channel hidden; predict each solid
(rf, ant) cell from other channels only; n=15-16 cells):

| model | MER MAE (dB) |
|---|---|
| regime-mean transfer (shipped) | **1.59** |
| freq-weighted kernel + shrinkage | 1.67 |
| band-mean (VHFhi/UHFlo/UHFhi) | 1.85 |
| baseline (a): antenna global median | 1.97-2.15 |
| baseline (b): global median | 2.11 |

Split by regime (regime-mean model vs antenna-median baseline):
- **VHF cells: 0.74 vs 2.47 dB** — the transfer's entire win. Knowing A
  is bad at VHF from RF7 predicts RF9 within ~1.6 dB where the baseline
  errs 5-7 dB.
- UHF cells: 1.97 vs 1.74 dB — transfer ≈ baseline (slightly worse);
  territories are the irreducible error.

The fancier models (frequency kernel, finer bands) do **not** beat the
plain regime mean — evidence that within-band frequency response is not
the operative structure at this rig.

**Best-antenna hit-rate** (channels with ≥2 solid antennas, n=7):

| picker | hits |
|---|---|
| transfer model (any variant) | 2/7 |
| baseline (a): always the globally best antenna (B) | **5/7** |
| baseline (b): random (2 antennas) | 3.5/7 expected |

**Negative result, stated plainly:** for never-visited channels the
transfer model is *worse than random* at picking the antenna, because
UHF best-antenna is decided by ±3.5 dB territories that anti-correlate
with any smooth predictor. The model only got the VHF channels right —
where it agrees with "pick B" anyway. Correct policy: start with the
globally best antenna; only let the prior overrule when its gap exceeds
territory range (in practice: across a regime boundary).

**LOO-CELL** (channel known on *other* antennas, new on this one —
additive channel-effect + antenna-offset transfer): MAE **2.33 dB vs
2.08 baseline — transfer LOSES.** Same cause: the A↔B offset is
territory-dominated per channel. Do not transfer across antennas.

## 6. Label-merge sensitivity

Unmerged run reproduces every conclusion: VHF fingerprint signs match
(rabbit −3.6 / Antenna A −6.5; philips +1.9 / Antenna B +3.1), hour-curve
correlations ≈0 both groupings, within-regime smoothness weak and
sign-unstable. One real divergence: rabbit-legacy UHFlo −1.1 vs live
Antenna-A UHFlo +1.5 — consistent with the memory note that legacy rows
came from a different physical placement (attic + 25 ft RG6). This is
noise for the regime-level model but a reason **not** to unify the label
populations for anything finer.

## 7. Honest limits

- **8 channels, one market, one week.** VHF conclusions rest on 2
  channels (RF7/9); UHFlo on 2 (RF15/21). The regime split is consistent
  with antenna physics (rabbit ears vs Philips panel), which is why it's
  trusted despite small n.
- **Single second-site channel** (RF31) → tower hypothesis untestable
  here. RF21's Baltimore co-channel contaminates its identity.
- **Seasonal**: all data is July (AC-season impulse noise, tropo
  mornings). Fingerprints are day-stable within the week; season-stable
  is unproven. The 14-day half-life in the prior inherits the
  "recalibrate forever" law.
- Cell medians mix sources (flight_recorder vs scan vs labs) with
  different chain configs; hour-balancing helps but epochs remain.
- Discone (C) has one solid cell — its fingerprint is a placeholder.

## 8. Wire-in plan (NOT wired — `time_knob_prior.py` is imported by nothing)

Module: `prior(rf, ant, rows)` → `{mer_estimate, confidence
(solid/thin/fallback), basis, expected_mae_db, pool_spread_db}`;
`suggest(rf, rows)` → ranked antennas + `decisive` flag (True only when
the gap beats TERRITORY_DB=3.5 and isn't a fallback). Self-test runs the
real LOO and asserts the prior beats the antenna-median baseline.

1. **Guide badge (new/thin channel):** panel guide shows
   "~17 dB expected on Antenna B (prior ±1.6)" instead of blank, from
   `prior()` — confidence string surfaces as solid/thin.
2. **Deep-tune starting antenna:** order the antenna trial loop by
   `suggest()`; when `decisive` is False (all intra-UHF cases today),
   keep the current globally-best-first order — validation says never
   let the prior overrule inside territory range.
3. **Fast-tune qualification:** a never-visited channel qualifies for
   the MEMORY-TUNE fast path immediately iff
   `mer_estimate − expected_mae_db ≥ 15.2 (cliff) + margin` with solid
   confidence — e.g. a new UHF subchannel predicted 18.5±1.6 skips the
   full calibration ladder; one predicted 16.0±1.6 does not.
4. **What NOT to wire:** no hour-curve transfer, no cross-antenna
   channel transfer, no tower term — all validated negative above.
