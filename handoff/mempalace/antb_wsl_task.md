---
name: antb-wsl-task
description: "Task handed to the WSL Claude: make ANT-B (passive Total Vision EZ-HD indoor antenna) decode ATSC. Everything the Windows session learned about ANT-B's limits, the test apparatus, and the most promising path (LNA). Read this first if you're the WSL session picking up the ANT-B job."
metadata:
  node_type: memory
  type: project
  originSessionId: adaptive-tv-2026-06-26
---

**UPDATE 2026-06-29 — LNA THEORY TESTED & RESOLVED, see [[lna-passive-antenna-result]]:**
the LNA arrived and was tested on the passive Total Vision (+ aiming by ear, shelf pushed
to +13 dB). Result: it bought a genuinely CLEAN carrier lock (mean|x|=0.025) but STILL
ZERO video — the wideband 8-VSB data is under the FEC cliff and an LNA can't add wideband
SNR. So the "LNA is the likely bridge" hope below is **disproven for the passive antenna**;
the wall is antenna APERTURE/CAPTURE, not noise figure. Don't re-chase the LNA on the
passive antenna. The live path is a real DIRECTIONAL/high-aperture antenna (the user's shed
directional, or the Comet DS-150S discone on port A/B). Read [[lna-passive-antenna-result]]
first.

**GOAL (handed from the Windows session 2026-06-26):** make ANT-B — the passive **Total Vision EZ-HD** indoor antenna (VHF 87.5-230 / UHF 470-862 MHz, 5-7 dB, 75Ω, PASSIVE) on the RSPdx's Antenna B port — actually DECODE live ATSC, the way the amplified attic antenna on ANT-A does. The Windows session got ANT-A to watchable but could NOT get ANT-B to decode. Your job: try to bridge the gap.

**WHAT'S ALREADY PROVEN ABOUT ANT-B (don't re-derive — verify if you doubt it):**
- ANT-B LOCKS A PILOT cleanly (mean|x| ~0.041 at moderate gain) but produces ZERO MPEG-2 video (0 seq-headers) at EVERY position tried (room, window, attic), EVERY gain on the ladder, EVERY equalizer (long/pilot_dd/pilot_dd_soft/multifs/multifs_dd/cma), with erasure RS at max budget. It's DATA-SNR-limited, not config-limited: the narrowband pilot survives but the wideband data is below the ATSC cliff.
- GAIN CANNOT FIX IT — proven directly: cranking ANT-B to max gain (IFGR=28) made the lock WORSE (mean|x| 0.041→0.125), still 0 decode. Gain amplifies noise with signal; it adds no SNR.
- ANT-B is NOT FM-overloaded (only ~6.7 dB FM pickup vs ANT-A's ~25 dB), so the FM/DAB notch trick that saved ANT-A does little for B. B can run higher gain than A without overloading — but it doesn't help (see above).
- MEASURED GAP (compare_antennas.py): ANT-A captures ~12 dB more TV signal than ANT-B on average. ANT-B's best channel is **RF31 (+10.5 dB in-band shelf)**, then RF36 (+8). ANT-B raw IQ RMS ~0.05 vs ANT-A ~0.15-0.47.
- KEY INSIGHT: **ANT-A is an ACTIVE (amplified) antenna** — it has an LNA powered by an external injector (proven when the injector's power cable fell out and ANT-A's signal collapsed to the noise floor, then recovered when re-powered). So a big part of ANT-A's advantage is AMPLIFICATION. **The single most promising way to make ANT-B work is an inline/masthead LNA preamp at the antenna** (~$20-30) — it targets exactly the SNR/system-noise-figure gap. After that: height/window placement to reduce multipath and increase capture.

**THE TEST APPARATUS (Windows paths — you'll need to PORT these to Linux; they're plain Python + ffmpeg/ffprobe/TSDuck + SoapySDR):** in `Z:\src\adaptive-tv\` — `quality_judge.py` (0-100 decode score via ffmpeg null-decode error counting — THE objective oracle, ported from the Linux `quality_judge.sh`), `stress_test.py` (time-series quality + telemetry correlation), `quality_sweep.py` (auto-A/B configs), `compare_antennas.py` (head-to-head A vs B levels/shelf/SNR), `player_probe.py` (chain-vs-player isolation), `play_live.py` (buffered VLC player), `auto_tv.py` (orchestrator: detect→pick antenna→calibrate gain→validate REAL decode via seq-headers→play), `find_signal.py` (visual+audio antenna finder), `signal_sonar.py`/`aim_meter.py`/`decode_meter.py`. The Linux originals of several live in the main repo `tools/` (quality_judge.sh, skip_detector.sh, quality_controller.sh, playback_monitor.sh). See [[adaptive-tv-project]] for the full tool list + the gain/garbage/FM-overload lessons.

**WSL GOTCHAS TO EXPECT:** (1) SDR access from WSL2 needs USB passthrough via `usbipd-win` (bind+attach the RSPdx); the SDRplay API + SoapySDR must be installed Linux-side. (2) The adaptive-tv tools hardcode Windows paths (C:\Program Files\SDRplay\API\x64, %USERPROFILE%\radioconda\python.exe, Z:\src\...\live.ts) — adapt to Linux paths. (3) The chain itself is the gr-atscplus/gr-dtv flowgraph — the Linux rig already runs it (see [[ubuntu-rig-workflow]], [[handoff-doc-convention]]). (4) Don't re-fight battles already logged: [[sdr-firmware-replug]] (thermal/wedge), [[scanner-chain-env-gotcha]] (CHAIN_DEFAULTS), the FM-overload + max|x|=π/2 lesson in [[adaptive-tv-project]].

**CURRENT WORKING ANT-A CONFIG (reference baseline that DOES decode):** RF34, STVT_ANTENNA="Antenna A", both notches (STVT_RFNOTCH=1 STVT_DABNOTCH=1), STVT_IFGR=42 STVT_RFGAIN_SEL=2, STVT_RS=erasure STVT_RS_ERASURES=20, STVT_EQ=long STVT_VITERBI=soft + the rest of CHAIN_DEFAULTS (SPS=1.1, RRC_SYMS=8, TEISCRUB=1, EQ_LKG=1).

Related: [[adaptive-tv-project]], [[wsl-handoff-briefing]], [[ubuntu-rig-workflow]], [[discone-vhf-hi-atsc-works]].
