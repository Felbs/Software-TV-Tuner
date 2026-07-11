---
name: radiotuna-project
description: "gr-radiotuna: SEPARATE project (Z:\\src\\gr-radiotuna, split from Software-TV-Tuner 7/05) applying the adaptive method to other bands. Campaign 1 = HD Radio via nrsc5 (already installed C:\\Tools\\nrsc5\\nrsc5.exe, native cs16 -r input); tools/hd_radio.py capture/decode/live. Public GitHub repo creation = USER's step (agent blocked from choosing visibility)."
metadata:
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

**Born 2026-07-05 evening.** User wants Radio Tuna fully separate from the
TV project, GNU Radio lineage ("for our decoders we should fork GNU Radio
projects"). Local repo committed at Z:\src\gr-radiotuna (README manifesto,
docs/HD_CAMPAIGN.md, docs/ROADMAP.md, tools/hd_radio.py, lab/ gitignored).
TV-repo divorce commit a722806 (files removed from Software-TV-Tuner).

**PUBLISHED by user 2026-07-05 ~9:15 PM: https://github.com/Felbs/gr-radiotuna
(branch master).** Agent remains blocked from creating public repos /
choosing visibility — user runs `gh repo create` themselves in plain
PowerShell (NOT the Claude `!` prefix, and no `&&` in PS 5.1 — two
separate lines). gh CLI at C:\Program Files\GitHub CLI\gh.

**Campaign 1 — HD Radio (NRSC-5):**
- nrsc5.exe ALREADY on box at C:\Tools\nrsc5\nrsc5.exe; supports
  `-r <iq>` with `--iq-input-format cs16` (our capture format!) at its
  native 1,488,375 Hz. Plan: capture 2.97675 MS/s → ÷2 decimate.
- tools/hd_radio.py: `capture --mhz 103.5 --secs 15`, `decode --iq ...
  --mhz --prog [--play]`, `live --mhz --prog` (growing-file trick:
  IQ file grows under nrsc5 while its WAV output grows under mpv).
- E-ladder in docs/HD_CAMPAIGN.md: E1 first light (WTOP 103.5 capture,
  discone = ideal FM antenna, vertical pol) → E2 dial into SIGNAL
  FINDER → E3 gain cal fork on BER → E4 band survey → E5 marginal hunt.
- nrsc5 stderr = the dial: sync / MER: / BER: / Title: / Station name:.

**Roadmap tiers (docs/ROADMAP.md):** 1) HD Radio; Meteor-M LRPT 137 MHz
(rabbit ears BEND INTO the 120° V-dipole — free sat antenna; Doppler +
pass-scheduler = sentry pointed at the sky); GOES HRIT 1.69 GHz (dish
aiming = aiming-tone destiny, ~$130 hw). 2) AIS 162 (discone home turf),
UAT 978, ADS-B (toy). 3) FT8/WSPR HF (ionospheric adaptivity, RSPdx HF
port's real job), P25 merge with [[spectrum-scout]], DRM, Inmarsat.

**Naming law:** tuna at brand level, discipline in API —
radiotuna.hd_* / sat_* / ais_* blocks in ONE namespace; no per-decoder
fish names.

**NIGHT 1 RESULTS (7/05, pushed 5de2def):** FIRST LIGHT COMPLETE — WTOP
synced, station ID + metadata + 4 programs + tower GPS + 17.8s audio.
LIVE STREAMING WORKS: radio_panel.py = vintage-cabinet UI at :8643
(tuning dial w/ station dots, survey-built guide 53 stations/6 HD,
HD program buttons + analog-FM button per station, band tabs FM·HD/
AM-soon/SW-soon, staged progress bar, MER/BER/LOCK meters). KEY TECH:
(1) this nrsc5 build's cs16 file path SILENT/BROKEN — cu8 works; with
-r, OMIT the frequency positional; stdin-EOF quits it (keyboard thread),
keep a pipe open or stream data on stdin; (2) LIVE = `nrsc5 -r -` stdin
streaming (growing-file EOF STALLS this build — synced then froze at
161KB); (3) FM blowtorches CLIP at TV gains — IFGR 59/rfgain 3 = clean
0.115 rms on WTOP; survey's fixed-gain probes likely missed HD on the
strong 96-108 block (WTOP!) — ADAPTIVE PROBE (retry at stepped gains) =
first open bug; sweep hops [91,97,103] miss 106.5-107.9 (gap #2);
(4) HD→analog auto-fallback after 25s no-sync = the blend philosophy v0;
(5) analog FM v1 = numpy discriminator + 75µs deemph, mono (gr stereo +
pilot-SNR telemetry = next); WETA live: BER 0.012-0.02, 3 programs.
DC HD roster: WAMU(2p), WETA(3p), WGTS, WFLS(2p), WPFW(weak -4.5),
93.9(?). Adaptive-FM vision discussed: pilot-SNR/envelope-ripple/RDS-BER
dials → adaptive IF bandwidth + stereo blend + HD/analog crossfade =
"best available sound at every instant".

**NIGHT 1 LATE (audio truth apparatus + WETA mystery):** User heard
static on "live HD" → built tools/audio_probe.py (spectral flatness/
tilt/crest on WAV tail → MUSIC/SPEECH·STATIC·SILENCE·CLIPPED; noise
flat~0.55/tilt~0, real audio flat<0.1/tilt>18) + audio_watch in panel
(probe every 10s → AUDIO meter ♪/✗; 2×STATIC → auto-fallback analog) +
lossless pump (queue.Queue decouples SDR reads from nrsc5 stdin).
VERDICT: pump was innocent — **WETA 90.9 HD audio = uniform static on
this nrsc5 build ALWAYS** (any gain IFGR 34-59, file or live, MER 12;
metadata/titles decode fine — codec mystery, try prog 1/2 or newer
nrsc5). **WTOP 103.5 live HD = WORKS, probe-verified MUSIC/SPEECH
(flat 0.023) even at MER 6.5.** Debug law: when a station reads STATIC,
cross-check a DIFFERENT station before blaming the plumbing. Analog-FM
fallback volume scale too quiet (rms 0.014) — bump the *12000 constant.

Related: [[mer-dial-universal-algorithm]] (the method, entries 16-20),
[[adaptive-tv-project]], [[lna-passive-antenna-result]] (spectrum-scout).
