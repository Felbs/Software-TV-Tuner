# Software TV Tuner (STVT)

This is a software TV tuner for a software-defined radio. Watch TV on
your SDR.

A custom GNU Radio fork (`gr-atscplus`) decodes ATSC 1.0 broadcast TV
from a $100 SDR + a TV antenna into a live MPEG-TS stream. Bundled with
a CLI launcher (`tv_tuner.py`) that scans your area, picks a channel,
tunes it, and plays it — also records to MP4 and re-streams to RTMP.

## Download & install (Windows, ~10 minutes)

You need:
- A computer with **GNU Radio 3.10+**, easiest via
  [`radioconda`](https://github.com/ryanvolz/radioconda) (free).
- A SoapySDR-supported SDR. Tested with **SDRplay RSPdx** + the
  SDRplay API v3 driver.
- A **horizontally-polarized TV antenna** — see
  [Why polarization matters](#why-polarization-matters) below.
- (Windows only) [`ffmpeg`](https://www.gyan.dev/ffmpeg/builds/) full
  build extracted to `C:\ffmpeg\`.

```powershell
# 1. Clone the repo
git clone https://github.com/Felbs/Software-TV-Tuner.git
cd Software-TV-Tuner

# 2. Build the C++ decoder OOT module
#    Windows: VS 2022 BuildTools + NMake. Linux: bootstrap.sh.
gr-atscplus\_build.bat

# 3. Verify the new blocks load from Python
python -c "from gnuradio import atscplus; print(dir(atscplus))"

# 4. Install the resilient player's runtime deps
& "$env:USERPROFILE\radioconda\python.exe" -m pip install opencv-python sounddevice

# 5. Pick + run a channel
python tools\tv_tuner.py
```

The interactive picker shows every channel in your DMA grouped by RF
frequency. The default channel table covers DC/Baltimore — edit
`tools/default_stations.py` for your region.

## Run

```powershell
# Interactive: banner + channel picker
python tools\tv_tuner.py

# Direct: tune RF36 (Fox 5 DC) and play locally
python tools\tv_tuner.py --rf 36

# Pick a subchannel (4.1 NBC = --program 1, 4.4 Oxygen = --program 4)
python tools\tv_tuner.py --rf 34 --program 1

# Record to MP4 (no playback window)
python tools\tv_tuner.py --rf 36 --no-play --record fox5_news.mp4

# Stream live to Twitch / YouTube / any RTMP destination
python tools\tv_tuner.py --config-set twitch rtmp://live.twitch.tv/app/YOUR_KEY
python tools\tv_tuner.py --rf 36 --stream twitch

# Dry-run: print the planned subprocess commands without spawning
python tools\tv_tuner.py --rf 36 --dry-run
```

`tv_tuner.py` uses ffmpeg's `tee` muxer so one command can play
locally, record, and push to RTMP simultaneously without re-encoding
twice.

## Watchdogs

Three layers keep playback alive on marginal signals:

- **Decoder watchdog** — periodically samples PAT count from the live
  TS. When the equalizer drifts (PAT drops below threshold), kills and
  respawns `tv_live` for a fresh equalizer convergence.
- **Pipeline watchdog** — when ffmpeg blocks on bad input, the watchdog
  detects no-bytes-forwarded-while-data-flowing and respawns ffmpeg
  while keeping `tv_live` alive.
- **`tv_player.py`** — optional Python video player with decoupled
  audio/video clocks. When the SDR briefly produces corrupt video PES,
  video holds the last good frame while audio keeps decoding from its
  own PID — a more honest diagnostic than ffplay's all-or-nothing
  freeze. Toggle with `--player magic`.

## How does this actually work?

[`docs/science.md`](docs/science.md) is a long-form explainer of every
signal-processing step, written for readers without an RF engineering
background: 8-VSB modulation, the Hilbert transform, the FPLL carrier-
lock loop, the LMS equalizer, soft-decision Viterbi, Reed-Solomon, the
field-sync spacing-validation fix that finally made it watch a baseball
game, and how to read `atsc_fs_checker_inst`'s output to find which
step is broken when something goes wrong.

[`docs/proven_capture_recipe.md`](docs/proven_capture_recipe.md)
documents the SDRplay gain settings, antenna polarization, and capture
parameters that produce the best lock.

## Why polarization matters

ATSC broadcast TV in North America is **horizontally polarized**. A
vertically-polarized antenna (e.g. a discone or a vertical whip) loses
10–15 dB of signal versus a horizontally-polarized one. That loss is
below the threshold the FPLL needs to lock the carrier. A "perfectly
fine" SDR setup that decodes ham radio and aircraft signals beautifully
will produce 100% TEI=1 garbage on TV unless the antenna is correctly
oriented.

Indoor rabbit-ears bent into a horizontal "V" work surprisingly well.
A purpose-built UHF Yagi gives the best SNR margin.

## Repo layout

```
gr-atscplus/                  GNU Radio OOT module (custom C++ blocks)
  _build.bat                  Windows VS 2022 + NMake build
  _rebuild.bat                Windows incremental rebuild
tools/
  tv_tuner.py                 Channel picker, player, recorder, streamer,
                              and live channel changer all in one CLI
  tv_live.py                  Continuous SDR → MPEG-TS pipeline
  tv_live_softvit.py          Same pipeline, soft-Viterbi variant
  sdr_sweep.py                Fast carrier-presence pre-scanner
  atsc_psip.py                PSIP parser (virtual channels + EIT)
  default_stations.py         Sample channel table (edit for your DMA)
  config.py                   Default tuner/antenna/gain config
  tv_player.py                Resilient video player (decoupled A/V clocks)
docs/                         Science explainer, capture recipe, session log
bootstrap.sh                  Linux setup + build + install
```

## License

GPL-3.0-or-later (inherited from gr-dtv).
