---
name: mer-dial-universal-algorithm
description: "THE breakthrough for 'tune any antenna' (2026-07-01): the chain's equalizer already computes a live MER (fs_err_rms via STVT_EQ_TELEM=1) — MER_dB = 20*log10(5.0/fs_err_rms), ATSC data cliff = 15.2 dB. Turns the binary lock-vs-decode cliff into a continuous dial: gain becomes a grid search, aiming becomes a tone, failures classify themselves. Tools: mer_meter / mer_gain_cal / config_shootout in adaptive-tv. Shipped as new GitHub main."
metadata:
  node_type: memory
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

**2026-07-01 — the answer to the user's ultimate goal ("software TV tuner that
tunes ANY antenna").** The wall was always the blindness between "pilot locked"
(`mean|x|`) and "video decodes" (seq-headers) — a binary 15.2 dB cliff with no
gradient. The dial was already in the chain: `atsc_equalizer_long_impl.cc`
computes **fs_err_rms** (RMS error vs the known ±5 field-sync training
sequence) — that IS broadcast-grade **MER**. Gated behind `STVT_EQ_TELEM=1`
(zero overhead off; prints ~5/s; already in the shipped June-13 DLL, no rebuild).

**MER_dB ≈ 20·log10(5.0 / fs_err_rms). Cliff = 15.2 dB** (validated live:
15.3-15.8 decodes with headers flowing, ≤14.3 = zero headers). Chain's own
`STVT_EQ_LKG_RMS=1.0` default ≈ 14 dB — self-consistent.

**Why it changes everything:** (1) gain calibration = grid search on a
continuous score, finds the sweet spot in ~3 min and catches overload ridges;
(2) aiming = `mer_meter --tone` (880 Hz = cliff, higher = margin) — ground
truth, unlike power/shelf/ripple which all peak on undecodable reflections;
(3) failure classification by SHAPE: MER flat vs gain = aperture-limited
(hardware verdict with a number), falls at high gain = overload, healthy MER +
0 TS = plumbing, oscillating ±2 dB = multipath fade.

**The algorithm (stage order matters):** USB multi-rate probe → interferer
census/notches → port+carrier scan (actives POWERED) → MER gain grid →
channel survey by MER (multipath is channel-specific) → recovery-config
shootout judged by quality_judge (MER stops discriminating above cliff; fps/
v_err takes over) → honest dB verdict + per-antenna profile.

**Tonight's proof (directional shed antenna, RF31):** started MER 9.5 no-lock
(dead LNA inline) → LNA out + grid recal → 15.79, TV playing, quality_judge 84
"watchable". Sweet spot MOVED with every path change (amp-only 2:36; last
night's 3:40 was for no-LNA-different-position) — **never reuse gain settings
across RF path changes, recalibrate**. Config shootout winner for cliff-edge
signals: `STVT_RS=erasure STVT_RS_ERASURES=20 STVT_EQ_QUALITY_BAD_RMS=8`
(avg 54.7 vs 43.7 plain erasure); FS_AVG_DEPTH=4 is POISON on fluttering
multipath (MER 15.7→9.4 — too slow to track); RLS also lost. At +0.6 dB margin
scores breathe 22↔84 with the fade cycle — cable quality (90+) needs ~17-18 dB
= physical (aim/position), and the algorithm's verdict stage should SAY so.

**Where it lives:** `Z:\src\adaptive-tv\` = `mer_meter.py` (dashboard + tone,
--biast), `mer_gain_cal.py` (--cells "rf:ifgr,..." fine sweeps, --biast),
`config_shootout.py` (--only/--reps for paired A/B). All pushed 2026-07-01 as
the **new GitHub main** (see [[branch-layout]]): repo Felbs/Software-TV-Tuner,
`adaptive-tv/` folded in on top of the windows branch, README rewritten,
science.md §12.5 added. Old Ubuntu main preserved as `main-linux`.

**2026-07-02 VALIDATION — attic antenna, 3 configs, PC moved into the room
(short USB, clean 8 MS/s):** the algorithm handled all three unaided.
(1) **LNA on Antenna B + bias-T** (wake +22.8 dB): overload staircase until a
sweet-spot ISLAND way out at rfgain_sel=8/IFGR=40-44 → MER 19.0-19.3, decodes,
but quality only ~26 — AND the LNA's wideband floor BURIED the carrier census
(scan showed only RF17/18/21; 17/18 were phantoms) so it steered to the one
dirty channel. (2) **BARE antenna, Antenna A**: MER 19.4-19.5 flat across the
whole grid, channel survey found RF31+RF36 clean → **quality 100/100, 56 fps,
cable-grade, PASSIVE**. (3) **Own amp**: same MER 19.5 (amplification ≠ SNR,
again), winner 3:48, quality 100 on RF31+RF36. Ranking: bare ≥ amp >> LNA.
New failure class added to the playbook: **high MER + persistent rail
transients = impulse noise** — RF21 (515 MHz) scores 48 bare vs RF31/36's 100
because the PC/USB-3 sits next to the antenna and spews ~500 MHz; rails in the
gain-grid output are the tell (10-17 on RF21 vs 3 on RF31/36). Also: a huge
shelf that never field-syncs at any gain (RF18 +28) = non-ATSC interferer,
phantom carrier.

**2026-07-02 pt2 — `tune_antenna.py` SHIPPED (commit 324e36c, pushed to
`main-universal`; `main` fast-forward awaits user go-ahead — auto-push to
default branch was denied by policy).** THE one-command orchestrator in
adaptive-tv/: sweep → per-carrier coarse MER cells with classification
(CLEAN/IMPULSE/BELOW-CLIFF/PHANTOM) → overload-staircase auto-rescue (extends
grid to rfgain 5-9 when MER rises monotonically into the low-gain edge — the
LNA-island lesson) → refine ±1 rfgain/±4 IFGR (near-ties broken toward fewer
rails) → quality judge with rescue configs when margin <2 dB → verdict +
profile JSON in adaptive-tv/profiles/. Validated cold on attic-bare: flagged
RF18 phantom, RF15 honest -0.4 dB, won RF31 3:36 MER 19.55 → 100/100
CABLE-QUALITY. 8-min soaks: RF31 avg 100 (min 79, convergence only); RF36
100/100/100 swing 0, zero overflows. `stress_test.py` now takes
--ifgr/--rfgain/--rs (hardcoded gain removed). Full bare-attic channel map in
profiles/attic-bare.json: RF31+RF36 cable, RF34 46, RF21 48 (impulse), RF15
14.7 unbridgeable (rescue configs scored 0), RF18 phantom, RF19/25 no-lock,
RF27 8.6. REPLICATION #1 PASSED same day: BJ's Philips SDV3824B/27 attic antenna
(PASSIVE — no amp hooked up), one cold tune_antenna run → RF36 2:32 MER 18.75,
100/100 CABLE-QUALITY, v_err 7.5/s (quiet win, the new QUIET_VERR≤15 early-exit
fired correctly live). Channel personalities DIFFER per antenna: Philips
decodes RF15 (18.4 — attic couldn't, 14.7) but loses RF31 (11.7 — attic's
19.5); RF18 PHANTOM confirmed on both antennas. v_err tiebreaker patch =
commit 3f09277 (main-universal; main one behind, needs user's push word).
Two antennas, two bare-wire 100s — "no electronics" thesis is 2-for-2;
next: rest of the antenna fleet.

**2026-07-02 pt3 — CHAIN-LAB (Bayesian optimization, commit 80239bb):**
`adaptive-tv/chain_lab.py` = offline Optuna/TPE search over 11 chain knobs,
each trial a bit-identical tv_replay of a frozen IQ capture (verified: same
sha16 twice), ~29 s/trial at idle priority, resumable sqlite studies in
adaptive-tv/lab/. Score = seq-headers − 0.05·ffmpeg-null-decode-error-lines.
Capture library in lab/captures/: philips rf36_strong, rf15_cliffedge (hot
2:32), rf15_cliffclean (calibrated 4:44), rf21_impulse, 30 s cs16 each; plus
the old ab_rf34_30s.cf32. **Night-1 results (~70 trials, 4 studies):**
(1) rf34 winner beat 6-week hand-tuned baseline +56% headers (207 vs 133, err
156→3), reproduced exactly. (2) UNIVERSAL knobs — in every study's top-10:
FS_AVG_DEPTH=2, GEAR_LMS=1 (slow≈1e-6), RRC_SYMS=10, EQ_BETA≈1-3e-5 (half
default), QUALITY_BAD_RMS 8-10. Production's fs_avg=1/gear-off was the blind
spot. (FS_AVG=2 on steady signals ≠ the "FS_AVG=4 poison on flutter" memory —
lab drew the regime boundary.) (3) PER-CLASS: RS erasure-7 (noise-limited) vs
erasure-18 + FPLL_ALPHA≈5e-4 (impulse — bursts are erasures, don't chase
phase hits); hand-picked erasure-20 was OVER-budgeted (passes corrupt pkts →
error floods). (4) Cliff-edge captures: no config crosses the cliff (honest-
verdict rule survives) but optimizer converts corrupt soup (101 hdrs/24k errs)
into ~30 CLEAN hdrs — two archetypes: few-but-pristine vs volume+concealment.
(5) Synthesized universal cfg (fs2+gear+rrc10+beta2e-5+qr10) beats baseline on
ALL 4 captures (190/3 vs 133/156; 242/611 vs 239/694; 155/10k vs 101/24k;
36 vs 16 score) but class winners still beat it at the edges → dispatch via
tune_antenna's classifier is the architecture. MER telemetry is NOT comparable
across configs (FS_AVG changes fs_err_rms semantics) — score by TS only.
**(6) LIVE VALIDATION FAILED — THE NIGHT'S BIGGEST LESSON.** The universal
cfg that beat baseline on ALL frozen captures scored **0/100, 0 fps live** on
the same RF36/Philips that plays 100 with baseline (massive AC3/TS corruption,
chain locked, full mux rate). Classic overfit-to-the-test-set: a frozen 30 s
replay is STATIONARY — no breathing multipath, no drift — so configs that
slow adaptation to exploit stationarity (GEAR_LMS with BETA_SLOW=1e-6 = 50×
slower tracking, FS_AVG=2 halving adapt rate) win in replay and collapse on
live dynamics. **RULE: chain-lab replay is a SCREENING stage only; every
candidate must pass quality_judge LIVE before shipping** (same discipline as
[[no-push-linux-without-test]] but for configs). Also suspect RRC_SYMS=10
interacts. Mitigations for lab v2: longer captures (2-3 min, catch a fade
cycle), score = worst-window not whole-file, penalize slow-adaptation knobs,
and a mandatory live-confirm stage in the loop.
NEXT: isolate WHICH knob kills it live (one-knob-at-a-time live A/B: fs_avg=2
alone, gear alone, rrc10 alone — each cheap vs baseline), wire per-class
dispatch into tune_antenna, more trials on rf36_strong study,
re-record cliff captures as 2-3 min takes with true 15.0-15.2 dB margin.
A/V-sync gotcha: lab replays hammer Z: disk → live TV player pipe desyncs;
pause TV or move lab I/O when watching. `main` pushed to 80239bb late
2026-07-02 (v_err tiebreaker + chain-lab) — main and main-universal in sync.
ALSO: sweet spot drifts with TIME OF DAY (RF36 2:32 perfect afternoon →
overload garbage at night, 3:40 restored 100).

**(7) THE DRIFT ANSWER = HARDWARE AGC, not a watchdog (00:15 2026-07-03).**
User: "the math should be so perfect we rarely need the watchdog" → found
`STVT_SDR_AGC=1` already in tv_live.py (added 2026-05-22 for EXACTLY this,
"hardware AGC adapts in microseconds", default OFF) — the whole MER-era ran
fixed-IFGR snapshots and fought the drift that created. ARCHITECTURE REFRAME:
IFGR is a LEVEL knob (drifts with sun/weather → give it to the silicon servo);
rfgain_sel is a REGIME knob (headroom vs sensitivity, changes only with the
RF path → MER grid still owns it). VALIDATED: with STVT_SDR_AGC=1, cells
2:32/3:40/4:48 — afternoon-overload to starved — ALL converge to MER
18.8-18.9, in_rms pinned ~54, headers 65-82. The IFGR axis of calibration
vanishes. tune_antenna v2: grid collapses to rfgain_sel (× maybe
agc_setpoint), watchdog demoted to rare regime-change detector. Setpoint sweep: -30 thin (in_rms 55, rails 14), **-20 = the invariant**
(MER 19.05, in_rms 177, rails 3), -14 near compression (18.66/349/2).

**(8) NIGHT ANTI-GLITCH CAMPAIGN (00:30-00:50 7/03):** night survey (servo
-20 levels every channel to ~170 — fair cross-channel compare): RF36 18.38
rails 9 / RF34 17.79 rails **1** (night's impulse-clean channel) / RF15 17.44
/ RF21 16.60 / RF31 12.2 below-cliff at night too. Live paired shootout:
**erasure RS 7 live-CONFIRMED on RF36** (v_err 26.0 vs baseline 29.6, both
reps) — first lab finding to pass live validation; shipped in tonight's
viewing config (RF36 + servo -20 + erasure-7). **quality_judge BUG found:
score assumes ~60 fps** — RF34 prog 3 is 1080i/29.97 and scores 30 at 25 fps
despite MER 18.5; judge v2 must normalize to the stream's native rate before
RF34 can be fairly ranked. CC-ORACLE idea (user's): CEA-608 captions =
known-structured content-layer data (per-byte parity + English words) →
cc_integrity metric for judge v2, third truth layer (MER=channel,
fps/v_err=decoder, captions=content); also wire caption RENDERING into
play_marginal/mpv (user noticed subs don't show — track never selected;
tv_tuner --cc is the supported path). **(9) OVERNIGHT LIVE CAMPAIGN (00:46-06:49 7/03) — `live_tune.py`, 834 LIVE
trials, the lab v2 that matters.** Each trial = real chain on real RF,
scored by quality_judge (fps normalized per mux native rate — the RF34 60fps
bug fixed in-scorer) + CC probe (CEA-608 extract per trial; ffmpeg movie
filter chokes on Win drive colons → run cwd=lab with relative path).
**WINNER (obj 992.8, v_err 5.2/s a_err 1.0 full-rate — cleanest ever):
RF36 rfgain_sel=4 AGC setpoint=-20 erasure RS 6 quality-reset 8
fpll_alpha=0.002.** Top-20 consensus: setpoint -20 (19/20), erasure 6-8
(19/20), quality-reset 8 (17/20), **rfgain 4 (15/20 — QUIETER than the
hand-picked 2-3; less front-end gain wins at night)**, fpll_alpha split.
Per-channel: RF36 992.8 / RF34 975.1 (fps-normalized redeemed it) / RF21
743. CC oracle live-proven: 455 caption-bearing trials, read morning news
("MTGEROMY" = Montgomery, caption-visible corruption even in 0.92-ratio
trials → v2 should score dictionary-word validity, not letter fraction;
♪-lyric segments false-flag as corrupt). TV auto-launched on winner 06:49,
judge 100/100. User will name this release **"TV Tuna"** (speech-to-text
accident turned mascot) once morning-validated.

**(11-CORRECTED ⚠️ — the "glitch killer" was a METRIC MIRAGE (7/03 ~11:00).**
The celebrated "NB th=2.0 → 0 gaps/min" result was measuring an EMPTY STREAM:
at threshold ≤2.0 the blanker clips 8-VSB's own modulation and the chain
emits full-rate 100% NULL-PADDING output (231 MB audited: 0 seq headers,
0 real PIDs) — and the CC-gap metric, which excludes null packets and had NO
LIVENESS CHECK, scored nothing-at-all as perfect. User's "nothing playing"
during the whole NB era was the correct observation; the 395s-clean-live-
session claim, the README/science 'cure', and the 87512be commit story were
all wrong and have been RETRACTED in-repo. The "SDR firmware wedge" diagnosis
+ USB replug were ALSO wrong — every FATAL was NB null soup. What IS true:
missing-packet class is real (control runs: 24-26 CC-gap bursts/min, all
PIDs at once, RS/TEI zero — glitches are packets never emitted, uncorrectable
by RS/EQ/gain); blanker at th 2.2-3.0 trims gaps ~15-30% (modest, real);
tv_up.py's header-based wait_decode caught the mirage 3× (the self-verifying
bot works). LAWS: (a) EVERY metric needs a liveness denominator — a zero is
meaningless until real video packets are confirmed; (b) celebrate only after
the EYE or a decoded-frame metric confirms; gaps/min pairs with header-rate,
never alone. live_tune NB space now [0, 2.2, 2.5, 3.0].
**BLANKER FINAL VERDICT (12:02 7/03): dead end.** Units insight: threshold
multiplies MEAN envelope; 8-VSB legit peaks = 3-4× mean (thr≤3 amputates the
signal itself — the whole ≤3.0 test region was inside the modulation).
Corrected sweep at 3.5/4.5/6/9 (liveness-guarded, tv_lab phase A): all
harmless, NONE reduce gaps (20-29/min vs baseline 20.4 — pure breathing
noise). Conclusion: impulses arrive pre-smeared by SDR front-end filtering;
no sharp spike survives to sample-domain blank. Software impulse mitigation
at this chain position is EXHAUSTED — remaining fixes are physical (antenna
away from impulse sources / more margin) + time-of-day. tv_lab.py = the
autonomous campaign (phase A honest sweep → phase B delivery A/B by mpv
avsync → phase C closed loop with cable criterion gaps≤3 + hdrs/s≥4.5 +
avsync≤0.3 held 20 min, self-healing incl. avsync-bounce + CC re-cycle).

**(12) PLAYER SAGA RESOLVED — file-native tv_watch (7/03 afternoon, cf84a7d).**
The tail→ffmpeg→mpv PIPE was the source of the all-day lips/CC/freeze
complaints: pipes can't seek, so stream damage stalls video while audio
sails → av_bench (new apparatus: sample mpv avsync every 5 s, fit drift)
measured **+3.355 s/min RUNAWAY (+17 s after 4 min)**; its ffmpeg remux also
dies on one bad audio packet, and a 1-2 GB live.ts strangles the tail.
**tv_watch.py plays the growing FILE: seekable → watchdog hops to the live
edge on stall/lag>12 s, resetting A/V AND stuck CC in one motion; tracks
picked by program-id via IPC track-list. Benched on the same damaged
stream: median A/V +0.001 s, drift +0.009 s/min (373× better).**
lipsync_daemon (audio-delay servo) obsolete — teaspoon vs runaway. Stack:
tv_hunter (channel-hopping quiet-chaser, SD/HD tier, floor_history.jsonl)
→ tv_watch. USER IDEAS bank: custom compression → receiver-side forms:
(a) smarter concealment / neural inpainting of damaged frames = next-gen
roadmap item; (b) joint source-channel decoding with MPEG structure as
priors = far-frontier science entry.

**(13) DISCONE RETEST + SESSION CLOSE (7/03 afternoon, PC shutdown for
antenna work).** Discone on Antenna C, honest verdict: NOT decodable today.
FM control = port/cable/antenna ALIVE (90.9 +20 dB, 97.1 +16; WTOP 103.5
OVERLOADS even at IFGR 40 — shelf reads negative when clipped, don't misread
overload as dead; my 2/6-alive threshold was too strict). VHF-hi: **RF9 the
only real carrier, +10 dB shelf ≈ 5 dB under cliff** (fs-gate rightly refuses);
RF7 (June-22's clean lock) reads +0.9 = GONE — discone was repositioned for
spectrum-scout + propagation differs. UHF on C: energy but no ATSC (rolloff +
polarization confirmed). **GRID-SPAN LESSON: passive antennas on lossy ports
need MAX-gain cells (0:20-1:32) — the UHF-strong grid (2:32+) read in_rms 1-4
and looked like dead input. tune_antenna v2 must span the whole gain range
when antenna class is unknown.** TROPO PLAN for RF9: July = prime mid-Atlantic
tropo; inversions on calm clear nights, golden window 4-8 AM (impulse floor
also bottoms then); levers = height (omni gains fast with elevation) + ~$15
FM bandstop trap (kills WTOP intermod, allows more gain). Offer stands: leave
discone plugged overnight → 30-min RF9 MER watcher logs the propagation curve.
GOTCHA: duplicate tv_hunter ghosts — kill patterns must include tv_hunter
(v1 survived v2's launch and they fight over the player).

**NEXT SESSION (user shutting down + rearranging):** the shed DIRECTIONAL
antenna moves to the ATTIC (decided: antenna up, not PC down — its June
blocker was window multipath; attic = height + clean path + short USB).
Rules: ITS OWN AMP powered, NOTHING else inline, aim S/SW at DC towers,
Antenna A. Run `tune_antenna --name directional-attic`, then the three-way
attic shootout (Philips vs old-attic vs directional, same sky). Viewing
stack = tv_hunter → tv_watch. Cable window was NOT achieved 7/03 daytime
(floor 17-31 gaps/min all day); evening/pre-dawn are the windows.
**REPO: main-universal = cf84a7d, main = 80239bb (6 validated commits
behind: retraction, tv_up, rotation fix, tv_lab, housekeeping+adaptive
stack, tv_watch+av_bench) — needs user's "push it" word.**

**(14) DIRECTIONAL-IN-ATTIC SESSION (7/03 evening, PC shutdown #2).**
Profile `directional-attic.json`: amp ON (shelves RF18 +22/RF15 +19.8/RF31
+18.6), rotator motor DEAD so aim = rough. Coarse survey all below cliff
(RF31 15.06), but **FINE cells rescued it: 4:40 = MER 15.28 (+0.08!) — when
a coarse survey reads -0.3..0 vs cliff, ALWAYS fine-sweep before verdict**
(tune_antenna v2 should auto-refine near-misses). FORCED-VIDEO mode works:
4:40 + erasure20+QR8 + tv_watch "marginal" arg (show-all/no-skip flags,
commit 260efd4) → watchable staticy TV at +0.08 dB, user: "not bad all
things considered". CC GAUNTLET (all channels, cliff config, caption-read
as content proof): RF31 = 3.5 hdrs/s, 67% real, 1 gap/min, CC read
"awprnsnd as." (11 chars @ 0.91 wordness — TV whispering through static);
RF15 0.1 hdrs/7%, RF21 0.4/9%, RF34+RF36 NO LOCK (out of beam — the
directional is a ONE-CHANNEL TV as aimed; RF18+RF17 = phantoms, 4th antenna
confirming). GOTCHA: PowerShell Start-Process -ArgumentList SPLITS "Antenna
A" — must embed escaped quotes (`"Antenna A`") or the tool dies instantly on
argparse (two silent tone-meter failures — "user hears nothing" can mean
the process never ran). SDR THERMAL: ~30 h continuous duty + July + user
felt it hot → idled for cooldown; RSPdx case IS the heatsink (warm=normal,
finger-test >60°C=real worry, hot SDR drifts more). **PENDING NEXT SESSION:
(a) rf31_cliff_lab.py (copied to Z:\src\adaptive-tv\) — 9-config cliff-
regime sweep (erasure 7/14/20/stock below cliff, FAST-LMS beta 1e-4/2e-4
on flutter hypothesis, qr6, fpll 2e-3, SPS1.5) — ABORTED for heat, ZERO
data yet, rerun on a cool radio; (b) attic antenna swap back (Philips or
old-faithful, Antenna A) + evening cable-window session via tv_hunter;
(c) cc_gauntlet.py also saved to Z:\src\adaptive-tv\.** Claude Code update
note: npm install method, EPERM cleanup warning = harmless (locked exe),
2.1.200 was already latest. Repo: main-universal = 260efd4; main still at
80239bb (7 commits behind, needs push word).

**(15) EVENING 7/03 — PHILIPS DAY-2, THE CONTROL PANEL, WORLD CUP NIGHT.**
Philips re-profiled (`philips-day2`): RF36 fresh-cal 3:40 → **100/100 @ 0.58
v_err/s, 0 a_err — cleanest measurement ever**; RF15 18.35 but judge 40-55
w/ v_err 276 (flutter + judge-fps quirk); RF31/21 below cliff; **tune_antenna
v2 BUG: top-5 shelf cutoff let phantoms (RF17/18) crowd out known-decoder
RF36 — never let phantoms outrank proven channels**. Full scan: **6 muxes
locked = ABC(RF7)/CBS(RF9)/NBC(RF34)/ION(RF35)/Univision(RF15)/Fox(RF36);
42 stations, ALL channel-shared onto 6 towers** (Telemundo WZDC 44.1 = RF34
prog 5 INSIDE NBC's mux! CW 50.1 + WDVM 25.1 inside RF15). EPG was 174h
stale — re-scan refreshed; grid→DVR flow VALIDATED (add-show "2026 FIFA
World Cup" scheduled 5:45PM 135min from EIT by name). Telemundo carries
World Cup with **eng SAP + spa main — the language-toggle test content**
(# in mpv). VHF MYSTERY (open): scanner LOCKS RF7/9 (read PSIP!) but play
chain starves at every gain tried (in_rms ≤18 even 0:20) — find+adopt the
scanner's lock recipe. **tv_tuna_panel.py (localhost:8642, commit 0debe8b)**:
GUIDE tab (grid buttons tune via tv_live+tv_watch solo, REC via add-show,
tuneability badges incl. VHF '~' guard, signal bars+dB) + **STATS FOR NERDS
tab** (knob cards, live MER/in_rms/max|x|/gaps from panel_chain.log telem,
UHF waterfall 473-611: 2s/row, scroll-zoom/mid-drag-pan/dbl-click-reset,
channel directory overlay w/ callsigns + subchannel segments annotated as
TIME-shared not frequency-shared, yellow tuning cursor + 6MHz edge lines).
GOTCHAS: panel sweeper must YIELD SDR (checks live.ts mtime <6s; it silently
starved a cal run); click-debounce via GEN counter (4 stacked players);
Start-Process -ArgumentList still splits "Antenna A"; SD-subchannel tune-in
lag ~45s (extractor fill), player freeze self-heals via watchdog seek.
NEXT-ANTENNA SHORTLIST: (a) old-faithful more testing (user said), (b)
discone RF9 tropo window 4-8AM, (c) directional re-aim w/ tone when rotator
fixed/ladder weather, (d) Philips VHF recipe hunt. Tonight: DVR fires 5:45PM
World Cup RF36 135min (daemon armed, PC stays ON, 5:43 alarm frees tuner).

Original (falsified) entry kept below for the record:
**(11-RETRACTED) THE GLITCH KILLER — NOISE BLANKER th=2.0 (midday 7/03).** User's
"maybe you're looking in the wrong place" cracked the case the metrics never
could. Chase: recording-vs-live test (glitches in the RECORDING → stream, not
player pipe) → TS forensics: **RS/TEI damage = ZERO** (v_err was partly the
known ffmpeg-lies phantom; the TS was never corrupted) → but **CC-counter
analysis: ~24-26 delivery GAPS/min hitting ALL PIDs at once** = whole mux
slices NEVER EMITTED. Mechanism: impulse spike → sync/FS-gate drops out
cleanly for a fraction of a second → packets never exist → invisible to MER,
RS, erasure, every config (can't correct packets that were never emitted) —
the rails and the gaps are the same events. FIX: `STVT_NB=1
STVT_NB_THRESHOLD=2.0` (atsc_noise_blanker — in the codebase all along,
default-off, NEVER enabled in ANY test incl. the 834-trial campaign).
**Result: 26.2 gaps/min → 0.00, reproduced ×2 + 395s live session with ZERO
missing packets.** Knee is RAZOR sharp: 1.8 = blanks the signal itself (no
decode), 2.0 = perfect (8-VSB crest ≈ 2×RMS; blanking above it removes only
impulses), 2.2 = barely helps (19/min). Threshold is a ratio to tracked RMS →
should transfer across gain/level, but VERIFY per antenna (a 1.8-like
overshoot kills everything). **gaps/min (CC-discontinuity bursts) is the new
king metric** — measures exactly what the eye sees; judge v_err partly
phantom on multi-program TS. NEW LAB GOTCHA: nb_ab first run measured a STALE
live.ts because a leftover chain held the SDR and unlink failed silently —
kill stragglers + fail loudly on unlink in every harness.

**(10) MORNING 7/03 — NOSTALGIA CASE CLOSED + THE RECAL LAW IS ABSOLUTE.**
NOTE: the Philips IS mounted in the attic (corrected assumption — daytime
impulse comes through house wiring/atmospherics, not desk proximity; next
lever = re-aim Philips with mer_meter --tone, its RF31 blindness suggests
mis-aim). Old attic antenna re-profiled BARE (`attic-classic.json`): RF36
100/100 @ 6.2 v_err/s at 10 AM (best daytime ever), RF31 back (18.5, 100 w/
erasure+reset), RF18 phantom on 3rd antenna. tune_antenna v_err-tiebreaker
logic worked live. THREE-WAY A/B same hour: TODAY-CAL 5.8/s beats LEGACY-JUN
17.8 beats overnight CHAMPION 32.7 (!) — **every stored config goes stale in
hours; fresh calibration always wins; the product is the recal loop, not any
config** (proven at sunset, dawn, mid-morning). SPS 1.1+RRC8 vs 1.5+stock:
DEAD TIE live (18.1 vs 18.2) — the CPU trim was free; archive's crystal-clear
wasn't oversampling. AMPED 4-WAY (user re-attached old amp): ARCHIVE-exact
config (59/5, sps1.5, tau50, hard vit) = **0/0 both reps, cannot decode at
all today**; best amped = SERVO 26.3 vs LEGACY-JUN 30.1 vs FRESH-CAL 42 —
ALL 4-7× worse than same-antenna BARE (5.8). **The old amp is now a pure
liability; bare wins on every antenna tested.** CC startup-gibberish bug
fixed: play_marginal --cc adds +discardcorrupt so the 608 decoder never eats
mid-sequence bytes from pre-keyframe frames (was painting stuck garbage like
"HEIH@WT...SHERRI..." until sub cycle). play_marginal --cc renders captions
(sub-create-cc-track + sid=1). UNCOMMITTED: play_marginal --cc + discard
patch. NEXT: wire auto-recal loop (fresh cal at tune-in + MER-sag re-probe)
as the actual TV Tuna centerpiece; re-aim Philips; push main + tag. Uncommitted at time of
writing: live_tune.py (new), config_shootout erasure-7 candidate, tv_live
STVT_AGC_SETPOINT patch.

**(16) 2026-07-04 — RABBIT-EARS DAY: PANEL v4 (AGC-EVERYWHERE + SIGNAL FINDER)
+ LEAKY-CABLE LAW + RF15 REPEAT COLLAPSE.**
RCA ANT111E rabbit ears (indoor dipole, Antenna A, desk height): tune_antenna
verdict aperture-limited (best MER 4.8, −10.4 vs cliff), but USER HAND-AIMING
via the new SIGNAL FINDER hauled RF34 4.3→15.1 and RF15 to 16 (+11 dB by
hand!) — scan then locked RF15(7p)+RF34(6p). Never decoded sustained video:
desk-height sweet spots are inches wide and EVAPORATE in minutes (16→13→10,
multipath breathing + evening drift); even erasure+tap-guard+forced-video got
nothing — 3 dB short is 3 dB short. Verdict: locks when aimed, needs HEIGHT
(user buying coax extension — extend RF, never USB). PANEL v4 SHIPPED
(tv_tuna_panel.py): 📡 SCAN button (streams scanner progress, pipes region
"1"), milestone-gated tune progress bar (proven stages, not timers), 🎯
SIGNAL FINDER tab (big red→green MER + browser WebAudio tone + 🏆 peak-hold
+ 🔔 new-record chirp; /api/meter runs chain-only, /api/carriers lists ALL
scan carriers sorted by SNR), AGC-everywhere (STVT_SDR_AGC=1 setpoint −20 in
base_env — aiming no longer invalidates gain cal; Philips first AGC run MER
19.2 perfect), auto CLIFF-MODE (tune measures MER 12 s post-lock; <16.5 →
one chain restart with erasure-RS-20 + BAD_RMS=8 + tv_watch "marginal"
forced video), scan-dud guard (0-lock scan can't clobber a good map — fired
TWICE same evening), flight recorder (10 s JSONL: mode/rf/knobs/MER/gaps →
lab/flight_recorder.jsonl), tower-grouped grid (per-TOWER % + pilot dB +
"63,000× the static" + level/floor dBFS + edu legend; % is honest because
subchannels share the mux), 6-bug batch (stale METER on stop/scan freezing
sweeper, false-75% from stale live.ts → growth-gated, phantom "watching" →
red PLAYER-failed banner + ⚠ under-cliff warning, scan clock freeze, median
MER vs startup transients, chain-crash detector catches Traceback/no-RSP).
μV question answered: SDR counts are gain-relative — absolute μV/dBm needs
calibration (fake precision otherwise); honest units = pilot-vs-floor ratio
+ dBFS levels (floor rising between scans = noise came to YOU — the
unpowered-LNA tell). PHILIPS RE-VALIDATED post-swap: stress circuit 4/4
muxes (Fox 19.1 MER champion, 15-min soak stable, evening gaps ~7/min), but
**RF15 collapsed mid-settle AGAIN (17.8→6.6, in_rms 222) — 2nd time, it's a
channel-specific house quirk, prime flight-recorder case study**. Stress
harnesses: tv_stress.py (hop circuit + soak via panel API) and tv_stress90.py
(5-phase 90-min science campaign: baseline / endurance / RF15 lab / RF36
program hops / evening-drift repeat + latency per tune + auto service-restart
recovery). Wedge law extended: first radio session after ANY hardware
handling is cursed (init-fail OR opens-but-zero-samples OR "no available RSP
devices") — restart SDRplayAPIService and expect a dud attempt #1.
90-MIN CAMPAIGN RESULTS (17:45-19:14, ZERO failures/interventions, log
lab/stress90_20260704_1745.jsonl): (a) **RF15 collapse is TIME-OF-DAY** —
dead all afternoon (MER 17-18, hdrs 0, retune didn't help, max|x| spikes
0.42-0.54 = impulse class) then PARTIAL VIDEO at 19:00 (1.6 hdrs/s, 55%
real, tune took 72 s = cliff-mode engaged+paid off); assassin switches with
the clock → appliance/HVAC-like, NOT pure USB hash (constant), though 479
MHz USB3-band proximity may set baseline fragility; flight-recorder stakeout
= the follow-up. (b) **evening drift is PER-TOWER**: same hour Fox 19.0
MER/0 gaps while NBC gapped 24/min — channel choice by live health beats
any fixed favorite. (c) NO runtime rot: 30-min Fox soak + ION final soak
flat 18.3-19.2, no A/V walk, no leak. (d) extractor 4/4 RF36 subchannels
clean solo video; cold-tune latency 36-52 s (repeat ~36 s) = warm-start-EQ
prize benchmark. Panel v4 pushed as 172369a (main + main-universal);
science.md USB chapter = cea8be9.
**(17) 2026-07-04 EVENING — VHF MYSTERY SOLVED: IT WAS THE DAB NOTCH ALL
ALONG.** Evening-lab VHF probe showed RF7 in_rms flatlined at 16.5 (10× below
UHF's 150-180) with max|x|=0 — filter-shaped, not propagation. DAB band III
= 174-240 MHz = EXACTLY US VHF-hi (RF7-13, 174-216); STVT_DABNOTCH=1 (always
on in play-path env since forever) was a ~20 dB in-band notch amputating our
own TV channels — scanner locked VHF whenever its env lacked the notch,
play chain starved at every gain: the exact months-old symptom. PROOF: RF7
with DABNOTCH=0 → in_rms 16.5→170, EQ converged fs_err 0.88 (MER 15.1), and
the FIRST VHF VIDEO IN PROJECT HISTORY: 7.1 WJLA ABC via full panel path
(cliff-mode erasure + forced video) = 2-3 hdrs/s, 38-43% real, only 3
gaps/min at MER 14.85 on the PHILIPS (a UHF antenna!) at 8:35 PM. FIX
DEPLOYED: base_env DABNOTCH = 0 if rf<14 else 1; run_scan forces DABNOTCH=0
(scan must hear VHF — today's scans failing to lock RF7/9 were notch-blinded
too); JS VHF tune-block removed; stvt_epg VHF '~' guard removed (lock='+').
LAW: **never leave a region-specific notch in a universal signal path —
audit every "harmless" default against the local band plan.** Rabbit ears
(true VHF dipole) + height rematch = the follow-up; also re-scan post-fix
to get RF7/9 into the grid with callsigns.
**(18) 2026-07-04 LATE NIGHT — RABBIT-EARS PROOF OF CONCEPT: CLEAN TV.**
Ears in ATTIC via 25 ft RG6 (coax loss ~1 dB = noise): 26.1 WETA-HD (PBS,
RF31) played CLEAN — MER 18.2, 98% real, gaps 6/min, no cliff-mode needed,
through July-4 fireworks. RF31 grid identity = WETA PBS (26.1-26.5), a mux
NO other antenna ever locked — antenna #5 conquered, and it brought a new
network. Aim sessions: finder+headphones hauled RF36 10.9→12.7 through
fireworks (no decode; RF36 fussy at this aim, RF31 fine — PER-TOWER aim
sensitivity again); RF7 stays the VHF holdout: 52-56 dB carrier, won't lock
even notch-free = polarization/multipath, not strength (ears geometry =
tomorrow's boss fight). July-4 lesson: fireworks = hours-long broadband
impulse barrage; scans found 0 locks at 21:15 (dud-guard saved map #3),
11 hot carriers by 23:00 as barrage faded — NEVER scan during fireworks.
SHIPPED THIS BLOCK: 🌐 ALL TOWERS chord mode (per-tower pilot-hopper
2 MS/s, one WebAudio oscillator PER tower — unison note = fair spot; tone
follows WEAKEST tower; per-tower live bars; /api/balance; balance yields
to scan/tune/meter — forgot scan first, wedged service, fixed:
stop_tv clears BAL); AIM-WHILE-WATCHING (/api/meter falls back to live
chain telemetry via cheap chain_math() when TV plays — 🔴 LIVE badge,
tone+MER during playback = the authentic rabbit-ears workflow); tone-
ringing fix (meter released → oscillators silenced after 5 null polls);
CC startup-gibberish fix FINALLY PORTED to tv_watch (+discardcorrupt on
mpv demuxer + first sub-cycle at 20 s — was only in retired play_marginal);
volume fix (AC-3 dialnorm quiet → mpv --volume=130 --volume-max=200);
tv_overnight.py (waits for 6-min radio quiet then hourly circuit+soak
until 07:30 with service-restart recovery). Chrome automation drove the
panel UI (tab clicks, tone toggle) when user was headphones-on-ladder.
FINAL RABBIT-EARS GRID (22:00 scan, post-fireworks): 23 stations / 4
towers — RF34 NBC+Telemundo, RF36 Fox+WDCA, RF31 WETA PBS, and **RF21 =
BALTIMORE-market mux (MPT-HD Maryland Public TV 22.1-.4 + NHK World +
CW as 54.1 WNUV channel-share) — TV from a different CITY, never locked
by any antenna before**; second never-before-seen mux of the night.
LIVE DECODE CONFIRMED ×2: WETA 26.1 CLEAN (18.2/98%) + NBC 4.1 marginal
(15.0, 69% real, 3 gaps/min — user watched fireworks coverage on it).
Overnight campaign ran from ~22:35 (30-min grace after user's last watch)
to 07:30 — morning job: read lab/overnight_*.jsonl + flight recorder
(RF7 unlock hunt hour-by-hour, dawn window, per-tower drift curves).
Next session: morning report, RF7 geometry boss fight (52-56 dB carrier,
polarization/multipath), fresh daylight scan (fireworks-free), possibly
push rabbit-ears findings into science.md antenna gallery.
**OVERNIGHT CAMPAIGN VERDICT (13 cycles 22:40→07:30, ZERO failures, log
lab/overnight_20260704_*.jsonl):** NBC 4.1 = the rock (17.0-18.4 MER,
98% real, decoded ALL 13 cycles); WETA 26.1 = night bloomer (evening dip
to 10.8 healed by midnight → 18.4-19.1 rest of night, several 0-gap
readings — evening propagation transient, not damage); Baltimore MPT
22.1 decoded nearly every cycle 16-18 MER but long-path flutter (gap
bursts 9-45/min swinging hourly); Fox 5.1 = this aim's sacrifice (11-14.7
all night, rarely decoded — the ear geometry that bought WETA/Baltimore
cost RF36); RF7 FLATLINED 6-8 MER every hour incl dawn while carrier
stayed huge → time-of-day and interference RULED OUT, pure
geometry/polarization → echo X-ray (H2) is the confirmed weapon. Soak
pick bounced NBC/WETA/MPT across the night = 13-cycle proof that BEST
CHANNEL IS A FUNCTION OF TIME on marginal antennas → 6th hypothesis:
health-aware channel routing ("this show also airs on the feed that's
healthy NOW"). PMT-verified track selection + CC/volume fixes staged
uncommitted in tv_watch.py — commit with morning batch.
**(19) 2026-07-05 MORNING OF INSTRUMENTS (pushed 9fd646e).** H1 impulse
freeze: implemented in atsc_equalizer_long (STVT_EQ_IMPULSE_GUARD, batch
pre-error vs 2.5×EWMA, freeze skips update+leak+LKG/gear/quality), A/B on
NBC = SAFE (MER identical) but UNTESTED (3 freezes — Sunday AM too clean;
apparent gaps diff was rising-morning confound); H1 step 3 HUBER
confidence-weighted LMS drafted+installed (STVT_EQ_ROBUST, per-sample
clamp at HUBER_K×EWMA-of-batch-MEDIAN|e|, knobless-ish, telem hub=/clmp=);
3-WAY TRIAL (OFF/FREEZE/HUBER, mirrored order, ab3_impulse.py) SCHEDULED
2 PM impulse hours. H2 ECHO X-RAY shipped: PN511 correlation in adaptN
(STVT_EQ_CIR, coherent √N avg over 16 FS → floor −39 dB, [cir] telem
lines) + panel echo viewer in finder; FIRST-LIGHT LESSON: unaveraged CIR
showed phantom 17-25µs "terrain echoes" that √N-averaging KILLED (noise
artifact — averaging is mandatory); NBC real channel = mild (+0.56µs
−22.6 dB echo). H4 IQ TOOLS shipped: iq_capture.py (cs16, fixed gain,
continuity metadata) + iq_autopsy.py (pilot dynamics / ripple-vs-time /
pilot AM) → **RF7 MYSTERY SOLVED: pilot INNOCENT (steady, ±7 Hz, no
fades, 49 dB single line in 2 Hz zoom) but data band has 27.6 dB median
BREATHING multipath canyons** — VHF standing waves in the attic; linear
EQ can't invert deep nulls → cures = move ears ~1-2 ft (VHF λ≈1.7 m) /
DFE (H3, now has a recorded test-bench capture iq_rf7_*.ciq) — months-old
mystery closed by elimination in one morning. FLATNESS METER shipped:
panel mode toggle 📶MER/📏FLATNESS, no-lock aiming dial = instantaneous
in-band ripple (8-FFT snapshots, median — time-averaged spectra read 2×
optimistic because canyons BREATHE; autopsy single-shots read ~9 dB
pessimistic from noise texture); RF7 live = ~18 dB, zones <8 flat / 8-15
wavy / >15 canyons. Aiming toolkit now spans: FLATNESS (no lock) → MER
tone (cliff) → echo X-ray (locked) → ALL TOWERS chord (fair spot).
Wedge ritual reconfirmed: killed sweeper leaves dead session → restart
SDRplayAPIService. NEXT-PROJECT VISION (user): port adaptive method to
HD Radio NRSC-5 (nrsc5 decoder, BER dial), Meteor-M LRPT, DRM — "Radio
Tuna": swap decoder, keep philosophy.
**(20) 2026-07-05 AFTERNOON+EVENING — STRIKES TRIED HONESTLY, DISEASE
MULTI-SITE, RADIO TUNA BORN.** Viewing batch d4eb34c: same-mux hops
14-32s (kill_watch only, chain untouched), English-by-RULE (tags first;
blind aid=1 caused Spanish NBC; index-mapping caused FRANKENSTEIN
True-Crimes-video+Telemundo-audio → PID mapping -map 0:i:PID is the only
context-stable reference), CC loaded-but-hidden instant toggle.
RF7 midday arc (e87f1c2): flatness-aim 18.3→7.9 ripple → first EQ lock
on rabbit ears → first VHF cal (GAINS[7]=(5,32); mer_gain_cal had
hardcoded DABNOTCH=1 — VHF cals were blind); extraction ladder
full→video-only (cliff AC-3 damage makes copy-mode ffmpeg write NOTHING
— July-3 RF15 same signature); voice aiming (flat_tone v3, SAPI speaks
MER + down-from-best); body = 2-3 dB VHF reflector, judge two paces back.
3-WAY 2PM: H1 KILLED — FREEZE fired 0× in 30-gap weather (impulses
don't corrupt FS training; they kill packets upstream), HUBER 4% clamps
zero effect; retire FREEZE, HUBER opt-in. STRIKE 1 FLYWHEEL: v1 nulled
the stream (coasted 8× on startup noise; liveness law caught mirage #2);
v2 warmup gate (80 clean 313-spaced FS) → SAFE (97.6% real thru 14
coasts). 4-ARM 6-7PM (OFF/FLY/ERAS/BOTH mirrored): 27.35/26.35/28.35/
26.25 — nothing beat control; FS-miss ≤19% of gaps + coasted fields
still carry damaged bytes ⇒ GLITCH IS MULTI-SITE. WARM-START tap cache
shipped (per channel+antenna, periodic force-kill-proof saves). GAP
PROFILER shipped (attributes each CC burst to FS/RAIL/EQ/NONE): v1
blinded by stale-live.ts offset (ALWAYS unlink live.ts before following
a fresh chain); 120-min soak found 8 PM NIGHTTIME IMPULSE STORM
(50-58 gaps/min after a pristine 7-8 PM) with 98% NONE attribution ⇒
disease lives in the UNINSTRUMENTED viterbi/deinterleave/RS region —
their metrics exist as internal stream tags, never printed; NEXT
INSTRUMENT = viterbi_metric + RS-fail counters to stderr, re-profile.
FINAL MAP (120 min, 2231 bursts, 18.6/min): FS 2% / RAIL 0% / EQ 0% /
NONE 98% — the day's strikes addressed 2% of the disease; the blind
spot IS the battlefield.
Rescue battery on RF7 capture: 6 configs ALL zero (27 dB canyons kill
PN511 detection itself) → PN limits env-ified (ATSCPLUS_PN511_LIMIT/
PN63, rescue may accept risky syncs offline; built, install pending).
Discone evening: UHF best 11.3; RF9 = 19.4 dB canyons, cal 12/12 cells
0.00 — unlockable at night, tropo dawn = only hope; characterizer's
top-N hijacked by RF17/18 PHANTOMS (fix pending) so VHF never got
tested. Harness laws: quiet-gates FAIL-OPEN when panel dead (1 hr lost);
panel benched during direct-chain trials; DLL installs verified by
timestamp. Trials/kit pushed 2950858; TV-repo divorce a722806.
07:45 DAYLIGHT SCAN = BEST RABBIT-EARS SCAN EVER: **5 muxes locked**
(RF15 Univision 7p + RF21 Baltimore 5p + RF31 WETA 5p + RF34 NBC 6p +
RF35 WDCA 8p — RF15/RF35 returned in daylight; RF36 Fox still the aim's
sacrifice) ≈ 31 stations. Fireworks-free daylight is the honest scan
window; evening scans understate an antenna.

Related: [[directional-antenna-atsc-2026-06-30]], [[lna-passive-antenna-result]],
[[adaptive-tv-project]], [[sdr-usb-throughput-starvation]],
[[real-quality-metric-ffmpeg-fps]], [[scanner-chain-env-gotcha]].
