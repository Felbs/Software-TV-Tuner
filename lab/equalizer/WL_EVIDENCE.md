# Should the widely-linear equalizer ship as the default? — the evidence

**Date:** 2026-07-30 · **Verdict: NO — ship it AUTO-SELECTED, never globally
defaulted.** The data below says WL is a large win in a specific, common
condition and a *hard loss* outside it.

Everything here was measured with `tools/tv_dual.py`, which runs **one** shared
front end feeding **both** equalizers from **bit-identical symbols**, so the only
difference between the two transport streams is the equalizer. Impairments are
injected once, upstream of the split, so both legs see the identical impaired
stream. Frames are counted with an ffmpeg null sink (ffprobe lies on
multi-program TS). Source capture: `lab/marginal_iq/rf34_ctrl.cs16`, 15 s.

## Why the earlier live A/B tests could never settle this

Two sequential live runs sample two different slices of a changing sky. Our
7/29 live gate produced RF34 = 2637 vs 2627 frames — a 0.4% difference, well
inside run-to-run channel variance. That experiment could not have detected the
effect even if it were large, because the measurement noise exceeded the signal.
`tv_dual` removes that entire error term.

## Calibration

`interleaved_short_to_complex(..., 32767.0)` **divides** by 32767, then the chain
multiplies by 32768, so signal RMS into the impairment adder is 13441.4 and
`noise_amp = 13441.4 / 10^(SNR_dB/20)`. Sanity check: this puts `tv_dual`'s own
pre-existing docstring example (`--noise 2147`) at **15.9 dB**, which lands on the
watchability cliff independently measured on 7/09. Two roads, one number.

## Finding 1 — above the cliff, WL wins MER but the win is unspendable

| SNR | long frames | WL frames | Δ MER (p50) | paired fields WL/long/tie |
|-----|------------:|----------:|------------:|---------------------------|
| clean | 403 | 403 | **+2.54 dB** | 612 / 7 / 1 |
| 24 dB | 403 | 403 | +1.18 | 592 / 24 / 4 |
| 20 dB | 403 | 403 | +0.43 | 457 / 123 / 40 |
| 18 dB | 402 | 403 | +0.08 | 345 / 227 / 48 |

WL holds a large and near-unanimous MER advantage on a strong signal — and it buys
nothing, because both legs already deliver 100% of frames. **This is the honest
answer to "why does WL tie on strong channels":** the headroom is real, there is
simply nowhere to spend it.

## Finding 2 — under *pure* thermal noise, WL is WORSE

Δ MER as AWGN rises: **+2.54 → +1.18 → +0.43 → +0.08 → −0.16 → −0.27 → −0.36 →
−0.45 → −0.49 dB** (clean → 10 dB). Perfectly monotonic; crosses zero near
17–18 dB.

The mechanism: WL's advantage comes from second-order **impropriety** — the
imaginary companion carrying information independent of the real part. AWGN is
circular (proper), so it adds none of that while WL still pays the excess-MSE cost
of estimating twice as many parameters (textbook bias–variance). **On a purely
noise-limited channel, WL is a net loss.** Never justify WL with a noise sweep.

## Finding 3 — median MER is the wrong score near the cliff

At 16 dB, WL delivered **+74% more video** while its *median* MER was 0.16 dB
**worse** and it lost the paired-field tally 169/390. WL's payoff lives in the
**tail** of the distribution — fewer catastrophic fields — not the median. A
promotion gate scored on median MER would have rejected the single condition
where WL delivers most. **Score cliff experiments on delivered frames across
seeds.**

## Finding 4 — the cliff win is real: 10/10 seeds, non-overlapping distributions

Frame counts near the cliff are the least reproducible number we measure, so both
cliff SNRs were repeated across 5 independent AWGN seeds:

| SNR | long (median, range) | WL (median, range) | median Δ | seeds WL won |
|-----|---------------------|--------------------|---------:|-------------:|
| 17 dB | 355 (346–360) | 400 (397–408) | **+45** | **5/5** |
| 16 dB | 150 (134–174) | 264 (259–283) | **+114** | **5/5** |

**The distributions do not overlap at either SNR** (17 dB: long max 360 < WL min
397). At 17 dB WL still delivers essentially full video (400 of the 403 clean
maximum) where `long` has fallen to 88%. **WL buys about 1 dB of cliff-edge
margin** — which is the entire watchable-or-not window on a marginal channel.

## Finding 5 — WL earns most under *compound* stress, i.e. real living rooms

| condition | Δ frames | note |
|-----------|---------:|------|
| 17 dB noise only | +45…+54 | |
| 17 dB + 0.87 dB I/Q gain imbalance | **+131** | realistic for a budget SDR |
| 17 dB + complex echo g=0.30 | **+104** | +36% more video |
| 17 dB + real echo g=0.15 | **+88** | |

Note what these have in common: they are the ordinary circumstances of a
newcomer with an imperfect radio and an indoor antenna. In every one of them WL's
*median* MER was slightly negative — Finding 3 held in all seven sweeps.

## Finding 6 — ★ WL has a hard ENVELOPE, and outside it WL is worse

WL is **not** monotonically good. At 17 dB with a strong (g=0.45) complex echo:

- `long` delivered **105** frames
- WL delivered **50**

A reversal, not a gentle rolloff. `conj_frac` (WL's conjugate-branch activity)
rises with echo strength in every arm — 0.063 → 0.107 complex, 0.063 → 0.147
real — confirming the second filter genuinely engages under ISI; but past a
threshold the extra degrees of freedom hurt. **Shipping WL as the global default
would ship this cliff to every user with a strong reflector nearby.**

## Finding 7 — a corrected mechanism (a prediction of mine that failed)

I predicted that injecting `y = x + α·conj(x)` — the canonical "impropriety" —
would *grow* WL's advantage. It **shrank** it (Δ MER +2.53 → +0.40 for α 0 → 0.10;
`imag_benefit` collapsing 0.932 → 0.012). The algebra says why: for `x = a + jb`,
`x + α·conj(x) = a(1+α) + jb(1−α)`, so that knob is a pure **I/Q gain imbalance
that attenuates the imaginary companion** — and 8-VSB is already essentially real,
i.e. already maximally improper, with no impropriety left to add.

Corrected mechanism: **WL's food is not source impropriety but the CHANNEL making
the imaginary companion informative** — that is multipath. And the follow-up
control arm refined it further: the real-echo arm helped WL *more* at g=0.15 than
the complex echo did, so **echo strength, not echo phase, is the primary driver**;
phase shifts where the useful envelope sits. WL is a richer filter exploiting the
imaginary companion 8-VSB already carries, not narrowly a complex-channel trick.

## The recommendation

**Auto-select per channel. Do not flip a global default.** The decision signal
already exists — WL emits `imag_benefit` every field, and it brackets the outcome
cleanly across all seven sweeps:

| `imag_benefit` | condition | action |
|---------------|-----------|--------|
| ≳ 0.9 | strong/clean | either works; frames tie, no gain to collect |
| ~0.5–0.7 | **marginal** | **use WL** — +15% to +58% more video |
| ≲ 0.4 | severe multipath | **use `long`** — WL's reversal zone |

Rule: **enable WL when the channel is marginal AND `imag_benefit` ≥ ~0.5.** No new
instrumentation required.

### Blocker — RESOLVED 2026-07-30 (but needs a live test)

The tap cache used to live in `atsc_equalizer_long` **only**, so choosing WL threw
away the measured ~9.3× warm-start win and STVT's two flagship features were
mutually exclusive. **Now ported into `atsc_equalizer_wl`.**

WL stores two complex branches, so the cache uses its own magic (`'TAPW'`) and its
own file — long's path with **`.wl` appended** — giving each equalizer an
independent warm start that the other can never adopt. An LKG snapshot is banked
only when the field actually decoded well (`fs_err_rms < 1.0`, i.e. MER > ~14 dB),
so a diverged filter cannot be written out and served back as a "warm start."

Measured cold vs warm (offline, `tv_dual`, rf34_ctrl):

| | first field sync | mean first 5 | mean first 20 | settled |
|--|---------------:|-------------:|--------------:|--------:|
| COLD | 14.48 dB | 16.66 | 18.85 | 22.62 |
| WARM | **17.70 dB** | **21.38** | **22.55** | 22.64 |

**80 → 6 field syncs to reach within 0.5 dB of settled = 13.3× faster** (1.94 s →
145 ms of unsettled video at tune-in). Cold's first field sync sits *below* the
~16 dB cliff; warm's is already above it, so with a warm cache the picture is
watchable from the first field.

Regression and safety all pass: with no cache configured the baseline reproduces
exactly (403/403 frames, MER 19.967/22.503) with zero cache output; garbage,
truncated, and foreign (`TAPC`) cache files are each rejected with a clean
fallback.

**LIVE-VALIDATED** the same night — three 45 s visits to RF34 on Antenna B with
`STVT_EQ=wl`, cold then warm then warm:

| visit | warm start? | first fs | mean first 5 | mean first 20 | settled |
|-------|-------------|---------:|-------------:|--------------:|--------:|
| 1 cold | no (correct) | 14.73 dB | 16.29 | 18.80 | 20.55 |
| 2 warm | **yes** | **18.65** | **19.98** | **20.51** | 20.81 |
| 3 warm | **yes** | **19.45** | 19.36 | **20.56** | 20.79 |

**+3.69 dB on the first 5 field syncs**, reproduced on two independent visits, and
live numbers track the offline replay closely (cold first-fs 14.73 live vs 14.48
offline). On air, cold's first field sync sits *below* the cliff and warm's above
it.

⚠️ **`STVT_EQ_CACHE_EVERY` is load-bearing, not an optimisation.** "cache persisted
on stop" printed in **zero** of the three live visits — CTRL_BREAK does not get
GNU Radio's `stop()` to run on our Windows kill path — yet the cache was written
and adopted, entirely via the periodic tick. Setting `STVT_EQ_CACHE_EVERY=0` would
silently disable warm start in live use.

*Measurement subtlety:* `first_fs_seg` is identical cold and warm here, and that
is correct — in `tv_dual` the field sync is found by the shared front end
*upstream* of the equalizer split, so an equalizer warm start cannot move it.
E3's 21-vs-202 live speedup was measured where the equalizer feeds the sync
checker *downstream*. Same feature, different topology, different valid metric.

**Known limitation of the WL port (deliberate, not an oversight):** `long` also
exposes `save` / `warm` verbs on its command port so a *persistent* chain can
persist the old channel's taps, rebind `STVT_EQ_TAP_CACHE_FILE`, and warm-load
the new channel's on a **retune**. WL does not have those verbs yet, so in a
persistent chain that changes channels, WL warm-starts only on the channel it was
constructed for. Fresh-process tuning (the normal `tv_tuner` path) is fully
covered. Adding the verbs is the natural follow-up; it was left out rather than
written untested overnight.

Also note WL's LKG has no `STVT_EQ_LKG` gate. `long`'s snapshot is off by default
in C++ (documented as defect D3 — tv_live has to set it, and a direct run once
asked for a cache and silently never wrote one). WL instead always snapshots when
the field decoded well, so it has no equivalent footgun.

### Open items

- Re-run the live gate in the **evening** window (late-night UHF collapses:
  `in_rms` 19.6 vs ~175 evening, which killed RF36/RF31 on *both* equalizers).
- Validate the `imag_benefit` thresholds on a **second** antenna/capture — every
  number here comes from one RF34 capture on one antenna.
- All measurements are 15 s. Confirm the envelope on a longer capture before
  trusting the reversal threshold quantitatively.

### Reproducing

```sh
python lab/e5_wl_margin_curve.py     # the SNR ladder (Findings 1-3)
python lab/e5b_cliff_seeds.py        # cliff × 5 seeds  (Finding 4)
python lab/e6_impropriety_sweep.py   # I/Q imbalance    (Findings 5, 7)
python lab/e7_multipath_sweep.py     # multipath + real-echo control (6, 7)
```
