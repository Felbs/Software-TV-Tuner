# TURBO_BLUEPRINT.md — iterating between the Viterbi and RS
2026-07-07 night. The last great pure-code lever. Stage 1 shipped tonight;
stages 2-3 are the post-Fable builds.

## Why this wins physics
ATSC 1.0's concatenated code (trellis inner, RS(207,187) outer) predates the
turbo revolution (1993). Every consumer receiver decodes it ONCE, one-way.
Iterative decoding of legacy concatenated codes is proven science worth
~1-1.5 dB — and nobody ships it. Our SOVA plane (2026-07-07) built the
missing plumbing: per-byte trellis confidence delivered to RS.

## Stage 1 — GMD / Forney ladder (SHIPPED tonight)
`atsc_rs_decoder_erasure_impl.cc`: on hard-decode failure, walk the erasure
ladder s = 2,4,...,budget over the s weakest SOVA bytes; accept the FIRST
solution passing the full guard battery (sync 0x47 + TEI + AFC + PID
witness). One fixed 16-erasure pattern wastes correction power erasing good
bytes; GMD finds each codeword's sweet size. This IS Forney's original
"generalized minimum distance" iteration between inner soft info and outer
algebraic decoding. A/B hook: STVT_RS_GMD=0 restores fixed-smax.

## Stage 2 — RS-truth back-propagation (design)
Key structural fact: the convolutional interleaver spreads each RS codeword
across 52 segments, and conversely each SEGMENT contributes one byte to ~52
DIFFERENT codewords. So when most codewords in a window decode OK, every
segment has most of its bytes KNOWN-CORRECT — powerful pinning information
for re-decoding the trellis stretches that fed the FAILED codewords.

Architecture: one hierarchical block `atsc_turbo_decoder` owning
viterbi + deinterleaver + RS internally (stream graph cannot loop):
  1. Buffer W=104 segments of soft symbols (two interleaver depths).
  2. Pass 1: SOVA viterbi -> deinterleave -> RS+GMD as today.
  3. For failed codewords: map their 207 byte positions back through the
     interleaver to (segment, byte) coordinates. Mark those trellis spans.
  4. Pass 2: re-run ONLY the affected per-mux viterbi decoders over their
     buffered soft input with PINNED branch metrics: bytes lying in
     codewords that DECODED are known — force those trellis transitions
     (metric 0 for the known branch, INF for others). The pinning breaks
     error events that spanned the failed bytes.
  5. Re-deinterleave affected bytes, re-run RS+GMD on the still-failed
     codewords. Iterate <=2 times (returns vanish after that).
CPU: pass 2 touches only failed spans — on a breathing channel that's
5-20% of segments. Latency: +2 interleaver depths (~8 ms of stream).

## Stage 3 — soft output upgrade (optional, after stage 2)
SOVA margin is an approximation; max-log-MAP (BCJR) per-byte LLRs are
tighter and feed both GMD ordering and the pinning decisions. Only worth
it if stage 2 shows iteration gain that saturates on reliability quality.

## Honesty rails
- Deterministic replay A/B on lab/captures/*.cs16 before any live claim.
- Adopt only if wins somewhere, regresses nowhere (gauntlet law).
- Frames = ffmpeg null-sink only.
