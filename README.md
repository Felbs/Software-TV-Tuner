# Magic TV Decoder

**Watch free over-the-air HD television, decoded from raw radio waves
in software, on a regular laptop.**

A custom GNU Radio fork (`gr-atscplus`) that decodes ATSC 1.0 broadcast
TV from a $100 SDR + a TV antenna. It produces a live MPEG-TS stream
your favorite player can watch — VLC, ffplay, or our included
`magic_tv.py` CLI launcher.

The repo contains:
- A modified gr-dtv decoder with **6 experimental C++ blocks** that
  fix bugs and squeeze more locks out of marginal signals.
- A **command-line TV launcher** (`tools/magic_tv.py`) that scans your
  channels, picks one, and plays it — also records to MP4 and
  re-streams to RTMP / Twitch / YouTube live.
- Watchdogs for both the SDR decoder lock and the downstream player
  pipeline so playback can self-heal.

## What it took

This is the result of a multi-day iteration session, documented in
[`docs/2026-05-02-session.md`](docs/2026-05-02-session.md). Short
version: the stock equalizer was slightly broken (under-tuned LMS,
fixed in Tier 3), the FPLL alpha/tau had too much margin loss past
60 seconds (fixed in Tier 7), and the *actual* recurring 30 s drift
bug turned out to be upstream of all of that — `atsc_fs_checker_inst`
was accepting spurious early field-sync detections, frame-slipping
the entire downstream chain. Tier 21 added a 313-segment spacing
validator and turned "30 seconds and freeze" into continuous HD
playback.

| Tier | What we tried | Outcome |
|---|---|---|
| 1 | Long equalizer + slow AGC | massive PAT improvement, baseline |
| 2 | DD/DFE adaptation | falsified, removed |
| 3 | **Anti-windup + leakage on equalizer** | **shipped — current code** |
| 4 | Tap snapshot/revert | no win, reverted |
| 5 | GPU 1024-tap LMS | no win |
| 6 | CMA + DFE blind equalizer | no win — bottleneck not in EQ |
| 7 | **FPLL tightening (`alpha=0.001, tau=50us`)** | **shipped — fixed t=60s drift** |
| 8 | Neural-net Viterbi replacement | only beats Viterbi above 17 dB SNR |
| 9 | **Soft-Viterbi memset of uninit stack buffer** | **shipped — fixed 100% TEI on real RF** |
| 10 | Tighter `DIVERGENCE_BAIL` (50→10) | falsified, reverted (bit-identical TS) |
| 11–19 | Equalizer/Viterbi state speculation | all falsified — wrong layer |
| 20 | Per-FS `n_data_segs` instrumentation | smoking gun: FS slips at fs_pass 843/846 |
| 21 | **FS-checker 313-segment spacing validator** | **shipped — the actual fix** |

## Download & install (Windows, 10 minutes)

You need:
- A computer with **GNU Radio 3.10+**, easiest via
  [`radioconda`](https://github.com/ryanvolz/radioconda) (free).
- A supported SDR. We've tested with the **SDRplay RSPdx** + the
  SDRplay API v3 driver. Other SoapySDR-supported devices may work
  with parameter tweaks.
- A **horizontally-polarized TV antenna** — see
  ["Why polarization matters"](#why-polarization-matters) below.
- (Windows only) [`ffmpeg`](https://www.gyan.dev/ffmpeg/builds/)
  (full build) extracted to `C:\ffmpeg\`.

```powershell
# 1. Clone the repo
git clone https://github.com/Felbs/magic-tv-decoder.git
cd magic-tv-decoder

# 2. Build the C++ decoder OOT module (Linux: bootstrap.sh; Windows: gr-atscplus/_build.bat)
#    On Windows you need VS 2022 BuildTools + NMake; the build script handles the rest.
gr-atscplus\_build.bat

# 3. Verify the new blocks are loadable from Python
python -c "from gnuradio import atscplus; print(dir(atscplus))"

# 4. Install the resilient player's runtime deps
& "C:\Users\<you>\radioconda\python.exe" -m pip install opencv-python sounddevice

# 5. Pick + run a channel
python tools\magic_tv.py
```

The interactive picker shows every channel in your DMA grouped by RF
frequency. Pick one and start watching. (The default channel table
covers DC/Baltimore — edit `tools/fcc_dc_stations.py` for your region.)

## Run

```powershell
# Interactive mode (recommended): banner + channel picker
python tools\magic_tv.py

# Direct mode: tune RF36 (Fox 5 DC) and play locally
python tools\magic_tv.py --rf 36

# Record a show to MP4 (no playback window, ideal for unattended capture)
python tools\magic_tv.py --rf 36 --no-play --record fox5_news.mp4

# Stream live to Twitch / YouTube / any RTMP destination
python tools\magic_tv.py --config-set twitch rtmp://live.twitch.tv/app/YOUR_KEY
python tools\magic_tv.py --rf 36 --stream twitch

# Dry-run: print the planned subprocess commands without spawning
python tools\magic_tv.py --rf 36 --dry-run
```

## Live-streaming and recording

`magic_tv.py` uses ffmpeg's `tee` muxer so you can multiplex outputs
without re-encoding twice. One command can simultaneously play
locally, record to MP4, and push to RTMP. Re-encode is libx264
ultrafast / zerolatency / crf 28; audio passes through.

## Watchdogs and the resilient player

Three layers keep playback alive on marginal signals:

- **Decoder watchdog** — periodically samples PAT count from the live
  TS. When the equalizer drifts (PAT drops below threshold), kills
  and respawns `tv_live` for a fresh equalizer convergence.
- **Pipeline watchdog** — when ffmpeg blocks on bad input, the watchdog
  detects no-bytes-forwarded-while-data-flowing and respawns ffmpeg
  while keeping `tv_live` alive.
- **`magic_player.py`** — the default playback engine, a Python video
  player with **decoupled audio/video clocks** that *never* freezes.
  When SDR drift produces corrupt video PES, video holds the last good
  frame while audio keeps decoding from its own PID. ffplay would
  freeze both, hiding what's actually happening; magic_player shows
  the SDR's true state with a status overlay (frame age, byte rate,
  decoder health). Toggle off with `--player ffplay` if you prefer.

Together they replace "30 seconds and freeze forever" with continuous
viewing where SDR drift events appear as held frames + audio (visible
diagnostic) instead of the whole player locking up.

By default `python tools\magic_tv.py` prints instructions to launch
`magic_player.py` in a second PowerShell window — cv2's GUI window
attaches reliably when run interactively, not when spawned as a
subprocess. Recording (`--record`) and RTMP streaming (`--stream`)
automatically use the legacy ffplay path since they need ffmpeg's
tee muxer.

## Why this exists

ATSC has been around for 30 years; you can buy a $20 USB ATSC stick
that decodes it perfectly. So why software-decode it from raw RF?

- **Education.** Software decode lets you instrument every step of
  the chain — see what the equalizer is doing, what the FPLL is
  doing, watch field-sync detection happen. You can't open up the
  ASIC in your USB stick.
- **Research.** You own every byte from raw IQ to decoded TS, and
  every block in between is hackable.
- **Hackability.** Want to add a custom error-concealment frame
  interpolator? Write a new GR block. Try it. Compare to baseline.

## Forked C++ blocks

| Block | Status | What it does |
|---|---|---|
| `atsc_fpll_tight` | ✓ shipped | Carrier PLL with parameterized loop bandwidth + AFC time constant. Best params: **alpha=0.001, tau=50us** (Tier 7 finding) |
| `atsc_equalizer_long` | ✓ shipped (Tier 3) | 256-tap LMS equalizer with anti-windup + leakage. Replaces the stock 64-tap. Tier 3 fix made this the workhorse decoder |
| `atsc_viterbi_soft` | ✓ shipped (Tier 9) | Soft-decision Viterbi. The "broken on real RF" phase was an uninitialized stack buffer (`out_copy`); a `memset` to zero on entry fixed it |
| `atsc_fs_checker_inst` | ✓ shipped (Tier 21) | Field-sync checker with 313-segment spacing validation. Rejects spurious early FS detections that misalign the equalizer downstream — the *actual* root cause of the long-running 30 s drift bug |
| `atsc_sync_tunable` | ✓ working, neutral | Lock thresholds + hysteresis |
| `atsc_fs_checker_inst` | ✓ working | Instrumented field-sync checker (PN511/PN63 histograms to stderr) |
| `atsc_equalizer_cma` | experimental | Continuous-Modulus + DFE. Falsified in Tier 6 testing — included for reference, not used by default |

## How does this actually work?

[`docs/science.md`](docs/science.md) is a long-form explainer of every
signal-processing step, written for curious readers without an RF
engineering background: 8-VSB modulation, why VSB needs the Hilbert
transform, the FPLL carrier-lock loop, equalizer training vs
decision-directed adaptation, soft- vs hard-decision Viterbi,
Reed-Solomon, why gain settings matter so much, and how to tell from
`atsc_fs_checker_inst` output which step in the pipeline is broken
when something goes wrong.

[`docs/proven_capture_recipe.md`](docs/proven_capture_recipe.md)
documents the SDRplay gain settings, antenna polarization, and
capture parameters that produce the best lock.

[`docs/2026-05-02-session.md`](docs/2026-05-02-session.md) is the
narrative of the day-long iteration that produced the current
working state — every tier of fix attempted, what worked, what
didn't, and why.

## Why polarization matters

ATSC broadcast TV in North America is **horizontally polarized**. A
vertically-polarized antenna (e.g. an SDR-hobby discone or a vertical
whip) loses 10-15 dB of signal versus a horizontally-polarized one.
That loss is below the threshold the FPLL needs to lock the carrier.
A "perfectly fine" SDR setup that decodes ham radio and aircraft
signals beautifully will produce 100% TEI=1 garbage on TV unless the
antenna is correctly oriented.

Indoor rabbit-ears bent into a horizontal "V" work surprisingly well.
A purpose-built UHF Yagi gives the best SNR margin.

## Repo layout

```
gr-atscplus/                  Forked GNU Radio OOT module (6 C++ blocks)
  _build.bat                  Windows VS 2022 + NMake build
  _rebuild.bat                Windows incremental rebuild
tools/
  magic_tv.py                 Channel picker / player / recorder / streamer
  tv_live_rf34.py             Continuous SDR → MPEG-TS pipeline
  tv_live_rf34_softvit.py     Same pipeline, soft-Viterbi variant (--viterbi soft)
  fcc_dc_stations.py          Sample channel table (edit for your DMA)
  config.py                   Default tuner/antenna/gain config
  magic_player.py             Resilient video player (decoupled A/V clocks)
docs/                         Science explainer, capture recipe, session log
bootstrap.sh                  Linux setup + build + install
```

## License

GPL-3.0-or-later (inherited from gr-dtv).
