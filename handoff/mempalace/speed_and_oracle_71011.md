---
name: speed-and-oracle-71011
description: "7/10 night → 7/11: the speed stack (memory-tune, persistent retune 10s MET, deep tune doctor, scan planner wire-in), Knob-of-Time v3 UI + Market Brain, antenna fingerprint + tower axis findings, discone conquest, RF9 investigation (panel pollers + cliff lottery fixed)"
metadata: 
  node_type: memory
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

**MEMORY-TUNE (e8371c8, 7/10 late):** tune-from-memory — pid_cache.json
(self-healing 1 s PSI probe + 8 s no-output watchdog; ladder
cached→full→video→mux) + Knob-of-Time-gated 3 s margin stage (skips the
12 s "measuring decode margin" when the current-hour bin is
solid/thin ≥16.8). Warm hop 13.4→7.5 s, cross-tower 34.3→15 s.

**PERSISTENT-RADIO RETUNE (00a9a90, flag ON): 10 s TARGET MET** —
cross-tower 9.4-10.0 s to video (n=6), hop 3.1 s; retune applies in
0.08-0.14 s via retune.cmd/ack watcher + runtime soapy setters
(set_antenna WORKS on RSPdx at runtime); live.ts offset-0 rotation;
3 fallback gates (ack 3 s / field-sync 8 s / cliff quick-read) →
classic respawn; tap cache DISABLED under flag (would pollute the old
channel's file; runtime rebind = future); 5-min endurance 0 OsO.
STVT_PERSIST_RETUNE=1 (unset = byte-identical stock).
GOTCHA: polling ts_metrics at 2 Hz reads live.ts at ~97 MB/s and
CAUSES OsO — sparse polling only.

**DEEP TUNE (2b4a6eb): 🔬 channel doctor** — baseline → antenna race →
bounded gain grid (AGC servo respected) → disease verdict →
lab/channel_recipes.json consulted by every future tune (manual pick
wins; stale >14 d flagged); measurements feed history
(source=deeptune); cancel-on-user-tune. RF15 "mystery" SOLVED:
auto-picker/belief-map chose rabbit (93.8% loss) vs Philips 0.051% —
antenna-steering fossils are the top glitch source after decoder
health. /api/tune without antenna field resets picker to auto (web UI
always sends it). Button also on failed tunes ("DEEP TUNE the
unwatchable", 7/11).

**ANTENNA FINGERPRINT (30c0dbd, validated):** VHF/UHF regime
fingerprint per antenna is REAL and transfers (LOO 0.74 dB on VHF);
hour-curves do NOT transfer (r≈0.08); UHF ruled by ±3.5 dB per-channel
territories → best-antenna guess for new UHF = "your global best"
(validated policy); tower transfer untestable in this near-one-cluster
market. time_knob_prior.py prototype UNWIRED — plug-ins: new-channel
badge, deep-tune ordering, fast-tune qualification.

**CROSS-CHANNEL SPEED (9c597c7):** tower/direction fingerprint =
consistent but mathematically inseparable from frequency on 5 channels
(rabbit's 2 UHF wins are exactly the 2 off-cluster sites, sign-stable;
decisive test = PHYSICALLY ROTATE Antenna B and re-sweep — a boresight
moves, a frequency notch doesn't). scan_planner.py: discone sweeps
−45% via dead-VERDICT memory (carrier-without-data leaves NO MER — the
verdict log is the evidence, not the MER prior); Philips ordering gets
80%-of-locks 38% sooner. WIRED IN 7/11 (a05c8ee): scan.json stamps
antenna; scans seed pid_cache market-wide (first visits become warm);
STVT_SCAN_ORDER planner ordering (fail-open). Probe budgeting =
step 4 after an A/B night. Surf pre-qual fires on 0 pairs today
(honest; self-activates on better antennas).

**FRONTIER PATROL (a05c8ee):** generalized night hunter — picks its own
targets: any (rf, ant) pair with learned/prior MER within ~2 dB of the
cliff; probes while idle, dawn boost, CRACKED logging. First target
list: RF7 on rabbit ears (est 15.0).

**DISCONE CONQUEST (8626c25):** bedtime_discone.py overnight campaign
7/10→11: NINE probes, NINE decodes of RF7 (16-18 dB, 5-6 hdrs/s, best
4:15-4:45 <6% loss); morning-after live viewing 17.6 dB / 6.5%.
Months of sub-cliff history refuted — the decoder improvements moved a
"can't do TV" antenna across the cliff; midnight "failure" was
ghost-session chain contention. Case study in docs/knob_of_time.md.

**GHOST-SESSION TRAP:** user's accidental restart left a 2nd Claude
session alive spawning rival panels/chains (4 panels, 6 chains, 11%
loss). Diagnose stacked panels via parent-PID cmdlines; user must
close the other window. (Also: psutil/grep searches match their own
heredoc argv — the pgrep self-match trap, again.)

**KNOB-OF-TIME UI v3 + MARKET BRAIN (6f53a3a, 8695810, 32b62cd):**
growing bars (height = samples log-scale; green ONLY on reliable
decode: MER≥15.7 AND loss<1.5; red below cliff or loss≥5) +
antenna-scoped card (≥30 rows) + explicit verdict line ("learned — and
honestly unwatchable…"); knowledge vs verdict are SEPARATE metrics.
E7 renamed 🩺 REPLAY-HEAL (last 30 s) — kept: offline no-deadline
rescue path. 🧠 MARKET BRAIN card: market knowledge % + programs
memorized + recipes + AUDITED oracle accuracy (oracle_score.csv:
per-minute predicted-vs-actual pairs; card = rolling MAE; first check
0.16 dB). CH+/CH− surf buttons + ArrowUp/Down. mpv
--auto-window-resize=no (aspect-dance fix, 3ab9996).

**RF9 INVESTIGATION (3c18a0e, 7/11 — user right 5th time):** two
self-inflicted glitch sources found by 6-arm ABBA direct-chain ladder:
(1) panel pollers read live.ts in 48 MB bursts from THREE loops →
metronomic ~9.7 s OsO ("ambient trickle" SOLVED — ts_metrics_cached
30 s single-flight + chunked tail reads; full stack now 0.00 OsO/min);
(2) cliff_mode lottery: arsenal at tune-read <16.5 overruns realtime
in the 16.x band (16-22 OsO/150 s, MOD12 slips, 9-15% fewer good
packets — same disease class as DD) → threshold 16.5→16.0; bisect
which arsenal piece (prime suspect DFE+anchor) pending; cliff MER is
flattered by the DFE (not comparable to base MER). TURBO EXONERATED
on Threadripper: 238-265 att/s, ZERO OsO, −1.0 pp loss (27% of
failures rescued), gate modulates in storms — sub-gate CPU bleed is a
non-issue on this hardware but matters for the future feature-governor
on small CPUs. Antenna re-race in morning air: B wins mornings too
(A = no decode); recipe → 5/28. Residual RF9 morning loss 2.8-8%
breathing at MER ~16.6 = the sky; Knob's 17-21h advice stands.
9.1/68.1 SAME MUX (PSIP: RF9 progs 1-7 = WUSA 9.x + AltaVsn 68.x —
channel-share, not separate towers).

**ANTENNA AUDITIONS 7/11 midday:** OLD FAITHFUL (attic classic, port A
briefly) = UHF workhorse, house-best RF15 19.2, VHF-deaf (RF9 race:
Philips +10.2 dB — user's May true-crime memory was 4.3 CRIMES on
RF34-UHF, not 9.2-VHF; PSIP proves 9.x+68.x share RF9).
**TOTAL VISION RESURRECTED:** the never-decoded passive antenna, on
the attic coax + modern decoder, locked RF15 18.4 / RF34 16.3 /
RF31 16.2 on its maiden scan and PLAYED 14.1 AT 0.02% LOSS GLASS.
Old "SNR-limited" verdict dead — second resurrection after the
discone; decoder-era re-trials are mandatory for all written-off
hardware. Epochs (🔌) used in production for both swaps. Missing on
TV: RF36/35/21 + VHF → frontier-patrol targets. UI-stuck bug: literal
\n inside JS strings in the non-raw PAGE template = script-killing
SyntaxError (22b53ba) — PAGE needs escaped backslashes.
USER ROADMAP: shed directional next (PC moves downstairs, reboot —
relaunch stack after), then Linux port (WSL = build+replay validation
ONLY, live needs the Ubuntu box), then Pi + feature governor.

**SHUTDOWN CHECKPOINT 7/11 ~12:30 (PC moving downstairs for shed
directional):** everything pushed through 6ef8f28 (README + knob doc
current). RESTART CHECKLIST after reboot: (1) start panel via
PowerShell Start-Process from Z:\src\adaptive-tv with
STVT_PERSIST_RETUNE=1, stdout/err → lab/panel_*.log, verify ONE
process + http 200; (2) SDR first session after power cycle may be a
dud (restart SDRplayAPIService if init fails); (3) directional-shed
welcome: plug into PORT A → POST /api/new_antenna {"antenna":
"Antenna A"} (epoch!) → AMP-ONLY law (never LNA; old recipe rfgain 2 /
IFGR 36, doctor re-derives) → scan → deep tunes → maybe frontier
patrol at night; (4) drizzle_watch/time_trainer optional relaunch.
Total Vision verdict stands: RF15/34/31 decodable from attic coax,
14.1 played 0.02% glass. Then USER ROADMAP: Linux port (WSL =
build+replay validation only; live on Ubuntu box via main-linux +
relay + live-test law) → Pi + feature governor (Fable until 7/12).

**H(f) PRINT — LIVE-PROVEN 14:03 (abdb8fc):** the magic moment
happened: after the user's physical unplug/replug, 🪪 returned
"recognized shed directional on Antenna A (90% match)" —
cross-instrument (enrolled from scanner phase-1 sniffs, recognized by
standalone Welch sweep). UI name = H(f) PRINT (antenna+cable = filter;
sweep estimates |H(f)| shape). Env laws found: sdr_sweep children need
env_with_sdrplay(); panel relaunch needs radioconda\Library\bin +
SDRplay x64 on PATH or waterfall looks radio-dead.

**ANTENNA AUTO-ID shipped 7/11 ~13:40 (9e3f546, user's idea):**
antenna_id.py — spectral-SHAPE fingerprints (floor-referenced ratios;
shape=antenna, absolute=hour), free as scan byproduct + 🪪 IDENTIFY
button (~60 s idle sweep). Verdicts RECOGNIZED/MOVED (history follows
the metal across ports!)/CHANGED (asks, never silently forks)/NEW
(auto-profile+auto-epoch). VALIDATED: same antenna 31 min apart 0.91,
cross-antenna ≤0.46, thresholds 0.75/0.55, dud sweeps refused; ledger
lab/antenna_profiles.json seeded ant-0001 "shed directional"
re-recognized 90%. CAVEAT: Old-Faithful×Total-Vision pair unverified
on full signatures (scans overwritten; ledger now archives every scan
signature). Scan strict-carrier records lack freq_mhz (rf_to_mhz
recovers). SHED DIRECTIONAL day: amp working, VHF star (RF7 19.1 best
ever), UHF absent pending AIM (🎯 WETA rotation experiment pending);
RF9 early-afternoon storms 26-42% on everything. **USB LAW UPDATE:
the RSPdx dropped off the bus TWICE after the PC moved downstairs —
flaky physical connection; each drop needs replug + service restart +
dud-eat. Only ONE antenna connected now (shed on A) — Philips/others
unplugged: recipes pointing at B are for a dead port until user
reconnects; deep-tune races vs empty ports correctly pick A.**
Deep-tune design gap logged: single-shot antenna race on fader-class
channels is a lottery (RF9 13h race) — needs ABBA/interleaved races.

**"I THINK IT IS DONE" — user declared the universal tuner complete
7/11 ~14:15, right after H(f) PRINT's live recognition.** The vision
shipped end-to-end: any antenna, plug in and go — measured, named,
remembered, scheduled, healed, honest. Remaining adventures are
expansions, not gaps (aim experiment, other-antenna enrollment, Linux
port via WSL build-lab → Ubuntu live, Pi + feature governor).

**H(f) PRINT FULL LIFECYCLE PROVEN (round trip A→B→A, 9feedea):**
MOVED @92% both directions, biography follows the metal; VACANT prints
(user insight: empty socket has a radio face — enrolled from the
ant-0002 ghost; MUST COMPETE never preempt: disconnected ports hear
real leakage ≈ weak antenna); math-derived designations
(print_id → HF-XXXXX at enrollment, stable under EMA drift;
auto_descriptor = band tilt + peak; user names optional) — shed
directional = HF-YSHDI "VHF-tilted 4 dB · peak 177 MHz" (descriptor
correctly reflects its mis-aimed-UHF state); observe() self-heals
dangling port_current refs. Round-trip test caught BOTH fresh bugs
(vacancy veto, dangling pointer) — user's breaking-tests remain the
best instrument (6 catches now).

**SHUTDOWN CHECKPOINT #2, 7/11 ~15:00 (PC returning upstairs):** all
pushed through 9feedea, working tree clean, repo copies verified
byte-identical. MISSION AFTER RESTART: **enroll ALL antennas with
H(f) prints** (Philips, rabbit ANT111E, discone, Old Faithful, Total
Vision). Per antenna: plug → select port in dropdown → 🪪 → NEW
auto-enrolls (HF-XXXXX + auto-descriptor + auto-epoch) → user may
name via the toast/name API. **TRAP TO PREVENT: only port A has a
VACANT print — an 🪪 on empty B or C will enroll a leakage ghost
(ant-0002 pattern). Bootstrap: sweep each EMPTY port first, let it
enroll a ghost, then convert ghost→vacant print via
aid.mark_vacant(port, sig) + delete ghost + purge port_current ref
(observe() now self-heals dangling refs).** Restart checklist:
PowerShell Start-Process panel from Z:\src\adaptive-tv with PATH =
radioconda\Library\bin + SDRplay x64 prepended, STVT_PERSIST_RETUNE=1,
verify single process + /api/ports; SDR post-boot dud law applies.
Ledger state at shutdown: HF-YSHDI (shed directional) resident on
Antenna A confirmed, 7 sightings, vacant print exists for A only.

**ENROLLMENT COMPLETE + identity hardening (693f7ca):** final port
map: A=HF-HQFFH "Total vision" (broadband·peak 85), B=HF-E622J "Old
faithful" (descriptor suspect — enrollment sweep may be off), C=
HF-RYUH5 "discone"; HF-YSHDI shed directional benched. OF×TV = REAL
near-twins in the wild (92% cross-match after a scan, exactly as the
LOO research predicted) → 3 fixes: one-antenna-one-place invariant,
near-twin ASK guard (runner-up within 0.07 → never auto-follow),
CONSERVATIVE scan byproducts (routine scans never enroll/evict/prompt
— RECOGNIZED silent, everything else = quiet pending). ✏️ NICKNAME
button shipped (callsign immutable, name user's). UI renamed
"ANTENNA FINGERPRINT SCAN" (math in tooltip). The \n-in-PAGE trap bit
a SECOND time (nickname prompt) — NEVER put \n in PAGE JS strings.

**FINAL CHECKPOINT 7/11 evening — WSL ERA BEGINS:** user declared
happy with the code; WSL Claude Code session opened, given the paste
prompt (read handoff/WSL_HANDOFF_20260711.md → bootstrap build →
compile sweep → selftests → replay validation vs reference numbers →
commit to wsl-port branch ONLY, never main-linux without live test).
Late-session ships: GHOST-SAP fix (d41ca59 — PMT-declared zero-packet
audio streams killed whole extraction; extractor now counts real
packets per PID before mapping — WDVM 25.1 sound restored); 4-TIER
knob bars (green well/yellow struggles/red bad/BROWN no-decode, dark=
untrained); knob card IDENTITY-SCOPED (bd58a31 — rows_for_profile:
history follows the recognized antenna, fixed "watching TV under a
brown bar"); PRIVACY SCRUB (6c34f42): repo tree = zero email, zero
username (27 files → sys.executable/portable paths); PRE-push
identifier sweep now law. Shutdown state: 25.1 playing WITH sound,
ports = Total vision (A) / Phillips (B) / discone (C), HF-YSHDI shed
directional benched; swap-test + nickname-persistence experiment
still pending user's cable hands; repo through 6c34f42.

NEXT QUEUE: feature governor (computer-agnostic dial-in via OsO
headroom), arsenal-piece bisect, README 10s-tuning section,
knob-audit fix pass (DVR path = different decoder), label unification
(rabbit↔Antenna A), probe budgeting A/B night, rotate-antenna tower
experiment.
