# Software TV Tuner (STVT) — Linux

A free and open source software TV tuner. Watch free over-the-air
television on an SDR (Software Defined Radio). This is the most
stable open source software TV decoder on the Internet right now.

A custom GNU Radio fork (`gr-atscplus`) decodes ATSC 1.0 broadcast TV
into a live MPEG-TS stream. A CLI launcher (`tv_tuner.py`) scans your
area, builds an on-screen TV guide from the broadcast PSIP/EIT data,
picks a channel, tunes it, and plays it — also records to MP4,
re-streams to RTMP (Twitch, YouTube), changes channels live, and
overlays closed captions in English or Spanish.

It's also a **DVR**: read the on-air program guide, schedule shows,
record whole muxes (several subchannels at once), and browse the
results. A **channel surfer** flips channels right in the player like a
remote, captions can be **overlaid on the picture**, and a **signal
meter** helps you aim an antenna. See the sections below.

The pipeline runs hours of live TV on marginal indoor antennas
without manual intervention: three independent watchdogs (decoder,
ffmpeg, optional player) detect equalizer drift, ffmpeg stalls, and
SDR dropouts and respawn the affected stage automatically. We've
watched full sports games, news blocks, and overnight programming
end-to-end on this stack. If your antenna can lock the carrier, the
software keeps the picture up.

> **Looking for the Windows build?** That lives on the `main` branch.
> This README and branch are Linux-only.

## Install — three steps

Tested on **Ubuntu 22.04 / 24.04** (bare-metal). See the WSL2 note at
the bottom of this section before you start if you're on Windows.

### 1. Clone and bootstrap (~5 minutes)

```bash
git clone -b linux-port-stvt-v3 https://github.com/Felbs/Software-TV-Tuner.git
cd Software-TV-Tuner
chmod +x bootstrap.sh && ./bootstrap.sh
```

`bootstrap.sh` apt-installs GNU Radio 3.10 + ffmpeg + SoapySDR + the
Python bindings, builds and installs the `gr-atscplus` OOT module,
and pip-installs optional player extras. After it finishes:

```bash
python3 -c "from gnuradio import atscplus; print('OK')"   # should print: OK
```

### 2. Install your SDR driver

**SDRplay (RSPdx, RSP1A, RSPduo)** — needs the vendor API *and* a
SoapySDRPlay3 build with our ring-buffer patch. The patch is the
single biggest quality fix on Linux; without it you get OsO sample
overruns several times a second.

```bash
# 2a. SDRplay API v3 (vendor installer)
wget https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-3.15.2.run
chmod +x SDRplay_RSP_API-Linux-3.15.2.run
sudo ./SDRplay_RSP_API-Linux-3.15.2.run
sudo systemctl enable --now sdrplay

# 2b. SoapySDRPlay3 with the enlarged ring buffer
sudo apt-get install -y libsoapysdr-dev
git clone https://github.com/pothosware/SoapySDRPlay3.git

# IMPORTANT: patch the ring buffer BEFORE building. Bumps 2 MiB / 83 ms
# of headroom up to 32 MiB / 1.3 s. Re-run after any SoapySDRPlay3
# upstream pull.
~/Software-TV-Tuner/tools/patch_soapy_ringbuffer.sh ./SoapySDRPlay3

cd SoapySDRPlay3 && mkdir build && cd build
cmake .. && make -j"$(nproc)" && sudo make install && sudo ldconfig
cd ~  # back to home
```

**RTL-SDR, HackRF, BladeRF, Airspy** — `bootstrap.sh` already pulled
the SoapySDR module via apt. Nothing more to do.

Verify your SDR is visible:

```bash
SoapySDRUtil --probe    # should list driver=... + antennas + sample rates
```

If `--probe` finds nothing, the driver isn't installed correctly —
check vendor docs and replug the SDR.

### 3. Per-boot CPU tuning (re-run after every reboot)

The 8-VSB matched filter is single-threaded and pinned to one core.
Linux's default `schedutil` governor + deep C-states starve it,
causing OsO overruns → picture freezes. One script fixes everything:

```bash
sudo tools/fix_linux_tuning.sh
```

It sets the CPU governor to `performance`, disables USB autosuspend
on the SDRplay, raises the realtime priority limit, and holds CPUs
out of deep C-states. On a 2017-era CPU this is the difference
between freezing after ~90 s and running for an hour. On a modern
CPU it matters less but still helps. **Re-run after every reboot** —
the governor resets on boot.

### First run

```bash
python3 tools/tv_tuner.py
```

The interactive picker shows every channel in your DMA grouped by RF
frequency, with on-now show titles, ratings, and signal strength
pulled live from PSIP / EIT after a successful scan. The default
table covers DC/Baltimore — edit `tools/default_stations.py` for
your area's RF channels and callsigns.

For a separate window per stream (so the picker stays clean), make
sure one of `gnome-terminal`, `konsole`, `xfce4-terminal`, or
`xterm` is installed; the launcher detects whichever is available.
Headless environments without a terminal emulator just print the
streaming output inline.

### WSL2 note

The full receive chain (bootstrap → decoder build → SDR enumeration →
scan → equalizer lock) runs cleanly under WSL2 Ubuntu via the
Windows-side SDR exposed through SoapyRemote. **However, sustained
sample-stream integrity over WSL2's NAT loopback is not reliable
enough for end-to-end MPEG-TS decode** — we measured ~1.8% sample
loss + ~22k UDP-buffer overflow events per second, which the FS
checker survives but Reed-Solomon decoding does not. The equalizer
locks textbook-clean but the TS downstream is corrupted, so ffmpeg
never sees a valid program.

This is a WSL2 USB / network passthrough limitation, not a project
limitation. Run it natively: dual-boot Ubuntu, native Linux desktop,
or any Linux machine with USB plugged directly into the host.

## Watch live HD — the proven recipe

This is the exact two-process setup verified end-to-end on bare-metal
Ubuntu (SDRplay RSPdx, native Wayland desktop): one process decodes
the broadcast to a growing `live.ts`, a second plays one program
from it in mpv.

`tv_tuner.py` (the all-in-one launcher with scan + guide + watchdogs)
also works, but the two-process split below is the most robust for
sustained viewing and is what the troubleshooting section assumes.

### Easiest: one command, hands-off (recommended)

```bash
tools/stvt_run.sh 34 3        # RF channel 34, play program 3 (HD 1080)
```

`stvt_run.sh` supervises the whole pipeline for you: it starts the
decoder chain with the lean config, starts the HD player once the
chain locks, and **auto-recovers**. On a modest CPU the chain
periodically slips into a "noise drought" (locks the carrier but
decodes garbage — see Troubleshooting); the supervisor detects it and
restarts the chain, then brings the player back, with a hard cap on
restarts so a dead SDR can't spin forever. A drought shows up as a
~40 s blip (picture freezes, window closes and reopens) instead of a
permanent freeze. Stop everything with:

```bash
pkill -f stvt_run.sh; pkill -f tv_live.py; pkill -f stvt_play_hd.sh
```

The two steps below are what `stvt_run.sh` runs internally — use them
directly if you want to drive the chain and player by hand.

### 1. Start the decoder chain

```bash
cd tools
# Lean real-time config — for modest / older CPUs (e.g. Ryzen 1600X).
# Trades a little decode margin for throughput so the single-thread
# matched filter keeps up. This is the config validated for sustained
# live HD.
export STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long
export STVT_SPS=1.1 STVT_RRC_SYMS=4 STVT_TEISCRUB=0
export STVT_IFGR=59 STVT_RFGAIN_SEL=5 STVT_ANTENNA="Antenna A"
python3 tv_live.py --rf 34          # writes tools/data/tv_live/live.ts
```

On a **fast modern CPU** you can run full quality instead — drop the
lean knobs (`STVT_SPS`, `STVT_RRC_SYMS`, `STVT_TEISCRUB`) and it
defaults to `SPS=1.5`, 8-symbol RRC, TEI scrub on. Pick your channel
with `--rf N` (find N by running `python3 tv_tuner.py --scan` once,
or from your local ATSC channel listings).

### 2. Watch one program in HD

An ATSC channel is a **multiplex of several programs** (often 1–2 HD
1080 + some SD subchannels). Point a player at the raw multi-program
stream and it picks a track at random — you may get "Invalid frame
dimensions 0x0" garbage even though the decode is perfect. The
supervisor below stream-copies **one** program and feeds it to mpv
with a buffer cushion, and self-heals if playback freezes:

```bash
# from the repo root, in a second terminal:
tools/stvt_play_hd.sh 3        # play program 3 (an HD 1080 feed)
```

List the programs to choose one whose video is `1920x1080`:

```bash
ffprobe -show_programs tools/data/tv_live/live.ts
```

`tools/stvt_play_hd.sh [program] [tailMB]` runs `tail -c | ffmpeg
-map 0:p:N -c copy | mpv` with all the flags that took this project
a while to get right (`-flush_packets`, last-N-bytes tail, software
decode, a cache cushion), plus a bounded watchdog that relaunches
the player — never the chain — if it freezes.

### 3. Verify it's actually working

```bash
cd tools/data/tv_live

# OsO (sample-overflow) count should stay LOW and not climb ~1/s:
grep -c OsO tv_tuner.tv_live.log

# The single most useful health check — is the chain emitting real TV
# or noise? A healthy mux has ~20-40 unique PIDs. Hundreds/thousands =
# the chain locked the carrier but is decoding NOISE (a "drought");
# restart the chain.
tail -c 2000000 live.ts | python3 -c '
import sys; d=sys.stdin.buffer.read(); s=set(); i=d.find(b"\x47")
while i+188<=len(d):
    if d[i]==0x47: s.add(((d[i+1]&0x1f)<<8)|d[i+2])
    i+=188 if d[i]==0x47 else 1
print(len(s), "unique PIDs")'
```

### Environment / config reference

All knobs are env vars read by `tv_live.py` (defaults in parentheses):

| Variable | Default | Meaning |
|---|---|---|
| `STVT_RS` | `stock` | Reed-Solomon decoder: `stock` or `erasure` |
| `STVT_VITERBI` | `hard` | Viterbi: `hard` (fast) or `soft` (heavier) |
| `STVT_EQ` | `long` | Equalizer variant |
| `STVT_SPS` | `1.5` | Internal samples/symbol. `1.1` = lean (less CPU); `1.0` breaks timing |
| `STVT_RRC_SYMS` | `8` | Matched-filter RRC half-span. `4` = lean (fewer taps) |
| `STVT_TEISCRUB` | `1` | Rewrite RS-failed packets to NULL. `0` = lean (skip, saves CPU) |
| `STVT_IFGR` | `59` | SDRplay IF gain reduction (20–59 dB) |
| `STVT_RFGAIN_SEL` | `5` | SDRplay LNA stage selector |
| `STVT_ANTENNA` | `Antenna A` | SDRplay antenna port |

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
SoapySDRUtil --probe
```

That should print at minimum a `driver=...` line and list of antennas
+ sample-rate ranges. If it prints nothing or "No supported devices
found", your SoapySDR drivers aren't installed for that SDR — see
your SDR vendor's docs (e.g. SoapySDRPlay3 for SDRplay,
SoapyHackRF for HackRF).

For deep diagnostics, we ship two helper scripts:

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
(`python3 tools/tv_tuner.py --scan`) populates real PSIP data for any
channel that locks, but the static table is what shows up in the
picker before that.

## Run

```bash
# Interactive: banner, channel picker, live channel-changer
python3 tools/tv_tuner.py

# Direct: tune RF36 (Fox 5 DC) and play locally
python3 tools/tv_tuner.py --rf 36

# Pick a subchannel (4.1 NBC = --program 1, 4.4 Oxygen = --program 4)
python3 tools/tv_tuner.py --rf 34 --program 1

# Record to MP4 (no playback window)
python3 tools/tv_tuner.py --rf 36 --no-play --record fox5_news.mp4

# Stream live to Twitch / YouTube / any RTMP destination
python3 tools/tv_tuner.py --config-set twitch rtmp://live.twitch.tv/app/YOUR_KEY
python3 tools/tv_tuner.py --rf 36 --stream twitch

# Closed captions on (English by default, --cc-channel 2 for Spanish)
python3 tools/tv_tuner.py --rf 36 --cc

# Dry-run: print the planned subprocess commands without spawning
python3 tools/tv_tuner.py --rf 36 --dry-run
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
  CEA-708. `sudo apt install ccextractor` to add. Recommended.
- **Bundled pure-Python decoder** (`tools/atsc_cc.py`) — CEA-608
  only, no external deps. Always available. Implements:
  full TS demux (PAT → PMT → video PID), MPEG-2 picture
  reorder by `temporal_reference` so B-frame captions arrive
  in display order, CC1/CC2 channel demux, doubled-control-code
  suppression, and pop-on / roll-up / paint-on mode buffering.

With `tv_tuner.py --cc`, captions appear in their own console window
beside the TV. To overlay them **on the picture** (like a real TV),
use the dedicated mpv launcher:

```bash
# captions burned onto the mpv video via its OSD (program 3, CC1 English)
tools/stvt_watch_cc_osd.sh 3 1
# CC2 (often Spanish on bilingual stations):
tools/stvt_watch_cc_osd.sh 3 2
# tune timing if captions lead/lag the picture:
STVT_CC_DELAY=4.5 tools/stvt_watch_cc_osd.sh 3 1
```

It decodes CEA-608 from the program with `atsc_cc.py` and pushes each
line into the running mpv over its JSON IPC socket. (Verified live on
native Linux — clean real-time English/Spanish captions.) The video
uses mpv's `gpu` VO by default; override with `STVT_MPV_VO` if needed.

If captions don't show, the broadcaster may simply not be transmitting
them on that subchannel (rare for major networks, common for
secondary subchannels and shopping channels).

## DVR — guide, schedule & record

A full set-top-box loop: read the on-air program guide, schedule
shows, record them, and browse the results.

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

The scheduler persists its queue to `~/.tv_tuner/schedule.json`, so a
daemon restart keeps your timers. Recordings are stream-copied (no
re-encode), auto-named from PSIP (e.g. `mux_p3_4_1_WRC-HD_*.ts`).
Because all programs in one mux ride shared transport-stream PIDs,
`stvt_multirec.py` records every program you list from a **single**
SDR tune — you can't, however, record two different RF channels at
once with one SDR. The scheduler defers conflicting cross-mux timers
rather than dropping them.

## Channel surfer

Change channels right in the mpv window like a TV remote — PageUp /
PageDown or the mouse wheel — with captions carried across channels:

```bash
tools/stvt_surf.sh        # PgUp/PgDn or scroll to change channel; Ctrl-C to stop
```

Channels come from your last scan (`~/.tv_tuner/scan.json`), sorted by
virtual channel. Switching to a subchannel in the **same** mux is
instant (no retune); changing mux retunes the SDR (a few seconds, like
any OTA tuner).

## Signal meter (antenna aiming)

A real-time signal-strength readout for pointing an antenna:

```bash
python3 tools/stvt_signal.py --rf 34          # live bars for one channel
python3 tools/stvt_signal.py --scan-band      # sweep the whole UHF band
```

It sniffs the RF every few seconds and renders pilot SNR / VSB lock /
RMS so you can rotate the antenna for the strongest reading before
committing to a watch.

## Troubleshooting

### `SoapySDRUtil --probe` shows no devices

Your SoapySDR driver for that SDR isn't installed. Install the
vendor module:

- **SDRplay**: API v3 from sdrplay.com + SoapySDRPlay3 from source
  (see install step 2 above).
- **HackRF**: `sudo apt install soapysdr-module-hackrf`
- **RTL-SDR**: `sudo apt install soapysdr-module-rtlsdr`

After install, replug the SDR and re-run `SoapySDRUtil --probe`.

### `[scan] phase-1 sweep failed` / "no SDR detected"

Same root cause: SoapySDR can't open the device. Verify with
`SoapySDRUtil --probe` first; if that works but the scan still
fails, another process is holding the SDR open (a stray `tv_live`
or another SoapySDR app — `pkill -f tv_live.py` to clear).

### Scan finds carriers but every channel says "no live.ts growth"

The decoder pipeline started but didn't produce output. Check the
log at `tools/data/tv_live/tv_tuner.tv_live.log` for errors. Common cause:
the antenna port in `config.py` doesn't match what your physical
feed is connected to (silent failure — driver accepts the antenna
name but the port has no signal).

### Carriers found but lock fails ("PAT=0 in 5MB")

Equalizer convergence is probabilistic on weak signals. Try:
1. Re-aim the antenna (point at the transmitter; horizontal-V for
   indoor rabbit ears).
2. Run `python3 tools/tv_tuner.py --rf <strongest_channel>` and let
   it retry up to 6 times — convergence sometimes needs multiple
   cold-starts.
3. Set `STVT_CONVERGENCE_SEC=30` and `STVT_MIN_PAT=3` env vars to
   give weaker signals more time + lower the lock threshold.

### Video stutters / picture freezes briefly

This is normal on marginal signals. The three watchdogs (decoder,
ffmpeg, optional player) auto-respawn the failing stage. If freezes
last more than 10s and don't recover, your SNR is too low — better
antenna or closer to the transmitter.

### Picture froze but `live.ts` is still growing and OsO is low

The **player** starved, not the decoder. This happens if you point
mpv straight at the live edge with no buffer, or feed it through a
pipe that stalls. Use `tools/stvt_play_hd.sh` (above) — it keeps a
buffer cushion and self-heals. Tell-tale: in mpv's status line the
playback clock (1st number) stops advancing while `tail -c live.ts`
shows the file still growing. The chain is fine; restart only the
player.

### Video turned into garbage / random blocks after running a while

The chain fell into a **noise drought**: it still locks the carrier
(FPLL fine, `live.ts` growing, ~0% NULL) but is decoding noise — the
live edge shows hundreds or thousands of unique PIDs instead of
~20-40 (run the PID-count check above). This is usually OsO
accumulation after a long uptime, **not** RF. Fix: restart the
decoder chain.

**`tools/stvt_run.sh` does this automatically** — it watches the live
edge and restarts the chain on a drought, so you rarely need to do it
by hand. To restart manually:

```bash
pkill -f tv_live.py
rm tools/data/tv_live/live.ts            # start a fresh capture
# then relaunch the chain (step 1 above); stvt_play_hd.sh will pick
# it back up
```

If it droughts again quickly, make sure `sudo tools/fix_linux_tuning.sh`
was run this boot and that the SoapySDRPlay3 ring-buffer patch is
applied — both directly reduce OsO. Avoid running other CPU-heavy work
(the matched filter needs a full core); even frequent `ffprobe`/PID
sampling can tip a marginal CPU into more droughts.

### "Unknown codec / PID 0x30" when piping live.ts to ffmpeg

You're either reading the file before the equalizer converged
(wait ~30 seconds after `tv_live` starts), or sample loss in the
SDR-to-decoder path is breaking RS decoding (WSL2 caveat — see the
install section above).

### No window pops up

You need a display server. Ubuntu Desktop has one by default;
Ubuntu Server doesn't. If you're SSH'd in, run `ssh -Y` from your
local machine (X11 forwarding) or install a desktop environment on
the server.

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
tools/
  tv_tuner.py                 Channel picker, player, recorder, streamer,
                              and live channel changer all in one CLI
  tv_live.py                  Continuous SDR → MPEG-TS pipeline
  tv_live_softvit.py          Same pipeline, soft-Viterbi variant
  stvt_run.sh                 Hands-off watcher (chain+player, auto-recovers)
  stvt_play_hd.sh             Single-program HD playback supervisor
  stvt_surf.sh                Channel surfer (PgUp/PgDn / wheel to change)
  stvt_watch_cc_osd.sh        Player with captions overlaid on the picture
  atsc_cc.py                  Pure-Python CEA-608 caption decoder
  stvt_epg.py                 Electronic Program Guide grid (from EIT)
  stvt_schedule.py            DVR scheduler + daemon (queue of timers)
  stvt_multirec.py            Multi-program recorder (whole mux at once)
  stvt_recordings.py          Browse / play / dedupe recordings
  stvt_signal.py              Real-time signal meter for antenna aiming
  sdr_sweep.py                Fast carrier-presence pre-scanner
  atsc_psip.py                PSIP parser (virtual channels + EIT)
  default_stations.py         Sample channel table (edit for your DMA)
  config.py                   Default tuner/antenna/gain config
  tv_player.py                Resilient video player (decoupled A/V clocks)
  fix_linux_tuning.sh         Per-boot CPU governor + USB tuning
  patch_soapy_ringbuffer.sh   SoapySDRPlay3 USB ring-buffer enlargement
  tests/                      Unit tests (DVR, EPG, scheduler, signal, …)
docs/                         Science explainer, capture recipe, session log
bootstrap.sh                  Linux setup + build + install
```

## License

GPL-3.0-or-later (inherited from gr-dtv).
