---
name: overnight-cube-campaign
description: "Antenna×Channel×Time cube (overnight 7/05→06): both antennas software-swapped on RSPdx ports (B=rabbit ears, A=discone), 6 RFs × 2 ants MER-sampled every 20 min until 07:15 + gain self-trim + folded-in RF7/RF9 tropo sentry; ANY_ANTENNA_CAMPAIGN.md = the E1-E8 experiment ladder"
metadata: 
  node_type: memory
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

**2026-07-06 overnight run.** User at work ~9 AM; cube ends 07:15; he wants
continuous watch + upgrades all night ("evolve the code until I can decode
TV on anything").

- Script: `Z:\src\adaptive-tv\overnight_cube.py` (log `cube_log.jsonl`,
  evolving gains in `cube_state.json`, background task boncs8p0f,
  persistent monitor bsjn1xear filtering DECODE/TROPO/SUSPECT/Traceback).
- Antennas: **ANT-B = rabbit ears, ANT-A = discone** (physically wired
  7/05 night). RSPdx port switch = software antenna diversity — the key
  unlock; auto-antenna selection (E2) consumes the cube's map.
- Ladder doc: `Z:\src\adaptive-tv\docs\ANY_ANTENNA_CAMPAIGN.md` — E1 cube,
  E2 port diversity, E3 60s antenna fingerprint interview, E4 instrument
  viterbi/RS (the dark 98%), E5 FEC-directed adaptation, E6 DFE from CIR,
  E7 IQ multi-pass rescue, E8 blend/graceful degradation. Each has a
  Radio Tuna twin listed inline (BER servo, adaptive survey probe,
  HD/analog crossfade).
- **PowerShell-kill law:** bash eats `$_` inside double-quoted powershell
  -Command strings → kills silently no-op → FOUR radio panels stacked on
  one port fighting over the SDR (7/05). Always use the PowerShell tool
  for process kills, never bash-wrapped powershell.
- mer_meter.py still carries the STVT_DABNOTCH=1 fossil (would blind VHF);
  cube sets notch per-RF itself. Fix mer_meter when touched next.

## RESULTS (night of 7/05→06, 264 samples, 22 cycles, zero crashes)

**CHANNEL 9 FIRST VIDEO HEADERS IN PROJECT HISTORY** — via gap-hunts
(rf9_hunt.py: 5-min dwells + STVT_EQ_TAP_CACHE slipped into the cube's
inter-cycle SDR-idle gaps). Bell curve: 05:40=3 hdr, 06:00=19, 06:20=35
(peak, MER 14.56/p90 15.15), 06:40=0. **RF9 decode window ~05:30-06:30,
peak just after sunrise (05:49).** Gap-hunt trick = permanent sentry
feature candidate.

Map: rabbit ears own everything (21/34/36 = 66/66 decodes all night;
RF7 = 10 decodes but ONLY 01:49-06:52). **Discone decoded RF7 3× at dawn**
(15.2-15.4) — "discone can't do TV" is UHF-only. RF15 sleeper: touched
15.03 at 4 AM (hunt it tomorrow). RF21 Baltimore seesaws AGAINST VHF
(17.9 peak 4 AM → 15.5 by 7 AM). Trim receipts: adopted IFGR 26 for
rf7|rabbit at 01:35 → first VHF decode 15 min later; walked back to 32
at 03:35 when conditions changed.

**TIME = OFFICIAL TUNING KNOB (user ratified 7/06 morning).** The
adaptive method's dials are now gain, antenna port, AND hour. Artifacts:
cube_log.jsonl (the night), cube_report.py (renders + exports
cube_map.json for E2 auto-antenna/hour selection). Day cube (09:00→17:00
handoff while user at work) fills the daylight half of the 24 h clock.

## DAY CUBE FINDINGS (7/06 09:00-17:00, E4-annotated)

- **CLIFF S-CURVE MEASURED** (cliff_curve.py, packet survival vs MER):
  0% @ ≤14.0 / 7.6% @ 14.5 / **48% plateau @ 15.0-15.5** / 88-90% @
  16.0-16.5 / 99.8% @ 17. **15.2 dB = HALF-LOSS point, not
  watchability — solid TV wants 16+.** Set every threshold from this
  curve, not folklore.
- **OWNERSHIP FLIPS BY HOUR**: discone TOOK RF7 from rabbit at 09:04
  and held 15+ consecutive daylight decodes (rabbit fell to ~13.7 by
  day); overnight they tied. cube_map.json now has owner_by_hour;
  panel antenna_for() consults current hour. UHF sags ~1-2 dB late
  morning (bottom ~11:00), recovers after noon; RF21 Baltimore rides
  the cliff all day after owning the night.
- **E4 self-audit law (instruments lie until audited)**: specimen
  system shipped TWO false-positive families day one — channel-hop
  quiet→storm forgeries, then frozen-log SYNCLOSS whose stale triggers
  made the NEXT channel dump garbage rings. Fixes: re-arm on log
  rotation; SYNCLOSS only while chain alive+writing; iq_ring discards
  triggers >20s old. Audit every new autonomous instrument's first
  outputs BEFORE trusting its data.
- Recovery-recipe kill (morning, panel): erasures=20 on borderline
  signal = 98.3% miscorrections, gaps 21/min → 3/min at erasures=0
  (eff_eras clamps to 1 — C++ floor, uninvestigated). Forced-video
  threshold 16.5 → 15.8 (was muzzling audio on playable signals).
- Toolchain complete: specimen_watch (storm/syncloss) → iq_ring
  (STVT_IQ_RING env in tv_live) → specimen_autopsy (IMPULSE/FADE/
  SHIFT/**CLEAN-RF = our DSP did it**; validated 3/3 synthetic; stage-2
  = tv_replay w/ FEC narration). Genuine storms are RARE on healthy
  channels — museum stayed empty for hours once watcher was honest.
- Pushed: fd9e03b (night), 4a38056 (noon), 12d7cd3 (day final).
- DAY FINAL: discone RF7 23 straight daylight decodes (owned it 9-5);
  rabbit 47/47 on 34+36, RF21 45/47. **RF15 "DEAF" anomaly**: one 28s
  window MER 17.41 + 99.9% RS fail (pristine symbols, dead FEC = the
  eq→RS dark region live), then gone; 4-min dwell shows RF15
  OSCILLATES 12↔17 dB (multipath class), RS dead even in high phases —
  prime E6/DFE patient. DEAF trigger added to cube (med≥16 + >98% RS
  fail → dump ring mid-chain). Watcher 3rd false family: pilot-only
  channels fired SYNCLOSS — v4 requires sync to have EXISTED in this
  log. Survival curve @166 samples: ~50% at 15.0-15.5, 84-86% at
  16-16.5, 99.7% at 17; the DEAF sample = 0% hole at 17.5.

## MATH NIGHT (7/06 evening, Fable's last) — pushed d78aa56 + DFE commit

- **Wiener warm-start** (wiener_seed.py + STVT_EQ_CIR_DUMP signed CIR):
  closed-form W=H*/(|H|²+1/SNR) → TAPC cache; cosine 0.77-0.81 vs LMS;
  axis shift constant = 55. **CONDITIONAL LAW: seed only sub-cliff +
  only FRESH CIR** (patient +67-83% hdr; STALE seed on breathing cliff
  channel = 62.7%-loss round; healthy = wash).
- **quick_gauntlet.py = the fast test**: paired ABAB × 3 regimes
  (healthy RF34 / cliff RF21 / patient RF7-discone), verdict ~8-10 min,
  drift cancels in pairing; adoption rule "win somewhere, regress
  nowhere". Killed histogram erasures (1 rescue per 586k attempts,
  regressed 2 regimes). RS lesson shipped: erasure ceiling 20→16
  (s=20 = ZERO error margin caused the 30k-miscorr disaster) + guard
  v2 (PID-witness + TEI + AFC invariants, g2rej telem).
- **DFE v1.1 SHIPPED (STVT_EQ_DFE, default OFF)** in
  atsc_equalizer_long: NFB=192 fb taps subtract decided symbols,
  training-symbol history flush per FS, per-field leak. v1.0 bug
  (fb update AFTER history push) corrupted data while FS stayed clean —
  **REPRODUCED THE DEAF SIGNATURE → RF15's DEAF is likely this
  corruption class (eq data-path vs FS-path divergence)**. Gauntlet:
  patient +58 hdr/sample (4x, loss 80→21%), cliff tie, healthy has
  INTERMITTENT instability (adaptN not fb-aware — blueprint next step:
  fb-aware FS anchor). Conditional weapon: marginal channels only.
  Night ambush (night_shift.py) runs RF9 with DFE=1 + warm taps.
- 8 PM+: RF7 sags below cliff on BOTH antennas (its hour passed —
  hour-map vindicated again); evening TV = RF34 NBC (17+).
- **DFE v1.2 anchor = A MIRAGE WE BUILT OURSELVES**: adjusted-target
  training (trn'=trn+fb·hist) made fs_err self-referential → FFE+FBF
  co-drifted to MER "19.5" with 100% RS fail (5-round gauntlet rounds
  3-5). LAW REINFORCED: any metric whose reference the adapting system
  itself controls is a mirage — anchor metrics to transmitted ground
  truth or decoded content, NEVER to internally-adjusted targets.
  v1.3 = anchor opt-in (STVT_EQ_DFE_ANCHOR), default DFE = v1.1
  marginal-channel semantics, sanity-proven. Anchor's redemption path:
  RS-fail-disciplined joint adaptation (E5). Also: 2-round gauntlets
  can miss slow-developing failure modes — decisions about DEFAULTS
  need 5 rounds.
- PHYSICS_LADDER.md (P1-P6): channel tempo/τc + aircraft-flutter FFT
  (DCA flight path suspect for breathing channels), METAR-driven dawn
  forecasting, pilot rising-edge tripwire, FM-beacons-as-propagation-
  oracle (Radio Tuna synergy), MRC/RSPduo, λ/2 standing-wave mapping.
- **0.54 Hz PERIODIC FADE found on RF34/rabbit** (~20 dB above floor,
  hw-AGC was OFF) — P1's first catch; night-shift flutter probes
  disambiguate antenna-local vs common-path. Tripwire (P3) SHIPPED
  into night_shift (early-start ≥13.8 RF9 + hot extension to 07:10).
- **Reseed-on-collapse SHIPPED** (STVT_EQ_RESEED: quality resets
  re-read tap-cache FILE — recovery jumps to refreshed knowledge);
  armed in ambush with QUALITY_BAD_RMS=8.
- **Final-gauntlet law (4 gauntlets consistent): full marginal stack
  (DFE+reseed+warm taps) = sub-cliff ONLY** (+35 hdr/sample patient;
  DFE v1.1-mode ALSO intermittently corrupts healthy ~1-in-3 runs —
  never enable DFE on healthy channels, root cause unknown, suspect
  same eq data-path corruption class as DEAF).
- **GRC NODES FIRST LIGHT**: gr-atscplus/grc/*.block.yml (equalizer_
  long, rs_decoder_erasure w/ max_erasures param, fs_checker_inst) —
  validated via GRC Platform loader, installed in radioconda, [ATSC+]
  category. gnuradio-companion.exe EXISTS in radioconda\Scripts.
  Community milestone queued (env→params pass, example flowgraph).
- NEXT_SESSION.md = the handoff brief (state/checklist/queue/laws).
- **E5 v1 SHIPPED ("the FEC sheriff")**: eq command port (STVT_EQ_CMD_
  FILE polled per FS: lkg/delta/cache/dfe0/dfe1) + fec_sheriff.py
  (confidently-wrong signature → tap surgery → ESCALATE chain-kill).
  KEY FINDING: tap surgery does NOT cure the corruption — 100% RS fail
  persists with zeroed fb + restored LKG → **the persistent damage
  lives DOWNSTREAM of the eq (viterbi/deinterleaver state) = THE DEAF
  ANATOMY, and DEAF IS NOW BREEDABLE ON DEMAND (DFE=1 on RF34, strikes
  within 1-5 × 45s tries)**. Tomorrow's root-cause: instrument
  deinterleaver/viterbi alignment during a bred onset. Sheriff rides
  the ambush (mer 14.0/badfrac 0.6/cooldown 20).
- Corruption specimen kept: specimen_20260706_212651.cs16 (CLEAN-RF,
  replays clean with DFE off — deterministic conviction).
- dawn_score.py (P2 v0): tonight 6.0/10 "decent" (calm+moist, but BKN
  cloud blanket) — check vs ambush catch at breakfast (calib pair #1).
- **CORRUPTION/DEAF FINAL NIGHT STATE (23:20)**: viterbi SCALPEL built
  (STVT_VIT_CMD_FILE full reset) + fired mid-corruption → NO cure in
  10s window; dfe0+lkg ack'd mid-corruption → no cure (equilibrium
  theory dead); deinterleaver cleared (realigns through); corruption
  shows spontaneous REMISSIONS (wrong-ATTRACTOR dynamics). Pre-RS
  sync-byte probe invalid (data whitened until derand). Sheriff regex
  tolerates sync= field (audit lesson #5).
- **CASE CLOSED (7/06 ~23:55): corruption/DEAF = MOD-12 STREAM-PHASE
  SLIP.** Viterbi batches rigidly by 12, NO plinfo realign; a stream
  discontinuity rotates segment→decoder mapping. Detector (field-start
  position within batch grid) caught phase 0→6→2 AT onset; clean runs
  zero slips. Explains everything: flat upstream dials through onset,
  1-in-12 remissions, all resets useless (fault = stream POSITION not
  state), restart cures. Operational cure SHIPPED tonight:
  STVT_VIT_SLIP_FILE announce → sheriff immediate restart (armed in
  ambush). **THE REAL FIX BUILT + VALIDATED LIVE SAME NIGHT (00:2x):
  STVT_EQ_MOD12_GUARD in the EQ — 312 data segs/field = 26×12, so
  FS-time emitted-count mod 12 must be 0; nonzero → swallow k segs
  (≈1ms) → downstream viterbi grid realigns. First encounter: slip
  struck at run start (26% window), guard read phase 7, dropped 7,
  ZERO corrupt windows after (pre-guard: 8-18/run at 100%). Counter
  deliberately NOT reset at detection (else drops oscillate every
  field). Default OFF pending 5-round regression gauntlet (defaults
  law) → then default-on = likely cures RF15 DEAF, RF21 evenings,
  and a chunk of the 98% dark-storm region in one stroke.**

## THE NIGHT OF 7/07 — CHANNEL 9 BECAME A TELEVISION STATION
Tripwire fired 03:30 (RF9 rising 15.45, TWO HOURS early — detection
preceded decoding, P3 vindicated). Ambush2 hot-swap (guard-armed,
rabbit-only after discone blanked 2×; lesson: fixed %2 → %len crash
cost 90s mid-window). **43/43 dwells with video, 35,546 headers
(prior all-time: 35), best 997, MER 15.0–15.9 SUSTAINED 3h40m, window
still open at 07:10 hard stop. Mod-12 guard healed 456 slips (~2/min)
— THE disease that made RF9 impossible.** Dawn-score 6.0 "decent"
underpredicted a monster (P2 calib pair #1: cloudy-but-calm can
still duct). Sheriff slip-kill proven live pre-swap. Slip rate
correlates with atmospheric churn (P1-adjacent note). Morning state:
panel up, guard still default-OFF pending 5-round regression gauntlet
= today's first task, then default everywhere + RF15/RF21 retests.

## DAY-OFF SESSION 7/07 (user home, vibe-code day)
- **GUARD v2 (debounce: same nonzero phase at 2 consecutive FS) PASSED
  the 5-round gauntlet → DEFAULT-ON in the DLL** (STVT_EQ_MOD12_GUARD=0
  opts out). v1 REJECTED by gauntlet: storm-glitch false FS detections
  caused needless drops. Defaults law vindicated twice in one arc.
- **SLIP SOURCES SOLVED**: (1) CPU load ×8 (28 spinners: 2→16 raw
  slips — RESOLVES the DFE-causality paradox: DFE's CPU cost →
  SoapySDR drops → slips, no signal feedback path needed); (2) USB
  hub: swap powered-hub → motherboard USB3 = slips 2→**0**, MER best-
  of-day, no dud attempt. **USB LAW EXTENDED: no hubs, ever** (hub
  joins the 7/04 leaky cable). Slip mining: fires uncorrelated with
  MER/fades — atmosphere acquitted.
- RF15 midday retest: still under-cliff-oscillating (12.2 med / 16.3
  p90), guard can't help below water; REAL DEAF trial = evening high
  phase 16:00-17:00 (guard+DFE together) — scheduled.
- Watchability lesson: hdr/s counts ≠ watchable; survival curve rules
  (16+ = players work; 15.1 morning RF9 = countable-not-watchable;
  ffmpeg found 0 assemblable frames despite 2.6 hdr/s). Panel needs a
  watchability gate + tv_watch marginal needs --force-window (mpv
  opens no window until first decoded frame). VLC-direct on growing
  marginal TS ALSO fails (cone) — record-then-play is the marginal
  viewing path (tail→ffmpeg pipe broke on cliff AC-3; video-only -an
  works for capture once stream ≥16).
- Rogue-sheriff lesson: orphaned children survive parent crashes —
  sweep fec_sheriff/tv_live orphans before radio work (a rogue killed
  a gauntlet sample at 08:11). Instrument-audit #6: cube RE_RS regex
  went stale when sync= field was added (rs5 stats silently None for
  ~12h).

## DAY-OFF AFTERNOON 7/07 (the position revolution + SOVA)
- **PHILIPS ON B = NEW TV CHAMPION** (found connector → 3-antenna era:
  philips B / rabbit A / discone C): census RF34 19.3, RF36 18.9,
  **RF15 WETA CONQUERED 17.66/362hdr (was 1 header LIFETIME)**, RF9
  decoding AT NOON 15.5. Rabbit moved in attic → its profile
  scrambled (gained WETA 17.1, lost RF7 to floor). **POSITION IS THE
  BIGGEST DIAL — a moved antenna is a NEW antenna** (hardware epochs).
- **Bayesian layers shipped**: belief_map.py (per-cell posteriors,
  exp-decay tau 18h, hardware-epoch variance inflation) + Thompson
  antenna_for() in panel. USER STEER: user picks antennas — code's
  job is decode-whatever-is-thrown; Thompson = suggester only, add
  manual antenna picker to panel UI (TODO).
- **Play-path campaign**: watchability = assemblability cliff ~16
  (steady 15.4 = ZERO ffmpeg-decodable frames despite headers);
  harvest_player.py (GOP-gated force-play: scrubbed input — TEI marks
  ARE the damage map; PES-anchored boundaries; healthy 22/31 GOPs →
  1266 frames; steady-cliff honestly 0 — prey = BREATHING channels);
  scrub A/B: concealment doubles stream richness (future vote-merge
  material). bo_harvest.py = BO (homebrew GP+EI) over 6 knobs,
  objective = harvested true GOPs; first hunt aborted (RF21 afternoon
  slump = flat objective — BO needs a live gradient; rescheduled into
  17:00 evening_block.py: oracle → BO → evening cube 22:30).
- **SOVA SHIPPED (Fable's viterbi upgrade)**: per-decision confidence
  (best-vs-runner-up margin, TB-aligned ring) → per-byte reliability
  plane (port 2, mirror fifos, 0-255) → TWIN deinterleaver (plinfo out
  needs null_sink!) → rs input 2 erases trellis doubts.
  **174 rescues/75s vs histogram's 1-in-586k lifetime; cliff gauntlet
  +16 hdr +0.28 dB, healthy 0 packet harm.** Marginal-lane adopted
  (ambush stack = guard+DFE+reseed+SOVA+warm taps); default pending
  5-rounder. beacon_oracle.py built (FM path-sounding on discone-C,
  first sounding pending — crashed on replug).
- TUNA SCIENCE section on nerd tab: survival %, time knob, guard/
  sheriff/dawn/oracle cards + education notes.

## 7/07 EVENING CLOSE — THE MAP COMPLETES
- **BALTIMORE CONQUERED ~18:15**: user re-aimed rabbits (tone-guided,
  RF21-targeted) → **16.4-16.7 STEADY, watchable-grade** — best RF21
  ever at any hour. Journey: new "clear" attic spot read 7.5 (visual
  clutter ≠ RF terrain — standing waves rule), old spot 14.3, touched
  15.6 mid-sweep, locked 16.5. **SIX FOR SIX: every roster channel
  decodes at 18:15 on at least one antenna.** Hardware epoch logged;
  position marked physically.
- **THE TWO-MODE CHAIN = the era's closing design (e6e7105)**: final
  5-round gauntlet showed full stack costs healthy channels ~20%
  throughput (CPU tax, equal quality) → lean chain for healthy,
  full arsenal (SOVA14+DFE+reseed, guard always) auto-deployed by the
  panel's cliff-mode. Gauntlet patient cell recast → discone-C RF7.
- Evening cube findings: **H-A CONFIRMED — RF9-philips is an ALL-DAY
  channel** (16.7-16.8 held 15:00→17:30+; the dawn window was the
  rabbit's marginality borrowing atmosphere). H-D holding (philips
  UHF flat ~19 through evening — diurnal swings partly marginality-
  amplified; sunset pending). BO lesson shipped: pilot gate (never
  hunt a flat objective; RF21 slump session = crater cartography,
  BETA≥3e-4 always diverges — priors banked).
- Aim-tone ops lessons: sounddevice binds default audio AT LAUNCH
  (replug headphones → restart tone); attic cable jostle wedges SDR
  (in_rms 0.0 = wedge, not aim).

Related: [[mer-dial-universal-algorithm]], [[adaptive-tv-project]],
[[radiotuna-project]], [[sdr-single-tuner-rule]].
