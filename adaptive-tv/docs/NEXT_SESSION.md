# NEXT_SESSION — start here (rewritten end of 2026-07-07, Fable's last day)

## The rig (three-antenna era, since 7/07 midday)
- **Philips = ANT B** — reigning TV champion (19+ on UHF pair, WETA
  conqueror 17.7, RF9 at noon 15.5). Position only "similar" to its
  7/03 calibration — a flatness-tone aim session would likely add dB.
- **rabbit ears = ANT A** — moved in the attic 7/07: profile scrambled
  (gained WETA 17.1, LOST RF7 to floor, Baltimore dented). Re-aim with
  the flatness tone targeting RF21 (its unique crown) when convenient.
- **discone = ANT C** (BNC→UHF adapter) — FM oracle home; TV only VHF
  (RF7 ~1.3 dB worse on C than A; its TV career is over anyway).
- USER STEER (standing): the user picks antennas — panel has a manual
  antenna dropdown (guide tab); belief-map Thompson is a SUGGESTER.
- SDR on motherboard USB3 (hub REMOVED — it caused slips; law: never
  hubs). SMA adapters on order → rabbit re-aim + all-ports-forever.

## The weapons (all validated live, all in the DLL)
| Weapon | Env | Status |
|---|---|---|
| MOD-12 guard v2 | STVT_EQ_MOD12_GUARD (**default ON**) | 5-round passed; 456 saves the historic night |
| DFE v1.3 | STVT_EQ_DFE=1 | marginal lane only (healthy-channel intermittent corruption unresolved — root cause was mod-12 slips via CPU load, may be safe now: RETEST with guard on) |
| Reseed-on-collapse | STVT_EQ_RESEED=1 | built, armed in ambush |
| FEC sheriff | fec_sheriff.py | 3 tiers proven live (surgery/scalpel/kill) |
| **SOVA** | STVT_SOVA=1 + RS erasure≥1 | **174 rescues/75s vs 1-in-586k lifetime**; cliff +16 hdr +0.28 dB; healthy no packet harm; marginal-lane adopted; **5-round default gauntlet = first task** |
| Harvest player | harvest_player.py | GOP-gated force-play; healthy 71%→1266 frames; prey = breathing channels; live --follow mode untested |
| BO knob tuner | bo_harvest.py | homebrew GP+EI; needs a live gradient (breathing channel) |
| Belief map | belief_map.py | posteriors + hardware epochs + Thompson |
| Dawn forecast v2 | dawn_score2.py | real radiosonde refractivity (Wyoming WSGI; cert fallback); calibration pair #1: surface-6.0 under a monster (duct was aloft) |
| Beacon oracle | beacon_oracle.py | FM path-sounding on discone-C |

## Laws added 7/07 (verify in memory palace)
- POSITION is the biggest dial: a moved antenna is a NEW antenna
  (hardware epochs). WETA: 1 lifetime header → 362/2min by location.
- Assemblability cliff ≈ 16 (not 15.2): headers count below it, frames
  don't assemble. Steady-cliff has nothing to harvest; BREATHING does.
- Slips = CPU load (×8 under stress) + USB path (hub → 0 after direct).
- Concealment (TEISCRUB=0) doubles stream richness but unmarked damage
  defeats gating — scrubbed for harvest, unscrubbed for vote-merging.
- Instruments lie until their control group catches them (7 cases in
  2 days). ffprobe -count_frames hallucinates on ATSC muxes — the only
  honest frame counter is ffmpeg null-sink decode.
- GR: every output port must land somewhere (twin deinterleaver's
  plinfo needs a null_sink).

## 7/07 late-night results (the finale sprint)
- GMD/Forney erasure ladder SHIPPED (rs_decoder_erasure, STVT_RS_GMD=0
  reverts): replay A/B verdict = keep, no dB — failed codewords die far
  beyond erasure range. Honest counters: gmd_trials=/gmd_rej=.
- tv_replay is now THE decoder test rail: SOVA plane wired, STVT_IQ_SKIP
  diversity knob. ~3 min/arm, zero radio steal, deterministic.
- E7 DONE + WIN (e7_vote.py): heal-merge (splice clean same-PTS GOP
  twins from sibling decodes over damaged ones, never drop). Impulse
  specimen: 799 frames kept, decode errors 715→688. Diversity proven.
- DFE anchor retest: INDETERMINATE (rf15 specimens ~96% dead in replay;
  dead material can't discriminate EQ questions). Stays opt-in.
- TURBO_BLUEPRINT.md: stage-2 RS-truth trellis pinning = the crown-jewel
  post-Fable build (~1-1.5 dB nobody ships).

## Queue (ranked)
1. TURBO stage 2: atsc_turbo_decoder hierarchical block per
   TURBO_BLUEPRINT.md (RS-truth pinning, re-decode failed spans only).
2. Capture a MID-CLIFF ECHO specimen (15-16 dB + multipath, RF15/RF21
   breathing) — the missing testbed for DFE-anchor + GMD + turbo A/Bs.
3. SOVA 5-round default gauntlet; if passed → default-on like the guard.
4. DFE healthy-channel retest WITH guard+no-hub (its corruption may
   have been slips all along → DFE could go default too).
5. E7 live integration: panel "SECOND OPINION" button — record 60s IQ
   (iq_ring), e7_vote offline, play healed file. More passes = more
   donors; try 5. Also finer-than-GOP splice (PES-level).
6. Harvest player --follow live demo on breathing evening RF21;
   integrate as panel "HARVEST MODE" button (force-play for sub-16).
7. BO hunt completion (needs breathing; evening block attempted 7/07).
8. Rabbit re-aim session (flatness tone, RF21 target) + Philips peak.
9. GRC: env→parameters pass + example flowgraph + README screenshots.
10. Radio Tuna: adaptive survey probe, 106.5-107.9 gap, WETA-HD codec
   mystery. Dawn-score calibration accumulates each morning.
11. P1 flutter: 0.2-0.5 Hz on all antennas (foliage?); tie to τc gearing.

## Tonight (armed)
Bedtime → night_shift.py: dawn_score2 (00Z balloon) → 3-antenna cube
till 05:00 → tripwire (03:30, MER≥13.8 early-start) → ambush3 on
PHILIPS port B: guard+DFE+reseed+SOVA+warm taps, TS archiver → best
dwell becomes rf9_morning_show.ts (null-decode-verified) + one polite
announce. Panel restored at 07:10.
