---
name: turbo2b-shipped
description: "7/10: TURBO 2B trellis pinning BUILT+LIVE on all panel tunes (STVT_TURBO=1 + STVT_SOVA=1 in base_env) — converts failed RS packets via pinned Viterbi re-decode; conversion is disease-dependent (69% impulse storm, 23% RF9 fader, ~0% steady-drizzle Fox)"
metadata: 
  node_type: memory
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

**Turbo stage 2b (the parked crown jewel) shipped 2026-07-10**, commit
3bc7ab2 on main-universal. Built inside atsc_rs_decoder_erasure: on
RS+GMD failure, weakest-SOVA bytes map back through deinterleaver
algebra + viterbi mux inverse to trellis spans; pinned full-traceback
Viterbi re-runs them with DECODED codewords' bytes as branch
constraints; one more RS+GMD pass behind the guards. 56-codeword lag.
Opt-in STVT_TURBO=1, needs SOVA plane; knobs STVT_TURBO_{LAG,BYTES,
CTX,PINZ2,MAXSYM,SELFTEST}. Wired in tv_live.py AND tv_replay.py
(equalizer:0 → rs:3, min_output_buffer 512). Panel base_env now sets
STVT_SOVA=1 + STVT_TURBO=1 → every tune runs it.

**Measured conversion of failed packets is a disease fingerprint:**
- impulse storm specimen (replay): 68.6% converted, bad 1.43→0.46%
- RF9 fast fader (live): ~23%, 65k pkts rescued in 5 min, 1.3 cores
- Fox steady drizzle (live, 0.9% loss): 0.1% — those failures arrive
  annihilated (plumbing/leakage class, not near-misses)
- strong clean channel: inert (0.9% attempts, no regression)
Zero miscorrections everywhere; frames never dropped.

**Also that day:** DD μ sweep = 1e-2 already optimal (bowl bottom;
5e-2 catastrophic runaway — clamp μ ≤ 2e-2 if ever runtime-tunable);
DD promoted to base_env (see [[drizzle-wave-interferer]]); honest loss
badges: live loss_pct chip in panel status + scanner records loss_pct
during dwell → guide badge overrides MER label when loss ≥ 0.3%
(fast faders alias 41 Hz MER sampling — RF9 read "flawless" median
17.3 while losing 10%/min).

**STAMPEDE GATE (same evening, commit 5ef74d6):** on a >few-% failure
channel turbo's re-decodes blow the realtime budget → source
overflows → MORE failures → self-sustaining death spiral (RF15: 768k
attempts/2min, 333 OsO, 52% loss on a channel that gated measured
0.04% GLASS — the stampede WAS the disease; cold tune-in can trigger
it since pre-lock everything fails). Fix: failure-rate EMA gate
(STVT_TURBO_MAXFAIL_PCT=4 default) — above it turbo skips (converts
~0% there anyway), chain recovers, turbo resumes. fail_ema=% now in
[rs_turbo] telemetry. LAW: any expensive per-failure rescue path
needs a failure-RATE gate or it self-amplifies exactly when the
channel is worst.

**Why:** the bimodal RS-failure law extends: turbo converts the
near-miss class only; conversion % is a cheap live probe of WHICH
disease a channel has.
**How to apply:** low conversion + steady loss → hunt plumbing/
interferer, don't sweep decoder knobs. High attempts + high conversion
→ decoder is earning its keep, channel is marginal-RF.

Related: [[fable-finale-builds]], [[mer-dial-universal-algorithm]],
[[datamosh-presentation-layer]]
