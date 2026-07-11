---
name: fable-finale-builds
description: "2026-07-07 endgame — GMD/Forney ladder shipped+verdict, turbo blueprint, E7 voting, replay is now the cheap test rail"
metadata: 
  node_type: memory
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

The Fable-era closing builds (2026-07-07 night), after user asked "are we
finished?" — answer: ~2-3 dB from ATSC 1.0's practical wall; remaining
pure-code levers are few and named.

**GMD / Forney erasure ladder — SHIPPED, verdict: keep, no dB.**
`atsc_rs_decoder_erasure_impl.cc`: on hard-fail, walk s=2,4..budget weakest
SOVA bytes, first solution passing full guard battery wins. A/B hook
STVT_RS_GMD=0. Replay A/B: cliff-edge specimen (96% dead) 27 vs 17 rescues;
impulse specimen (3.4% bad) WASH — 795 frames both arms. Confirms Entry-20
disease map: failed codewords here die FAR beyond erasure range (>20 bytes);
the RS-margin rescue pool is small. Kept ON (wins slightly, regresses
nowhere). Telemetry: gmd_trials=/gmd_rej= added so era_dec/miscorr/g2rej
keep pre-GMD meanings (sheriff/cube parsers safe).

**TURBO_BLUEPRINT.md** (Z:\src\adaptive-tv): stage 2 = RS-truth trellis
pinning inside one hierarchical block (each segment feeds ~52 codewords, so
decoded neighbors pin the trellis for failed ones; re-decode only failed
spans, <=2 iterations). Stage 3 = max-log-MAP soft output. These are THE
post-Fable builds — worth ~1-1.5 dB nobody ships.

**tv_replay.py is now the cheap deterministic test rail**: SOVA plane wired
(mirror of tv_live), STVT_IQ_SKIP=N skiphead for decode-diversity. Specimens
at Z:\src\adaptive-tv\lab\captures\*.cs16 (rf15 cliffedge=96% dead — too far
gone for decoder A/Bs; rf21_impulse=3.4% bad breathing, 795-frame baseline;
also rf15_cliffclean, rf36_strong) + tools/data/specimens/*.cs16 (7/06 DEAF
storms). Replay A/B beats live gauntlets for decoder-level questions: no RF
drift, no radio steal, ~3 min per arm.

**E7 union voting** (`e7_vote.py`) — BUILT, WIN on impulse specimen:
N replay passes (skip-offset + DFE alternation diversity) -> clean GOPs
keyed by video PTS -> v2 HEAL-MERGE (substitution not filtration: splice
clean twins over damaged GOPs in fullest pass, never drop). 799 frames
kept, decode errors 715->688, 2/26 damaged GOPs healed. Diversity PROVEN:
GOPs rescued that existed in only one pass. v1 strict-union lost 795->187
frames — filtration discards partially-renderable GOPs; heal, don't filter.
Donor pool scales with pass count + material quality.

**DFE ANCHOR PROMOTED 7/07 late — the night's biggest win.** First test
on rf15_cliffclean was indeterminate (96% dead, can't discriminate). Then
captured fresh discone_rf7_canyon.cs16 (46% bad BREATHING multipath — THE
echo testbed, now in lab/captures with philips_rf15_evening.cs16 as 0.4%
healthy control). Three-arm replay: no-DFE 629 frames / DFE 860 /
DFE+ANCHOR **951** (+51% frames, bad 46%->21% — halved). Impulse
regression check: equal-best (798 frames, bad slightly better). v1.2's
"self-referential mirage" kill verdict was tested on the wrong disease —
on echo channels the anchor is REAL. Now default in panel cliff-mode env
+ ambush3 (STVT_EQ_DFE_ANCHOR=1). Lesson: a killed feature deserves
retrial when the test regime, not the feature, was the suspect.

**night_shift port-map fix 7/07**: flutter probes + rf9 tripwire still
used pre-rewire ports (B=rabbit etc.); fixed to rabbit=A philips=B
discone=C, tripwire filters ant=='philips'.

**TURBO STAGE-2a SICKMAP — built, verdict: opt-in (wash).** Cross-codeword
erasure targeting via interleaver geometry (byte i of codeword n sits on
branch b=(i-n) mod 52, tx-time lag 156+208*(51-b) bytes; deint emits
deint_sync anchor tags; 512-slot sick ring in rs_erasure). Plumbing
verified (2M writes, 7.5M reads) but WASH on canyon AND impulse.
**BIMODAL-FAILURE LAW (proven 3x — fixed-16, GMD, sickmap): RS failures
on real channels are either lightly wounded (hard decode fixes them) or
annihilated (>20 bytes); the erasure-rescue pool is ~0.06% regardless of
aim. Stop mining the RS layer — remaining dB are in EQ/trellis (anchor
+51%) and diversity (E7). Stage-2b trellis pinning is the ONLY remaining
RS-adjacent lever.** sick_wr/sick_hit telemetry stays always-on.

**THE CLOSING TRIALS 7/07 late-night (user-driven regression hunt):**
- IQ RING v1 WAS A REAL REGRESSION (user caught it): per-chunk
  float->int16 in work() stalled healthy chains — live ABBA 4.28% bad
  (ring) vs 2.51% (off) at MER 17. v2 = pure complex64 memcpy, cs16
  conversion on dump thread: verified identical to off (2.43 vs 2.39).
  reg_check.py = reusable live ABBA harness (+--arsenal-arm).
- STRONG SIGNALS INTACT: RF36 UHF control 0.05% bad @ MER 19 steady
  with the full 7/07 codebase — the 16.5 fence works; arsenal never
  leaked onto strong paths.
- RF9 EVENING = BREATHING VHF CANYONS: median 17.2 but dips 13.4-14.3
  BETWEEN field syncs -> steady ~2.4% bad no decoder config touches
  (replug/ring/arsenal all no-ops on it). MER-median is blind to it —
  mer_min is the honest fence input (future: dip-aware cliff switch).
- ARSENAL ON BREATHING RF9: anchor lifts MEDIAN +1 dB (17.2->18.3,
  real EQ gain) but canyon losses unchanged (bimodal law) at ~17%
  throughput cost -> not promoted for median-strong channels.
- USB was INNOCENT tonight (replug changed nothing); the E7-capture
  96% ceilings were separate-session churn, cured by ring architecture.

**E7 panel button v2 — VALIDATED LIVE (8 cycles to perfect)**: 🩺 SECOND
OPINION snapshots the chain's OWN IQ RING (STVT_IQ_RING=35 in panel
base_env, ring dir lab/e7_ring, TRIGGER-file API) — no second SDR
session, no TV pause. Votes 3 warm-started chain-dialect decodes, heals,
pops replay ONLY when GOPs rescued. Validation 8: 995/1019/999 frames,
1 GOP healed, errors 1693->1659. LAWS LEARNED:
- separate capture sessions after open/close churn drop ~4% samples
  (decode-verified 15.8% bad on healthy) — snapshot the chain's ring,
  never open a second session
- REPLAY MUST SPEAK THE CHAIN'S DIALECT: ring specimen = 4.3% bad at
  chain env (SPS 1.1/RRC 8/no FOLD) vs 25.5% at replay defaults
  (SPS 1.5+FOLD) — e7_vote CLIFF_ENV now chain-matched
- iq_capture now RAM-buffers (single write): the old write-per-read
  loop overflowed the SDR ring on disk stalls — EVERY specimen ever
  taken carries ~0.5% capture-side damage; rf15's "0.4% bad" was
  probably mostly recorder, not RF
- healed output must be program-scoped; no-donor votes emit base
  untouched + no popup (remux jitter fakes wins otherwise)

**DLL hot-swap trick**: running chain locks gnuradio-atscplus.dll but
Windows allows RENAME of a mapped DLL -> rename to .old, copy new in;
running chain keeps old mapping, new processes load new build. No user
blackout during decoder iterations.

Related: [[mer-dial-universal-algorithm]], [[overnight-cube-campaign]]
