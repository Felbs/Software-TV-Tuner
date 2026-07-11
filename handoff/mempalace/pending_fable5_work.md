---
name: pending-fable5-work
description: "Deferred bigger code changes to do when back on Fable 5 (user is conserving; on Opus 4.8 since ~2026-07-08, wants only small/safe edits until Fable 5)"
metadata: 
  node_type: memory
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

User ran out of Fable 5 credits 2026-07-08 (switched to Opus 4.8) and
wants to **hold big code changes until Fable 5 is available again** —
small bug fixes / campaign babysitting OK on Opus, but save the
substantial builds. **Why:** they value Fable 5 for the heavy work and
are rationing. **How to apply:** when a non-trivial change comes up on
Opus, note it here and confirm before doing it; keep doing safe
overnight-campaign fixes.

**DD LIVE-DEBUT LESSON 7/10 (2c31a35): warm-start cache defeats
readiness flags.** Tap cache pre-sets d_lkg_valid at load → DD adapted
on stale warm taps during AGC settling → tap explosion (err_rms 5e14)
every cold cliff tune, silently eaten by quality-reset. Isolation A/B:
DD-alone clean 4/4; cache+DD = boom. Fix: DD holds until >=3 LIVE FS
trainings this session (monotonic d_fs_trained — disk state can't fake
it). LAW: any adaptive stage gated on "state valid" must distinguish
LIVE-verified state from RESTORED state. Live stress passed after fix:
2x panel cold starts 0 explosions; glass 616/616; live cold-start bugs
are unreachable by replay — always stress live after decoder changes.

**CPU CAMPAIGN CLOSED BY MEASUREMENT 7/10:** chain runs 0.6-0.8 of ONE
core (of 64) — CPU is NOT the binding constraint on this rig. Stage
shares: RRC ~29% of a core, sync ~26%, fpll ~14%, resamp ~12%, lean-EQ
minor. FFT-conv EQ path (STVT_EQ_FFT): validated byte-equivalent,
CPU-wash → opt-in dormant. RRC_SYMS sweep: real CPU lever but QUALITY-
VETOED on breathers (bad +8%@5 → +44%@3; frames 769→575) — span stays
8; knob relevant only for Pi-class ports. LESSON: replay WALL time
can't judge per-stage CPU on many-core (stages pipeline; wall = max
stage) — use total CPU-seconds (psutil sampling; PS TotalProcessorTime
races to 0 after exit).

**DD TRACKING PROMOTED 7/10 (the harvest's first trophy, f1c12d6):**
dormant filterN_dd guarded DD-NLMS (built 5/30 for drift, never tested
vs breathing) at STVT_EQ_DD_MU=1e-2 → RF9 breather replay bad −31%
(193716→134369), frames +13% (769→870); dose-response peak at 1e-2
(3e-2 over-drives); clean control ZERO regression at full dose. In
panel cliff env + ambush3. Coherence finding that predicted it: fades
alias past 41 Hz FS sampling (gain flat ±0.2 dB at sync instants on a
53%-loss specimen). Anchor kills echoes; DD kills flat fading. CPU:
+70% replay wall → cliff-only until FFT-conv paydown. Test rail new
tools: clarke_fader.py (synthetic breathers, seeded), echo_phat
--coherence. Live-chain CPU baseline: 81.1% of one core (glass decode).

**PYSDR HARVEST 7/10 (adaptive-tv/docs/PYSDR_HARVEST.md, commit
940c097): all 26 chapters mined → 23 ranked techniques.** Headliners:
decision-directed EQ tracking (attacks the 41 Hz adaptation bottleneck
= THE breathing lever, likely worth more than turbo 2b per the bimodal
RS-wall finding); time-based erasure marking (sickmap v2, correct
signal); Clarke synthetic fader for tv_replay; overlap-save FFT conv
(~10× EQ/RRC CPU); SigMF (cs16 already valid SigMF data); zero-hw
MER-driven auto antenna switching; coherence-time logger (free, run
overnight, decides DD vs diversity). **⚠ RSPduo coherent mode ≈2 MHz/
tuner — CANNOT do 6 MHz ATSC diversity; need AD9361-class 2-RX (Pluto
rev C/D, USRP B210) — warn user before purchase.** GCC-PHAT echo_phat
validated 7/10 (canyon 2 echoes, control 0, RF9=dynamic-fading-not-
static-multipath). User build order: auto-E7 → X-ray integration →
turbo 2b (reconsider vs DD tracking).

Queued for Fable 5:
- **Guide quality badge**: replace (or augment) the guide's signal-%
  bar — currently pilot-SNR strength `(snr-20)/35*100` in
  tv_tuna_panel.py JS ~line 1211 — with an MER-based WATCHABILITY
  indicator. The scanner now records `mer_med` per channel, so the
  badge can show quality vs the ~16 dB cliff instead of raw strength.
  User noticed twice that great-looking channels read a middling %
  (RF35 "amazing" @80%, RF21 "great" @74%) — the badge measures the
  less-useful half. Display-only change.
- Turbo stage 2b (trellis pinning) — the crown-jewel decoder build,
  see [[fable-finale-builds]] / TURBO_BLUEPRINT.md.

Related: [[fable-finale-builds]], [[overnight-cube-campaign]]
