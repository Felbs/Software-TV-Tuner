---
name: datamosh-presentation-layer
description: "7/10 saga: the 'datamoshing' was OUR 7/04 +discardcorrupt captions fix, not RF or decoder — user's eyes beat every transport-level instrument; presentation layer was an unmeasured blind spot"
metadata: 
  node_type: memory
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

**THE LESSON: we measured the transport stream religiously and NEVER
measured the pixels.** User insisted for two days "it played better on
the old code" while every TS-level instrument (bad%, MER, null-sink
frames) said nothing changed — because nothing HAD changed in the TS.
The regression lived in PRESENTATION: `fflags=+discardcorrupt` (added
7/04 as a CC-startup-garbage fix, in BOTH mpv flags and the solo
extractor's ffmpeg) drops the whole picture when one TS packet is bad;
I-frames are the biggest pictures so they die preferentially (11/100
survived a measured storm) → P/B frames smear on stale refs up to
6.4 s = the "datamoshing". Every damaged-channel complaint 7/04→7/10
was amplified by it.

**Why:** believe the user's eyes over summary statistics (third time
this pattern won: RF15 morning transient, badge-vs-quality, this).
**How to apply:** when video "looks wrong" but TS metrics are clean,
audit the PLAYER/EXTRACTOR pipeline (flags, concealment, remux) before
re-litigating the decoder; and never ship a presentation flag without
an eyeball test on DAMAGED material (clean-stream tests are blind to
concealment behavior).

**The fix (f4ba8c0, agent-measured on lab/captures/fox_impulse_storm
.cs16):** remove discardcorrupt everywhere; mpv vd-lavc:
`err_detect=+crccheck+bitstream+buffer+explode` (drops truly-broken
pictures individually, ~7 frames/storm) +
`error_concealment=deblock+favor_inter` (static patch, freeze beats
melt; guess_mvs — bit of the old `3` — was the smear synthesizer).
Mosh frames 67.5%→0.7%, frames shown +89%. Inert on clean streams.
Optional "freeze-pure" lavfi select gate validated (hide until next I).

**Also settled by the same storm specimen:** the dormant noise blanker
(STVT_NB) is NOT the impulse cure — 13-arm sweep flat at thresholds
≥3.0, catastrophic 100%-loss at 2.0 (fires on the signal's own
envelope). Stays dormant, now with data.

**RF31/Fox impulse source:** duty-cycling interferer (waves: 0.06% ↔
3.5%+ loss on the same antenna/hour) — July compressor/AC season
suspected; physical hunt via Signal Finder tone remains the real cure.

Related: [[day-quality-baseline]], [[fable-finale-builds]],
[[real-quality-metric-ffmpeg-fps]] (this extends it: null-sink counts
frames but not CONCEALMENT quality — the pixel layer needs eyes or a
stale-ref metric)
