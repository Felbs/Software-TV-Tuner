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

## Queue (ranked)
1. SOVA 5-round default gauntlet; if passed → default-on like the guard.
2. DFE healthy-channel retest WITH guard+no-hub (its corruption may
   have been slips all along → DFE could go default too).
3. Harvest player --follow live demo on breathing evening RF21;
   integrate as panel "HARVEST MODE" button (force-play for sub-16).
4. BO hunt completion (needs breathing; evening block attempted 7/07).
5. Double-decode voting (E7) using UNSCRUBBED captures.
6. Rabbit re-aim session (flatness tone, RF21 target) + Philips peak.
7. GRC: env→parameters pass + example flowgraph + README screenshots.
8. Radio Tuna: adaptive survey probe, 106.5-107.9 gap, WETA-HD codec
   mystery. Dawn-score calibration accumulates each morning.
9. P1 flutter: 0.2-0.5 Hz on all antennas (foliage?); tie to τc gearing.

## Tonight (armed)
Bedtime → night_shift.py: dawn_score2 (00Z balloon) → 3-antenna cube
till 05:00 → tripwire (03:30, MER≥13.8 early-start) → ambush3 on
PHILIPS port B: guard+DFE+reseed+SOVA+warm taps, TS archiver → best
dwell becomes rf9_morning_show.ts (null-decode-verified) + one polite
announce. Panel restored at 07:10.
