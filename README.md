# Software TV Tuner (STVT)

A free and open source software TV tuner. Watch free over-the-air
television on an SDR (Software Defined Radio). This is the most
stable open source software TV decoder on the Internet right now.

A custom GNU Radio fork (`gr-atscplus`) decodes ATSC 1.0 broadcast TV
into a live MPEG-TS stream. A CLI launcher (`tv_tuner.py`) scans your
area, builds an on-screen TV guide from the broadcast PSIP/EIT data,
picks a channel, tunes it, and plays it — also records to MP4,
re-streams to RTMP (Twitch, YouTube), changes channels live, and
overlays closed captions in English or Spanish.

The pipeline runs hours of live TV on marginal indoor antennas
without manual intervention: three independent watchdogs (decoder,
ffmpeg, optional player) detect equalizer drift, ffmpeg stalls, and
SDR dropouts and respawn the affected stage automatically. We've
watched full sports games, news blocks, and overnight programming
end-to-end on this stack. If your antenna can lock the carrier, the
software keeps the picture up.

## Download & install (Windows, ~10 minutes)

You need:
- A computer with **GNU Radio 3.10+**, easiest via
  [`radioconda`](https://github.com/ryanvolz/radioconda) (free).
- A SoapySDR-supported SDR. Tested with **SDRplay RSPdx** + the
  SDRplay API v3 driver.
- **Any antenna** — even ones that weren't designed for TV (we've
  locked broadcasts on a vertical ham-radio whip). A proper
  horizontally-polarized TV antenna gives the best signal margin,
  but it's not required. See
  [Antennas — what works](#antennas--what-works) below.
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

## Download & install (Linux, ~5 minutes)

Tested on Ubuntu 22.04 / 24.04 (build + decoder pipeline validated;
end-to-end watchable picture verified on bare-metal Linux. WSL2 has
a known-issue caveat documented below). The `bootstrap.sh` script
does the full setup in one shot — apt-installs GNU Radio + ffmpeg +
SoapySDR + the Python bindings, builds and installs the gr-atscplus
OOT module, and pip-installs optional player extras.

```bash
git clone https://github.com/Felbs/Software-TV-Tuner.git
cd Software-TV-Tuner
chmod +x bootstrap.sh && ./bootstrap.sh

# Run it
python3 tools/tv_tuner.py
```

SDRplay-specific install steps and the WSL2 caveat are above.

For a separate window per stream (so the picker stays clean), make
sure one of `gnome-terminal`, `konsole`, `xfce4-terminal`, or
`xterm` is installed; the launcher detects whichever is available.
Headless / WSL2 environments without a terminal emulator just print
the streaming output inline — usable, just less pretty.

### Validated on bare-metal Linux; WSL2 is build-only

The full receive chain (bootstrap → decoder build → SDR enumeration →
two-phase scan → equalizer lock with `min_pn511_err = 0`) runs
cleanly under WSL2 Ubuntu via the Windows-side SDR exposed through
`tools/soapy_server.bat` + SoapyRemote. **However, sustained
sample-stream integrity over WSL2's NAT loopback is not reliable
enough for end-to-end MPEG-TS decode** — we measured ~1.8% sample
loss + ~22k UDP-buffer overflow events per second, which the FS
checker survives but Reed-Solomon decoding does not. The result:
the equalizer locks textbook-clean but the TS bytes downstream are
corrupted, so ffmpeg/ffplay never sees a valid program. This is a
WSL2 USB / network passthrough limitation, not a project limitation.

Run it natively: dual-boot Ubuntu, native Linux desktop, or a Linux
machine with USB plugged directly into the host. SDRplay's API +
`SoapySDRPlay3` install per their docs (vendor `.run` installer +
build SoapySDRPlay3 from source against `libsoapysdr-dev`):

```bash
wget https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-3.15.2.run
chmod +x SDRplay_RSP_API-Linux-3.15.2.run
sudo ./SDRplay_RSP_API-Linux-3.15.2.run
sudo systemctl enable --now sdrplay

sudo apt-get install -y libsoapysdr-dev
git clone https://github.com/pothosware/SoapySDRPlay3.git
cd SoapySDRPlay3 && mkdir build && cd build
cmake .. && make -j"$(nproc)" && sudo make install && sudo ldconfig

SoapySDRUtil --probe   # should list your RSP* device
```

RTL-SDR, HackRF, BladeRF, and other SoapySDR devices work out of
the box from `bootstrap.sh`'s apt packages. For any SDR, run
`SoapySDRUtil --probe` to confirm the device is visible before
launching `tv_tuner.py`.

The interactive picker shows every channel in your DMA grouped by RF
frequency, with on-now show titles, ratings, and signal strength
pulled live from PSIP / EIT after a successful scan. **Before** the
first scan succeeds (or if no SDR is plugged in), the picker falls
back to a **static table** in `tools/default_stations.py` so you
have something to look at — these names are hardcoded, not scraped
from the air. The default table covers DC/Baltimore; edit it for
your DMA. Real PSIP/EIT data (live show titles, ratings, signal %)
populates only after the SDR successfully tunes a station.

## Configure for your SDR + antenna

The defaults assume an **SDRplay RSPdx with the TV antenna on
"Antenna A"**. If you have a different SDR or your feed is on a
different physical port, edit a few constants in `tools/config.py`.

### What SDRs work

This project decodes anything **SoapySDR** supports. Tested in-house:

| SDR | Notes |
|---|---|
| **SDRplay RSPdx** | reference setup. 8 MS/s, 14-bit, three antenna ports |
| **SDRplay RSP1A / RSPduo** | works; one antenna port (RSP1A) or two (RSPduo) |
| **RTL-SDR (R820T2 dongle)** | works for strong stations only; max sample rate is ~2.4 MS/s, which is below ATSC's full bandwidth so SNR margin shrinks. Fine for nearby transmitters. |
| **HackRF One** | works; 8 MS/s available; gain naming differs (no IFGR — uses LNA + VGA gain stages) |
| **Airspy R2 / Mini** | works; 10 MS/s; gain ladder names differ |
| **BladeRF** | works; expensive but excellent SNR |

To check what SoapySDR sees on your machine:

```bash
SoapySDRUtil --probe       # Linux / Mac
SoapySDRUtil.exe --probe   # Windows (in radioconda's terminal)
```

That should print at minimum a `driver=...` line and list of antennas
+ sample-rate ranges. If it prints nothing or "No supported devices
found", your SoapySDR drivers aren't installed for that SDR — see
your SDR vendor's docs (e.g. SoapySDRPlay3 for SDRplay,
SoapyHackRF for HackRF).

For deep diagnostics, we ship two helper scripts:

```bash
python tools/probe_sdr.py            # antennas, sample-rate, gain elements
python tools/probe_throughput.py     # streaming sustained-rate test
```

### Pick the right antenna port

In `tools/config.py`:

```python
ATSC_ANTENNA = "Antenna A"   # SDRplay RSPdx port label
```

The string must match what your SDR's driver advertises. Run
`tools/probe_sdr.py` to see the exact names. Examples:

- **SDRplay RSPdx**: `Antenna A`, `Antenna B`, `Antenna C`, or `HiZ`
- **SDRplay RSP1A**: just `RX` (single port)
- **HackRF**: `TX/RX` (single port)
- **RTL-SDR**: `RX` (single port)

If your SDR has only one port, the value doesn't matter much — but
it does have to be a string the driver recognizes, or the call
fails silently. If `tools/probe_sdr.py` prints `('A', 'B', 'C')`
instead of `('Antenna A', 'Antenna B', 'Antenna C')`, use the
short-form names.

### Gain settings

The two knobs in `tools/config.py`:

```python
ATSC_IF_GAIN_REDUCTION = 59   # SDRplay-specific; range 20-59 dB
ATSC_RFGAIN_SEL        = 5    # SDRplay-specific; LNA stage selector
```

Both are SDRplay terminology. Other SDRs use different names:

- **HackRF**: replace with `LNA` (0–40 dB, 8 dB steps) and `VGA`
  (0–62 dB, 2 dB steps).
- **RTL-SDR**: a single `TUNER` gain (0–49 dB), and AGC bool.
- **Airspy**: `LNA`, `MIX`, `VGA` (0–15 each), or `linearity` /
  `sensitivity` presets.

Rule of thumb: **start with a strong UHF station (RF 14–36)**, set
gain so the raw signal sits at about 60–80% of the ADC's range, and
verify lock. Too high → clipping → equalizer fails. Too low →
quantization noise → no lock. The included `tools/probe_sdr.py`
prints the device's full gain range so you can pick a starting
point.

### Configure for non-DC markets

Edit `tools/default_stations.py` to match your area's RF channels +
callsigns. The format is documented in the file. The first scan
(`python tools/tv_tuner.py --scan`) populates real PSIP data for any
channel that locks, but the static table is what shows up in the
picker before that.

## Run

```powershell
# Interactive: banner, channel picker, live channel-changer
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

# Closed captions on (English by default, --cc-channel 2 for Spanish)
python tools\tv_tuner.py --rf 36 --cc

# Dry-run: print the planned subprocess commands without spawning
python tools\tv_tuner.py --rf 36 --dry-run
```

`tv_tuner.py` uses ffmpeg's `tee` muxer so one command can play
locally, record, and push to RTMP simultaneously without re-encoding
twice.

## Live channel-changer

The interactive picker doubles as a remote: pick a channel, watch
it, then back at the picker prompt type another row number or
virtual channel — the running TV instantly retunes to the new
station without restarting from scratch. Single-keystroke commands
at the prompt:

| key | action |
|-----|--------|
| `5` | tune the 5th row in the guide |
| `5.1` | tune virtual channel 5.1 (`WTTG` Fox) |
| `g` | reprint the guide (refreshes show titles + signal %) |
| `i 7` | inspect row 7 (signal detail, all PIDs, EIT-now/next) |
| `c` | cycle captions: OFF → English (CC1) → Spanish (CC2) |
| `q` | quit |

Spanish captions on bilingual stations (Univision, Telemundo,
WFDC, WZDC) come through on CC2 / SAP — the `c` cycle is the
fastest way to switch.

## Closed captioning

Two backends, picked automatically:

- **`ccextractor`** if installed on PATH — handles both CEA-608 and
  CEA-708. `winget install ccextractor` to add. Recommended.
- **Bundled pure-Python decoder** (`tools/atsc_cc.py`) — CEA-608
  only, no external deps. Always available. Implements:
  full TS demux (PAT → PMT → video PID), MPEG-2 picture
  reorder by `temporal_reference` so B-frame captions arrive
  in display order, CC1/CC2 channel demux, doubled-control-code
  suppression, and pop-on / roll-up / paint-on mode buffering.

Captions appear in their own console window beside the TV. If
captions don't show, the broadcaster may simply not be transmitting
them on that subchannel (rare for major networks, common for
secondary subchannels and shopping channels).

## Troubleshooting

### `SoapySDRUtil --probe` shows no devices

Your SoapySDR driver for that SDR isn't installed. Install the
vendor module:

- **SDRplay**: API v3 from sdrplay.com + SoapySDRPlay3 from source
  (see Linux install steps above).
- **HackRF**: `apt install soapysdr-module-hackrf` (Linux) or
  `radioconda` already includes it (Windows).
- **RTL-SDR**: `apt install soapysdr-module-rtlsdr` or via radioconda.

After install, replug the SDR and re-run `SoapySDRUtil --probe`.

### `[scan] phase-1 sweep failed` / "no SDR detected"

Same root cause: SoapySDR can't open the device. Verify with
`SoapySDRUtil --probe` first; if that works but the scan still
fails, another process is holding the SDR open (a stray `tv_live`
or another SoapySDR app — `pkill -f tv_live.py` to clear).

### Scan finds carriers but every channel says "no live.ts growth"

The decoder pipeline started but didn't produce output. Check the
log at `data/tv_live/tv_tuner.tv_live.log` for errors. Common cause:
the antenna port in `config.py` doesn't match what your physical
feed is connected to (silent failure — driver accepts the antenna
name but the port has no signal).

### Carriers found but lock fails ("PAT=0 in 5MB")

Equalizer convergence is probabilistic on weak signals. Try:
1. Re-aim the antenna (point at the transmitter; horizontal-V for
   indoor rabbit ears).
2. Run `python tools/tv_tuner.py --rf <strongest_channel>` and let
   it retry up to 6 times — convergence sometimes needs multiple
   cold-starts.
3. Set `STVT_CONVERGENCE_SEC=30` and `STVT_MIN_PAT=3` env vars to
   give weaker signals more time + lower the lock threshold.

### Video stutters / picture freezes briefly

This is normal on marginal signals. The three watchdogs (decoder,
ffmpeg, optional player) auto-respawn the failing stage. If freezes
last more than 10s and don't recover, your SNR is too low — better
antenna or closer to the transmitter.

### "Unknown codec / PID 0x30" when piping live.ts to ffmpeg

You're either reading the file before the equalizer converged
(wait ~30 seconds after `tv_live` starts), or sample loss in the
SDR-to-decoder path is breaking RS decoding (WSL2 caveat — see
the Linux section above).

### No window pops up on Linux

ffplay needs an X server. Ubuntu Desktop has one by default; Ubuntu
Server doesn't. If you're SSH'd in, run `ssh -Y` from your local
machine (X11 forwarding) or install a desktop environment on the
server. WSLg (WSL2 on Windows 11) provides X11 automatically.

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

## Antennas — what works

**You can use this on antennas that weren't designed to receive TV
signals.** Our test rig regularly locks ATSC broadcasts on a vertical
ham-radio whip — exactly the kind of antenna conventional wisdom says
shouldn't work for TV. With a strong-enough station, a clean front
end, and the watchdogs respawning the decoder when it drifts, the
software pulls a watchable picture out of antennas that off-the-shelf
HDHomeRun-style tuners would give up on.

But — what *does* polarization mean, and why do TV antennas help?

Radio waves carry their energy in an electric field that oscillates
in some direction perpendicular to the direction the wave is
traveling. The orientation of that oscillation is the wave's
**polarization**. ATSC broadcast TV in North America is transmitted
**horizontally polarized**: the field oscillates left-to-right.

For maximum reception, the receiving antenna's element should be
oriented in the *same plane* as the transmitted wave. A vertically-
mounted whip catches a horizontally-polarized wave at maybe
10–20% of the energy of a properly-oriented horizontal antenna —
roughly **10–15 dB of signal loss**. That's a lot. For a marginal
station it can be the difference between a clean picture and no
lock at all.

So a **proper TV antenna helps** — and "proper" here means two
things:

- **Horizontally polarized** (the elements run side-to-side, not
  up-and-down). Indoor rabbit-ears bent into a horizontal "V" work
  surprisingly well; a purpose-built UHF Yagi or log-periodic gives
  the best SNR margin.
- **Connected with proper coax**, ideally short, ideally low-loss
  (RG-6 or LMR-style) with the right F-connector or N-connector
  matching for your SDR. Long thin coax + bad connectors throws
  away signal you can't get back.

Both of these make the receive side easier. **Neither is required
to use this program** — if your station is loud enough at your
location, a "wrong" antenna often still works. The watchdogs and
the equalizer's tracking margin cover a lot of sins.

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
