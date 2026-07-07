# Next Levers for Marginal-Signal Decode — v2 (2026-07-05 afternoon)
*Supersedes RESEARCH_NIGHT_20260704 items 1-2 (shipped: freeze, Huber,
echo X-ray, IQ tools, flatness meter). Grounded in this week's data:
missing-packet class, canyon channels (RF7 27 dB breathing ripple),
midday impulse floor (~32 gaps/min), 36 s cold tunes.*

## 1 — FEC-directed adaptation  ★ the sleeper, fits our architecture
Today the equalizer trains 1 segment in 313 (field sync) and coasts
otherwise; the existing decision-directed path is gated by SLICER
confidence, which collapses exactly when needed (cliff). But we hold a
truth signal nobody uses: **Reed-Solomon tells us which segments decoded
correctly.** Feed RS-validated bytes back as training (re-encode the
clean segment through trellis mapping = known symbols) and the EQ gets
ground-truth training ~300× more often, at precisely the SNR where it
matters. This is the practical core of turbo equalization (SC/MMSE
iterative EQ↔decoder approaches MLSE-optimal per the literature) without
the full iteration loop. Chain-side, real DSP work — but it attacks the
midday floor, flutter tracking, AND cliff-riding at once.

## 2 — DFE, CIR-informed (planned H3, now better armed)
Linear EQs cannot invert deep nulls (noise amplification); DFE cancels
post-echoes by decision feedback — the textbook cure for RF7-class
canyons. New advantages we didn't have when H3 was drafted: the echo
X-ray measures the actual echo set to design against, and lever #1's
RS-validated decisions would largely defuse DFE's classic error-
propagation risk. Sequence AFTER #1 (they share the decision plumbing).
Bidirectional (time-reversed second pass) variant applies to recordings.

## 3 — Warm start: CIR-seeded taps + per-channel LKG cache on disk
36 s cold tunes, and every deep fade forces a from-scratch reconverge.
We already snapshot LKG taps in RAM; persist per (channel, antenna
profile) to disk, seed on tune; where no cache exists, invert the
X-ray's measured CIR for an analytic first guess (US8711916B2 approach).
Cheap-to-moderate effort, pure plumbing, no decode-path risk (seed then
adapt as normal). Attacks: tune latency, fade recovery, hop feel.

## 4 — IQ multi-pass rescue decode (H4 stage 2)
The capture side exists. Offline decoder pass 1 measures the CIR/MER
trajectory; pass 2 decodes with acausal knowledge (future samples inform
past decisions — impossible live); run N configs and stitch the best
segments by CC-continuity audit. Hardware tuners can never do this.
First customer: any must-keep DVR recording on a marginal channel;
test bench: the RF7 capture from this morning.

## 5 — Learned enhancers (background research track)
2024-25 literature: neural receivers/equalizers beating DFE in studies
(DeepRx-style full replacements; NI shipping real-time SDR prototypes;
LSTM/GRU equalizers vs DFE comparisons). Our pragmatic entry is NOT a
full neural receiver: a small net as pre-slicer symbol denoiser or
CIR-to-taps mapper, trained on OUR corpus — the flight recorder +
IQ captures give us labeled clean/dirty data for free. Park until #1-#3
land; revisit with the Radio Tuna generalization (same nets would serve
HD Radio / LRPT).

## 6 — Hardware track (unchanged)
RSPduo MRC diversity (+3 dB, fade-proof); antenna placement remains the
cheapest dB in the house (rabbit ears: attic beat desk by ~the entire
decode margin).

## Ordering logic
#1 unlocks #2; #3 is independent quick relief; #4 is a separate
subsystem with a waiting test bench; #5 rides on data we're already
collecting. If today's trial crowns freeze or Huber, its Optuna pass
slots before #1 (hours, not days).

Sources: [SC/MMSE turbo equalization ≈ MLSE](https://ieeexplore.ieee.org/document/991143),
[8VSB PCCC/iterative art](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/8386896),
[CIR-based tap init US8711916B2](https://patents.google.com/patent/US8711916B2/en),
[DeepRx-style neural receivers](https://dl.acm.org/doi/abs/10.1109/TWC.2021.3101364),
[NI real-time neural receiver on SDR](https://www.ni.com/en/solutions/electronics/5g-6g-wireless-research-prototyping/prototyping-real-time-neural-receiver-usrp-openairint.html),
[RNN/LSTM equalizers vs DFE](https://onlinelibrary.wiley.com/doi/abs/10.1002/dac.5988).
