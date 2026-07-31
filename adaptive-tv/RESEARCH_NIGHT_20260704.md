# Night Research Memo — 2026-07-04/05
*Written while the overnight campaign runs. Question: what haven't we
tried that the literature says works — ranked by expected edge per unit
of risk for "runs on any antenna"?*

## What we already have (for contrast)
Linear feedforward LMS equalizer ("long") trained on field sync, with
gear-shift beta, leakage, LKG tap snapshots + quality-triggered restore,
erasure-mode RS, hardware AGC servo, impulse blanker (measured: dead
end), forced-video playback. Never tried: anything below.

## Hypothesis 1 — Impulse-gated adaptation freeze  ★ cheapest, try first
The literature's "stop-and-go" NLMS gates equalizer adaptation with a
reliability signal: don't let LMS learn from garbage samples.
We already HAVE the reliability signal: max|x| rails / fs_err spikes
during impulse hits (fireworks, RF15's afternoon assassin). Today the
taps get poisoned by every burst and must re-converge after.
**Change**: freeze mu (or drop to mu/100) for N symbols whenever the
FPLL phase error rails; resume after. A few lines in the equalizer.
**Predicts**: fewer post-impulse glitches; RF15 afternoon + fireworks
regimes benefit most. Test = A/B on an impulse-heavy hour, gaps/min.

## Hypothesis 2 — PN511 correlation = channel X-ray  ★ biggest UX win
Every ATSC field sync carries a known 511-symbol PN training sequence.
Correlating received baseband against it yields the channel impulse
response (CIR) directly: every echo's delay and strength, ~20×/sec.
Two products from one correlator:
  a) **Echo viewer in the panel** — the RF7 mystery tonight (56 dB
     carrier, MER 9.5) would be VISIBLE: "−2 dB echo at 4.7 µs" tells
     the user which wall the reflection is bouncing off; aiming kills
     echoes instead of chasing a scalar. This is the aiming tool.
  b) **Analytic tap seeding** (patent US8711916B2 approach) — initialize
     the equalizer from the measured CIR instead of from zero. Beats
     our planned "cache old taps" warm start (works on first visit to a
     channel, adapts to path changes). Attacks the 36 s cold-tune time.

## Hypothesis 3 — Decision-feedback equalizer (DFE), maybe bidirectional
Literature consensus: linear equalizers (ours) struggle with strong
echoes; DFE cancels post-echoes using symbol decisions, and the
serial bidirectional variant (normal + time-reversed cascade) exploits
multipath diversity further. This is the heavy-artillery answer to
indoor/VHF multipath (RF7's actual disease).
**Risk**: error propagation at cliff edge (long burst errors), real DSP
work in the .cc chain. Big win if it lands; not a weekend hack.

## Hypothesis 4 — IQ-capture "DVR rescue mode"  ★ hardware can't do this
Hardware tuners decode live or not at all. We can RECORD RAW IQ for a
marginal show and decode OFFLINE with unlimited patience: multi-pass
(try 10 configs, keep best per segment), bidirectional equalization
(acausal — future samples help past decisions), even splice passes.
8 MS/s cs16 ≈ 32 MB/s ≈ 115 GB/hr — feasible for a must-have recording.
**This is the flagship "software does what silicon can't" feature.**

## Hypothesis 5 — True diversity (hardware ask: RSPduo)
SDRplay's RSPduo does dual-tuner maximal-ratio combining: two antennas,
signals co-phased and summed, up to +3 dB SNR and strong fade
resistance. Rabbit ears + Philips MRC'd together would likely hold
RF34/RF36 above cliff through the flutter we watched all day. Our RSPdx
is single-tuner — this one costs money (~$280), not code.

## Ranked plan
1. H1 impulse-gated freeze (hours, pure win, test on next impulse day)
2. H2 PN511 echo viewer + tap seeding (the aiming revolution + faster
   tunes; medium effort, no risk to existing path — it's additive)
3. H4 IQ rescue mode (independent subsystem, showcase feature)
4. H3 DFE (only after H2 gives us CIR truth to design against)
5. H5 RSPduo (money; revisit after H1-H4)

Sources: IEEE 4586429 (quadrature DFE for 8-VSB), ResearchGate 3180300
(timing-offset independent EQ / stop-and-go NLMS), SB-DFE literature,
US8711916B2 (CIR-based tap init), US20020164966 (PN511 pre-equalizer),
sdrplay.com + rtl-sdr.com RSPduo MRC diversity articles.
