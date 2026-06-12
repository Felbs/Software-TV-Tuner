# Software TV Tuner (STVT) — Raspberry Pi / ARM

A free and open source software TV tuner. Watch free over-the-air
television on an SDR (Software Defined Radio) — **entirely on a
Raspberry Pi 5**. This is the most stable open source software TV
decoder on the Internet right now.

A custom GNU Radio fork (`gr-atscplus`) decodes ATSC 1.0 broadcast TV
into a live MPEG-TS stream, in real time, on the Pi's four ARM cores
(VOLK NEON kernels + an int16 equalizer data path + tuned GNU Radio
buffering). One command starts a supervised pipeline that tunes,
decodes, and plays 1080 HD with sound on the Pi's own HDMI output —
and auto-recovers from signal glitches, player stalls, and SDR
hiccups without your help.

It's also a **DVR**: read the on-air program guide, schedule shows,
record whole muxes (several subchannels at once), and browse the
results. A **channel surfer** flips channels right in the player like a
remote, captions can be **overlaid on the picture**, and a **signal
meter** helps you aim an antenna. See the sections below.

```
antenna → SDRplay RSPdx → [Pi 5: SDR I/Q → DSP chain → MPEG-TS → mpv] → your TV
```

| Board | Live TV | DVR (record → decode → watch) |
|---|---|---|
| **Pi 5** | ✅ ~1.1× real-time | ✅ |
| Pi 4 | ❌ (~0.4× real-time, hardware floor) | ✅ use `tools/stvt_dvr.sh` |

> **Other platforms** live on their own branches: `main` = Windows,
> `wsl-port-stvt-v2` = Windows + WSL, `linux-port-stvt-v3` = x86 Linux
> desktop. This branch (`pi-port-stvt`) and this README are
> Raspberry Pi / ARM only.

## What you need

- **Raspberry Pi 5** (8 GB tested; 4 GB should work) + **active
  cooler** + official 27 W power supply. Throttling or undervolting
  will glitch the decode.
- **SDRplay RSPdx** plugged into a **USB 3.0 (blue) port** (other
  SoapySDR radios work too — see "Configure for your SDR" below; the
  RSPdx is the reference setup).
- A **TV antenna** with coax to the SDR input. See "Antennas — what
  works" below.
- HDMI display (sound goes over HDMI too).
- **Raspberry Pi OS 64-bit, Desktop** (Debian 13 "trixie" tested).
  Verify: `uname -m` must print `aarch64`.

Find your local channels first: look up your address on
[rabbitears.info](https://www.rabbitears.info) and note the **RF
channels** (real broadcast channel numbers, not the "virtual"
7.1-style ones) of the strong stations near you.

## Install (copy-paste, top to bottom)

### 1. System packages

```bash
sudo apt update
sudo apt install -y \
  gnuradio gnuradio-dev \
  cmake build-essential git pkg-config \
  libsoapysdr-dev soapysdr-tools \
  libvolk-dev pybind11-dev python3-numpy python3-packaging \
  ffmpeg mpv
```

> On older Bookworm (Debian 12) the VOLK package is `libvolk2-dev`
> instead of `libvolk-dev`.

Verify GNU Radio is the 3.10 series (required — other ABIs won't load
our blocks):

```bash
gnuradio-config-info --version   # expect 3.10.x
```

### 2. Clone this repo

```bash
cd ~
git clone -b pi-port-stvt https://github.com/Felbs/Software-TV-Tuner.git
cd Software-TV-Tuner
```

### 3. SDRplay API (vendor driver)

Download the **Linux ARM64** API installer from
<https://www.sdrplay.com/api/> (`SDRplay_RSP_API-Linux-3.x.x.run`, the
aarch64 build), then:

```bash
chmod +x SDRplay_RSP_API-Linux-*.run
sudo ./SDRplay_RSP_API-Linux-*.run
```

> ⚠️ The installer shows its license through the `more` pager — it
> will appear to hang in a non-interactive shell. Run it in a **real
> terminal**, press `q` to leave the license, then `y` to accept.

```bash
sudo systemctl enable --now sdrplay
SoapySDRUtil --find          # should list "SDRplay Dev0 RSPdx ..."
```

If the device isn't found: replug the USB cable (really — the API
service binds at plug-in time), then re-run `SoapySDRUtil --find`.

(Using an RTL-SDR / HackRF / Airspy instead? Skip this step and
`sudo apt install soapysdr-module-rtlsdr` or `-hackrf` / `-airspy`.)

### 4. SoapySDRPlay3 driver (with the ring-buffer patch)

The stock driver's 2 MiB USB ring buffer overflows under load; the
repo ships a patch script that bumps it to 32 MiB — **the single
biggest reliability win**:

```bash
cd ~
git clone https://github.com/pothosware/SoapySDRPlay3
~/Software-TV-Tuner/tools/patch_soapy_ringbuffer.sh ~/SoapySDRPlay3
cd SoapySDRPlay3 && mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
make -j4
sudo make install && sudo ldconfig
```

### 5. Build the DSP blocks (gr-atscplus)

Custom equalizer / FPLL / sync / Viterbi blocks — this is where the
real-time speed comes from (VOLK auto-selects NEON on the Pi). Takes
~10–20 min on a Pi 5:

```bash
cd ~/Software-TV-Tuner/gr-atscplus
mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
make -j4                  # drop to -j2 on a 4 GB Pi if the linker runs out of RAM
sudo make install && sudo ldconfig
python3 -c "from gnuradio import atscplus; print('atscplus OK')"
```

No per-boot CPU tuning is needed on the Pi 5 (unlike the x86
branches) — but **cooling is**: check `vcgencmd get_throttled` prints
`0x0` whenever something seems off.

## Watch live TV

```bash
cd ~/Software-TV-Tuner
tools/stvt_run.sh 34 3        # RF channel 34, program 3
```

That one command supervises everything: it starts the decode chain
with the Pi-tuned config, waits for lock, opens the player on the
Pi's screen, and **auto-restarts either half if it ever dies or the
signal glitches**. First picture takes ~30–60 s (RF lock + equalizer
convergence — a "drought" warning or one chain restart during startup
is normal). Roughly once an hour the capture file recycles; the
player relaunches itself for a clean ~10 s blip and carries on.

**Don't know your RF channel / program numbers?** Rank your local
channels by actual decode quality (records a short clip of each and
scores it):

```bash
tools/stvt_dvr.sh scan 7 15 27 31 34 36     # your rabbitears RF list
```

Then list the programs (subchannels) inside the winner — HD programs
are the 1920- or 1280-wide ones:

```bash
ffprobe -v error -show_entries program=program_id:stream=width \
  -of compact tools/data/tv_live/live.ts | grep 'width=19\|width=12'
```

**Player keys** (click the video window first): `f` fullscreen · `#`
switch audio language · `9`/`0` volume · `m` mute.

**Stop everything:**

```bash
pkill -f stvt_run.sh; pkill -f tv_live.py; pkill -f stvt_play_hd.sh; pkill -x mpv
```

> ⚠️ Never `kill -9` the chain (`tv_live.py`). A hard kill wedges the
> SDRplay API service and only a USB replug recovers it. The commands
> above send normal signals and shut down cleanly.

### Settings

Everything is an environment variable — set it before `stvt_run.sh`:

```bash
STVT_IFGR=48 tools/stvt_run.sh 36 1
```

Everyday knobs:

| Variable | Default | What it does |
|---|---|---|
| `STVT_IFGR` | `50` | IF gain reduction (20–59). **Higher number = less gain.** Try ±4 if a channel won't lock. |
| `STVT_RFGAIN_SEL` | `5` | RF gain step (0–9). Lower it if a very strong local signal clips. |
| `STVT_ANTENNA` | `Antenna A` | RSPdx antenna port (`Antenna A` / `Antenna B` / `Antenna C`). |
| `STVT_ALANG` | `eng,en` | Preferred audio language (`spa` for Spanish SAP). |
| `STVT_AUDIO_DEV` | `alsa/hdmi:CARD=vc4hdmi0,DEV=0` | Audio output. Use `...vc4hdmi1...` for the Pi's second HDMI port. |
| `STVT_FIT` | `85%x85%` | Max window size as % of the screen (player starts windowed; press `f` for fullscreen). |
| `STVT_MPV_MUTE` | `no` | `yes` = start muted (unattended/overnight runs). |
| `STVT_ROTATE_GB` | `8` | Recycle `live.ts` at this size (≈55 min of TV per rotation). |

Under-the-hood (already tuned for the Pi 5 — change only if
experimenting):

| Variable | Default | What it does |
|---|---|---|
| `STVT_MIN_BUF_BYTES` | `8388608` | Per-edge GNU Radio buffer size. **The Pi 5 live-TV unlock**: stock 32 KB buffers run the 4-core pipeline in lockstep at 0.91× real-time; 8 MB decouples the stages → 1.10×. |
| `STVT_RXF_FUSED` | `1` | Fused resampler+matched-filter front end (one polyphase stage instead of two). |
| `STVT_EQ_S16` | `1` | int16 NEON equalizer data path (−13 % decode time, bit-identical output). |
| `STVT_EQ` | `long` | Equalizer (`long` = quality, `stock` = cheaper). |
| `STVT_SPS` | `1.1` | Samples per symbol through the back half of the chain. |
| `STVT_RRC_SYMS` | `4` | Matched-filter half-span in symbols. |
| `STVT_RS` / `STVT_VITERBI` | `stock` / `hard` | Reed-Solomon / Viterbi variants (the lean, validated pair). |
| `STVT_PLAYER_NICE` | `10` | Player priority handicap so the decode chain always wins the CPU. |
| `STVT_ROTATE_RELAUNCH` | `1` | Deterministic player relaunch when `live.ts` rotates (`0` = ride through). |
| `STVT_MPV_VO` | `gpu` | mpv video output driver. |
| `STVT_DEINT` | `lowdeint` | 1080i deinterlacing. `lowdeint` (default) = half-res decode + yadif: no combing, plays clean. `low` = half-res only (faintest residual comb). `no` = full-res but wavy lines on 1080i motion. `frame`/`field` = full-res deint — measured too heavy for the Pi 5 (thousands of dropped frames). Progressive 720p channels are never touched. |

## Configure for your SDR + antenna

The defaults assume an **SDRplay RSPdx with the TV antenna on
"Antenna A"**. If you have a different SDR or your feed is on a
different physical port, edit a few constants in `tools/config.py`.

### What SDRs work

This project decodes anything **SoapySDR** supports. Tested in-house
(on x86; the Soapy layer is identical on ARM):

| SDR | Notes |
|---|---|
| **SDRplay RSPdx** | reference setup. 8 MS/s, 14-bit, three antenna ports |
| **SDRplay RSP1A / RSPduo** | works; one antenna port (RSP1A) or two (RSPduo) |
| **RTL-SDR (R820T2 dongle)** | strong stations only; max ~2.4 MS/s is below ATSC's full bandwidth so SNR margin shrinks |
| **HackRF One** | works; 8 MS/s; gain naming differs (no IFGR — LNA + VGA stages) |
| **Airspy R2 / Mini** | works; 10 MS/s; gain ladder names differ |
| **BladeRF** | works; expensive but excellent SNR |

To check what SoapySDR sees:

```bash
SoapySDRUtil --probe
```

For deep diagnostics, two helper scripts:

```bash
python3 tools/probe_sdr.py            # antennas, sample-rate, gain elements
python3 tools/probe_throughput.py     # streaming sustained-rate test
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

If your SDR has only one port, the value doesn't matter much — but it
does have to be a string the driver recognizes, or the call fails
silently.

### Gain settings

The two SDRplay knobs (env vars `STVT_IFGR` / `STVT_RFGAIN_SEL`
override the `tools/config.py` defaults):

- `IFGR` — IF gain **reduction**, 20–59 dB. Higher = less gain. The
  Pi reference antenna runs `50`.
- `RFGAIN_SEL` — LNA stage selector, 0–9.

Other SDRs use different names: **HackRF** `LNA` (0–40 dB) + `VGA`
(0–62 dB); **RTL-SDR** a single `TUNER` gain (0–49 dB) + AGC bool;
**Airspy** `LNA`/`MIX`/`VGA` or `linearity`/`sensitivity` presets.

Rule of thumb: **start with a strong UHF station (RF 14–36)**, set
gain so the raw signal sits at about 60–80 % of the ADC's range, and
verify lock. Too high → clipping → equalizer fails. Too low →
quantization noise → no lock. The honest measure of a channel/gain
combo is `tools/stvt_dvr.sh scan` — it scores actual decode quality
(`segs_aligned`), not just signal bars.

### Configure for non-DC markets

Edit `tools/default_stations.py` to match your area's RF channels +
callsigns. The format is documented in the file. The first scan
(`python3 tools/tv_tuner.py --scan`) populates real PSIP data for any
channel that locks, but the static table is what shows up in the
picker before that.

## Run — the all-in-one CLI

`tv_tuner.py` bundles scan, guide, picker, player, recorder, and
channel changer:

```bash
# Interactive: banner, channel picker, live channel-changer
python3 tools/tv_tuner.py

# Direct: tune RF36 and play locally
python3 tools/tv_tuner.py --rf 36

# Pick a subchannel
python3 tools/tv_tuner.py --rf 34 --program 1

# Record to MP4 (no playback window)
python3 tools/tv_tuner.py --rf 36 --no-play --record fox5_news.mp4

# Closed captions on (English by default, --cc-channel 2 for Spanish)
python3 tools/tv_tuner.py --rf 36 --cc

# Dry-run: print the planned subprocess commands without spawning
python3 tools/tv_tuner.py --rf 36 --dry-run
```

> **Pi budget note:** the live decode chain needs ~91 % of the Pi 5's
> four cores. Stream-copy outputs (recording, the mpv player) fit in
> the rest; anything that **re-encodes video** (e.g. RTMP push to
> Twitch/YouTube, which works on the x86 branches) does not — don't
> expect it to keep up here. Likewise avoid heavy desktop work while
> watching live; a busy browser can tip the chain into overflows.

## Live channel-changer

The interactive picker doubles as a remote: pick a channel, watch it,
then back at the picker prompt type another row number or virtual
channel — the running TV instantly retunes. Single-keystroke commands
at the prompt:

| key | action |
|-----|--------|
| `5` | tune the 5th row in the guide |
| `5.1` | tune virtual channel 5.1 |
| `g` | reprint the guide (refreshes show titles + signal %) |
| `i 7` | inspect row 7 (signal detail, all PIDs, EIT-now/next) |
| `c` | cycle captions: OFF → English (CC1) → Spanish (CC2) |
| `q` | quit |

Spanish captions on bilingual stations come through on CC2 / SAP —
the `c` cycle is the fastest way to switch.

## Closed captioning

Two backends, picked automatically:

- **`ccextractor`** if installed on PATH — handles both CEA-608 and
  CEA-708. `sudo apt install ccextractor` to add. Recommended.
- **Bundled pure-Python decoder** (`tools/atsc_cc.py`) — CEA-608
  only, no external deps. Always available.

With `tv_tuner.py --cc`, captions appear in their own console window
beside the TV. To overlay them **on the picture** (like a real TV):

```bash
# captions burned onto the mpv video via its OSD (program 3, CC1 English)
tools/stvt_watch_cc_osd.sh 3 1
# CC2 (often Spanish on bilingual stations):
tools/stvt_watch_cc_osd.sh 3 2
# tune timing if captions lead/lag the picture:
STVT_CC_DELAY=4.5 tools/stvt_watch_cc_osd.sh 3 1
```

If captions don't show, the broadcaster may simply not be
transmitting them on that subchannel.

## DVR

Two complementary recorders:

### TS-level DVR (guide, schedule & record) — while the chain runs

```bash
# Electronic Program Guide — a printable grid from the broadcast EIT
# (run a scan first: python3 tools/tv_tuner.py --scan)
python3 tools/stvt_epg.py                 # next few hours, all channels
python3 tools/stvt_epg.py --rf 34 --hours 6
python3 tools/stvt_epg.py --watch         # live-refreshing grid

# Schedule recordings, then run the daemon that fires them on time
python3 tools/stvt_schedule.py tv         # pick shows from the guide
python3 tools/stvt_schedule.py list       # show the queue
python3 tools/stvt_schedule.py run        # daemon: records each show at its start time

# Record one mux right now (all programs share the 6 MHz channel, so
# you can grab several subchannels at once for the price of one tune)
python3 tools/stvt_multirec.py --rf 34 --duration 1800 --programs 3,4,5

# Browse / play / dedupe what you recorded
python3 tools/stvt_recordings.py          # interactive browser
python3 tools/stvt_dvr_play.py 3          # play a recorded program
```

Recordings are stream-copied (no re-encode), auto-named from PSIP.
One SDR = one mux at a time; the scheduler defers conflicting
cross-mux timers rather than dropping them.

### Raw-IQ DVR (`stvt_dvr.sh`) — also the Pi 4 path

Recording raw I/Q costs almost no CPU, so any Pi can record now and
decode later — and on a Pi 4 (which can't decode live) this is the
*only* mode:

```bash
tools/stvt_dvr.sh record 34 30 myshow   # record RF34 for 30 minutes
tools/stvt_dvr.sh decode myshow         # offline-decode to a playable .ts
tools/stvt_dvr.sh watch  myshow         # play it
tools/stvt_dvr.sh auto   34 30 myshow   # all three
tools/stvt_dvr.sh list                  # recordings + disk space
```

Mind the disk: raw I/Q is ~1.9 GB/min. The I/Q is deleted after a
good decode (`STVT_DVR_KEEP_IQ=1` keeps it); for long shows point
`STVT_DVR_DIR` at a USB SSD. `STVT_DVR_RAM=1` records short clips to
RAM for guaranteed-clean capture.

## Channel surfer

Change channels right in the mpv window like a TV remote — PageUp /
PageDown or the mouse wheel — with captions carried across channels:

```bash
tools/stvt_surf.sh        # PgUp/PgDn or scroll to change channel; Ctrl-C to stop
```

Channels come from your last scan (`~/.tv_tuner/scan.json`). Switching
to a subchannel in the **same** mux is instant; changing mux retunes
the SDR (a few seconds, like any OTA tuner).

## Signal meter (antenna aiming)

```bash
python3 tools/stvt_signal.py --rf 34          # live bars for one channel
python3 tools/stvt_signal.py --scan-band      # sweep the whole UHF band
```

It renders pilot SNR / VSB lock / RMS every few seconds so you can
rotate the antenna for the strongest reading before committing to a
watch.

## Troubleshooting

| Symptom | Fix |
|---|---|
| **No sound** | Sound goes ALSA-direct to HDMI0 by default (PipeWire can't drive the Pi 5's HDMI audio — it shows only a "Dummy Output"; this is normal and expected). TV on the other port? `STVT_AUDIO_DEV=alsa/hdmi:CARD=vc4hdmi1,DEV=0`. Also check the TV isn't muted. |
| **Blocky / glitchy video** | It's almost always the **channel**, not the software — some stations decode 99.99 % clean, others 65 % from the same antenna. Run `tools/stvt_dvr.sh scan ...` and use what scores ≥ 98. Then try `STVT_IFGR` ±4. |
| **`SoapySDRUtil --probe` shows no devices / chain can't open the SDR** | Unplug and replug the SDR's USB (a wedged SDRplay API service survives everything else, including service restarts). Then check `systemctl status sdrplay`. For non-SDRplay radios: `sudo apt install soapysdr-module-<your radio>`. |
| **Restart loop at startup, picture never comes** | Check `vcgencmd get_throttled` — must be `0x0`. Anything else = power/cooling problem; fix that first. |
| **A brief player restart about once an hour** | That's the `live.ts` rotation (`STVT_ROTATE_GB`); the player deliberately relaunches for a clean ~10 s blip. Nothing to do. |
| **Picture froze but `live.ts` is still growing** | The **player** starved, not the decoder; the supervisor relaunches it within ~30 s on its own. The chain is fine — never restart the chain for a player problem. |
| **Video turned into garbage / random blocks after running a while** | A "noise drought": the chain locks the carrier but decodes noise (live edge shows hundreds of unique PIDs instead of ~25–40). `stvt_run.sh` detects and restarts the chain automatically (~40 s blip). If it droughts repeatedly, check cooling/throttling and stop other CPU-heavy work — even frequent `ffprobe` sampling can tip the chain over. |
| **Carriers found but lock fails ("PAT=0")** | Equalizer convergence is probabilistic on weak signals: re-aim the antenna, try the strongest channel from `scan`, or give it more time with `STVT_CONVERGENCE_SEC=30`. |
| **Everything was fine yesterday, garbage today** | RF changes day to day. Re-run `scan`; antennas move, trees grow leaves, gain wants re-touching. |

Logs live at `/tmp/stvt_run.log` (supervisor),
`tools/data/tv_live/tv_tuner.tv_live.log` (chain),
`/tmp/stvt_mpv.log` (player), `/tmp/stvt_play_hd.sup.log` (player
supervisor).

## Watchdogs

Layers that keep playback alive on marginal signals, all bounded so a
dead SDR can't cause an infinite respawn storm:

- **`stvt_run.sh`** — the top-level supervisor: restarts the chain on
  death or noise-drought (with a grace window for the chain's own
  in-DSP re-acquire), and brings the player supervisor back when the
  chain is healthy.
- **`stvt_play_hd.sh`** — the player supervisor: relaunches the
  `tail | ffmpeg | mpv` pipeline when mpv dies, when playback freezes
  (~30 s with no progress), and proactively at each `live.ts`
  rotation for a clean cushion + clock.
- **In-chain re-acquire** — the field-sync checker re-acquires framing
  after lock loss instead of latching into permanent noise (the fix
  that made unattended multi-hour viewing possible).

## How does this actually work?

[`docs/science.md`](docs/science.md) is a long-form explainer of every
signal-processing step, written for readers without an RF engineering
background: 8-VSB modulation, the FPLL carrier-lock loop, the LMS
equalizer, soft-decision Viterbi, Reed-Solomon, and the field-sync
spacing-validation fix that finally made it watch a baseball game.

Pi-specific docs with the measurement history:
[`docs/raspberry_pi_setup.md`](docs/raspberry_pi_setup.md)
(feasibility methodology), [`docs/pi_dvr.md`](docs/pi_dvr.md) (DVR
design + channel-quality findings),
[`docs/pi_split_decode.md`](docs/pi_split_decode.md)
(Pi-as-SDR-server alternative).

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
mounted whip catches a horizontally-polarized wave at maybe 10–20 %
of the energy of a properly-oriented horizontal antenna — roughly
**10–15 dB of signal loss**. For a marginal station that can be the
difference between a clean picture and no lock at all.

So a **proper TV antenna helps** — and "proper" here means two
things:

- **Horizontally polarized** (the elements run side-to-side, not
  up-and-down). Indoor rabbit-ears bent into a horizontal "V" work
  surprisingly well; a purpose-built UHF Yagi or log-periodic gives
  the best SNR margin.
- **Connected with proper coax**, ideally short, ideally low-loss
  (RG-6 or LMR-style) with the right connector for your SDR.

Both make the receive side easier. **Neither is required** — if your
station is loud enough at your location, a "wrong" antenna often
still works.

## Repo layout

```
gr-atscplus/                  GNU Radio OOT module (custom C++ blocks)
tools/
  stvt_run.sh                 THE command: supervised live TV (chain+player)
  stvt_play_hd.sh             Single-program HD playback supervisor
  stvt_dvr.sh                 Raw-IQ DVR: record/decode/watch/scan (Pi 4 path)
  tv_live.py                  Continuous SDR → MPEG-TS pipeline
  tv_replay.py                Deterministic offline decode of recorded I/Q
  tv_tuner.py                 Channel picker, player, recorder, channel changer
  stvt_surf.sh                Channel surfer (PgUp/PgDn / wheel to change)
  stvt_watch_cc_osd.sh        Player with captions overlaid on the picture
  atsc_cc.py                  Pure-Python CEA-608 caption decoder
  stvt_epg.py                 Electronic Program Guide grid (from EIT)
  stvt_schedule.py            DVR scheduler + daemon (queue of timers)
  stvt_multirec.py            Multi-program recorder (whole mux at once)
  stvt_recordings.py          Browse / play / dedupe recordings
  stvt_signal.py              Real-time signal meter for antenna aiming
  atsc_psip.py                PSIP parser (virtual channels + EIT)
  default_stations.py         Sample channel table (edit for your DMA)
  config.py                   Default tuner/antenna/gain config
  patch_soapy_ringbuffer.sh   SoapySDRPlay3 USB ring-buffer enlargement
  tests/                      Unit tests (DVR, EPG, scheduler, signal, …)
docs/                         Science explainer, Pi setup + DVR docs
```

## License

GPL-3.0-or-later (inherited from gr-dtv).
