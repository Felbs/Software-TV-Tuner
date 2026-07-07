# NEXT_SESSION — start here (written end of 2026-07-06, Fable's last night)

## State of the rig
- Antennas: rabbit ears = ANT B (aimed, DO NOT disturb casually — position
  is part of the calibration), discone = ANT A. Philips/old-faithful NOT
  connected (one coax connector short; buy an F-connector/barrel).
- DLL: DFE v1.3 installed (STVT_EQ_DFE opt-in, anchor STVT_EQ_DFE_ANCHOR
  opt-in-and-unsafe, reseed STVT_EQ_RESEED built, erasure ceiling 16 +
  guard v2). Remember: _rebuild.bat does NOT install.
- night_shift.py = the overnight orchestrator (flutter probes → cube →
  tripwire → alternating-antenna RF9 ambush w/ DFE+reseed → panel).

## Open results to check first (morning of 7/07)
1. Ambush outcome: cube_log.jsonl events rf9-ambush / TRIPWIRE /
   ambush-done; golden specimen if hdr ≥ 20.
2. Flutter probes: flutter-probe events — did 0.54 Hz follow the antenna?
   (RF34/rabbit had a 20 dB periodic fade component, hw-AGC off.)
3. Specimens: Z:\src\magic-tv-decoder\tools\data\specimens (watcher v4
   is honest; DEAF trigger armed).
4. Reseed: grep chain logs for "QUALITY LKG reset" during ambush dwells.

## SOLVED CASE (01:50, read this first): DFE healthy-channel corruption
specimen_20260706_212651.cs16 (kept in specimens/): caught live at MER
16.87 / 56k bad. Stage-1 = CLEAN-RF. Replay of the SAME IQ with DFE off
= 17 headers, decodes fine. Corruption is 100% DFE-internal. Mechanism
candidate: confidently-wrong equilibrium — wrong decisions + feedback
make |e| small, the gate can't distinguish right from self-consistent,
fb taps absorb the lie between FS flushes. Fix directions: fb energy
cap; decision-vs-training crosscheck at each FS (fs_err spike while
data-e small = the signature); or E5's RS-fail discipline (below).
Same failure CLASS as v1.2's anchor mirage and (suspected) RF15 DEAF:
adaptation graded by a reference it can influence.

## DEAF/corruption forensics state (23:45 close — READ BEFORE E5 WORK)
Bred on demand (DFE=1, RF34, strikes in 1-3 x 60s tries). Facts:
- IQ is CLEAN-RF; same IQ replays fine with DFE off (specimen 212651).
- Deinterleaver EXONERATED: realigns steady 481/window THROUGH the
  corruption (field_syncs= in its telemetry line, new).
- Pre-RS sync-byte probe was INVALID (data still randomizer-whitened
  before RS — 0x47 only exists post-derand; control windows caught it).
- Tap surgery (dfe0+lkg) "didn't cure" — BUT command-ack lines in the
  corrupted samples' logs were NEVER verified. FIRST 2-MIN CHECK
  TOMORROW: breed + sheriff + grep "SHERIFF cmd" in the same log.
  If acks missing -> surgery never ran -> the simple theory wins (DFE
  confidently-wrong equilibrium sustains itself; dfe0 WOULD cure it).
  If acks present -> remaining suspect = viterbi 12-decoder mux/state.

## Build queue (ranked)
1. E5 — RS-fail-disciplined adaptation: ground truth for the DFE anchor
   (v1.2's mirage: self-referential fs_err; see memory + DFE_BLUEPRINT).
   NOW THREE CONVICTIONS deep (anchor mirage, DFE equilibrium, DEAF
   suspicion) — this is unambiguously the next big build.
2. Philips session (evening): swap onto B, probe RF15 (close-in echo
   escape hypothesis), full-cube column; then re-aim rabbits (flatness
   tone) when swapped back.
3. Evening cube 17:00–24:00 — the map's remaining gap.
4. 5-round DFE gauntlets marginal-only regime to tighten the conditional.
5. PHYSICS_LADDER.md P2 (METAR dawn forecasting) and P4 (FM beacon
   oracle — wants discone on port C, needs that connector).
6. Radio Tuna: adaptive survey probe, 106.5–107.9 sweep gap, WETA HD
   codec mystery (try prog 1/2, newer nrsc5 build).

## Laws learned this era (verify in memory palace)
- Time is a tuning knob; ownership flips by hour (cube_map owner_by_hour).
- Cliff = S-curve; 15.2 is half-loss; watchable = 16+ (cliff_curve.py).
- Wiener/DFE = conditional weapons: sub-cliff only, fresh only.
- Erasures: never zero-margin (≤16); histogram positions are useless
  (1 rescue / 586k) — positions must come from viterbi confidence.
- Metrics whose reference the adapting system controls are mirages
  (v1.2). Defaults need 5-round gauntlets; 2 rounds = direction only.
- Audit every new instrument's first outputs (specimen watcher: 3 false
  families in one day).
- Panel sweeper holds the SDR even when idle — bench the panel process
  for ANY chain work. Kill processes via PowerShell tool, never
  bash-wrapped powershell ($_ gets eaten).

## The scoreboard vs the ultimate goal
Rabbit ears: 6/6 channels decoded at the right hours. Discone: RF7
daytime + dawn VHF (its UHF is physics, not a bug). Channel 9: first
headers ever + measured window + tonight's armed ambush. RF15: named
disease (oscillating close-in multipath), two weapons staged (DFE,
Philips position). The tuner now chooses gain, antenna, hour, and
filter structure by measurement. Next level: the atmosphere itself
(P2), other transmitters as sensors (P4), true combining (P5/RSPduo).
