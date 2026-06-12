# Software TV Tuner — Raspberry Pi 5 Edition

Watch **live over-the-air ATSC HDTV** on a Raspberry Pi 5 with nothing but an
SDRplay receiver and an antenna. The entire ATSC receiver — tuner, demodulator,
equalizer, Viterbi/Reed-Solomon decoders — runs in **software** (GNU Radio + a
custom DSP fork), decodes in real time on the Pi's four cores, and plays the
1080 HD video with sound on the Pi's own HDMI output.

```
antenna → SDRplay RSPdx → [Pi 5: SDR I/Q → DSP chain → MPEG-TS → mpv] → your TV
```

| Board | Live TV | DVR (record → decode → watch) |
|---|---|---|
| **Pi 5** | ✅ ~1.1× real-time | ✅ |
| Pi 4 | ❌ (~0.4× real-time, hardware floor) | ✅ use `tools/stvt_dvr.sh` |

---

## What you need

- **Raspberry Pi 5** (8 GB tested; 4 GB should work) + **active cooler** +
  official 27 W power supply. Throttling or undervolting will glitch the decode.
- **SDRplay RSPdx** plugged into a **USB 3.0 (blue) port**. (Other RSP models
  may work via the same driver but are untested.)
- A **TV antenna** with coax to the RSPdx **Antenna A** input. ATSC is
  line-of-sight-ish: a decent outdoor/attic antenna beats rabbit ears.
- HDMI display (sound goes over HDMI too).
- **Raspberry Pi OS 64-bit, Desktop** (Debian 13 "trixie" tested). Verify:
  `uname -m` must print `aarch64`.

Find your local channels first: look up your address on
[rabbitears.info](https://www.rabbitears.info) or antennaweb.org and note the
**RF channels** (the real broadcast channel numbers, not the "virtual" 7.1-style
ones) of the strong stations near you.

---

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

> On older Bookworm (Debian 12) the VOLK package is `libvolk2-dev` instead of
> `libvolk-dev`.

Verify GNU Radio is the 3.10 series (required — other ABIs won't load our blocks):

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

> ⚠️ The installer shows its license through the `more` pager — it will appear
> to hang in a non-interactive shell. Run it in a **real terminal**, press
> `q` to leave the license, then `y` to accept.

```bash
sudo systemctl enable --now sdrplay
SoapySDRUtil --find          # should list "SDRplay Dev0 RSPdx ..."
```

If the device isn't found: replug the USB cable (really — the API service
binds at plug-in time), then re-run `SoapySDRUtil --find`.

### 4. SoapySDRPlay3 driver (with the ring-buffer patch)

The stock driver's 2 MiB USB ring buffer overflows under load; the repo ships a
patch script that bumps it to 32 MiB — **the single biggest reliability win**:

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

Custom equalizer / FPLL / sync / Viterbi blocks — this is where the real-time
speed comes from (VOLK auto-selects NEON on the Pi). Takes ~10–20 min on a Pi 5:

```bash
cd ~/Software-TV-Tuner/gr-atscplus
mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
make -j4                  # drop to -j2 on a 4 GB Pi if the linker runs out of RAM
sudo make install && sudo ldconfig
python3 -c "from gnuradio import atscplus; print('atscplus OK')"
```

---

## Watch TV

```bash
cd ~/Software-TV-Tuner
tools/stvt_run.sh 34 3        # RF channel 34, program 3
```

That one command supervises everything: it starts the decode chain, waits for
lock, opens the player on the Pi's screen, and **auto-restarts either half if
it ever dies or the signal glitches**. First picture takes ~30–60 s (RF lock +
equalizer convergence — a "drought" warning or one chain restart during
startup is normal).

**Don't know your RF channel / program numbers?** Rank your local channels by
actual decode quality (records a short clip of each and scores it):

```bash
tools/stvt_dvr.sh scan 7 15 27 31 34 36     # your rabbitears RF list
```

Then list the programs (subchannels) inside the winner — HD programs are the
1920- or 1280-wide ones:

```bash
ffprobe -v error -show_entries program=program_id:stream=width \
  -of compact tools/data/tv_live/live.ts | grep 'width=19\|width=12'
```

**Player keys** (click the video window first): `f` fullscreen · `#` switch
audio language · `9`/`0` volume · `m` mute.

**Stop everything:**

```bash
pkill -f stvt_run.sh; pkill -f tv_live.py; pkill -f stvt_play_hd.sh; pkill -x mpv
```

> ⚠️ Never `kill -9` the chain (`tv_live.py`). A hard kill wedges the SDRplay
> API service and only a USB replug recovers it. The commands above send
> normal signals and shut down cleanly.

---

## Settings

Everything is an environment variable — set it before `stvt_run.sh`:

```bash
STVT_IFGR=48 tools/stvt_run.sh 36 1
```

### Everyday knobs

| Variable | Default | What it does |
|---|---|---|
| `STVT_IFGR` | `50` | IF gain reduction (20–59). **Higher number = less gain.** Try ±4 if a channel won't lock. |
| `STVT_RFGAIN_SEL` | `5` | RF gain step (0–9). Lower it if a very strong local signal clips. |
| `STVT_ANTENNA` | `Antenna A` | RSPdx antenna port (`Antenna A` / `Antenna B` / `Antenna C`). |
| `STVT_ALANG` | `eng,en` | Preferred audio language (`spa` for Spanish SAP). |
| `STVT_AUDIO_DEV` | `alsa/hdmi:CARD=vc4hdmi0,DEV=0` | Audio output. Use `...vc4hdmi1...` for the Pi's second HDMI port. |
| `STVT_FIT` | `85%x85%` | Max window size as % of the screen (the player starts windowed; press `f` for fullscreen). |
| `STVT_ROTATE_GB` | `8` | Recycle `live.ts` at this size. Each rotation is a ~2 s playback blip (~every 55 min at 8 GB). |

### Under-the-hood (already tuned for the Pi 5 — change only if experimenting)

| Variable | Default | What it does |
|---|---|---|
| `STVT_MIN_BUF_BYTES` | `8388608` | Per-edge GNU Radio buffer size. **The Pi 5 live-TV unlock**: stock 32 KB buffers run the 4-core pipeline in lockstep at 0.91× real-time; 8 MB decouples the stages → 1.10×. |
| `STVT_RXF_FUSED` | `1` | Fused resampler+matched-filter front end (one polyphase stage instead of two). |
| `STVT_EQ_S16` | `1` | int16 NEON equalizer data path (−13 % decode time, bit-identical output). |
| `STVT_EQ` | `long` | Equalizer (`long` = quality, `stock` = cheaper). |
| `STVT_SPS` | `1.1` | Samples per symbol through the back half of the chain. |
| `STVT_RRC_SYMS` | `4` | Matched-filter half-span in symbols. |
| `STVT_PLAYER_NICE` | `10` | Player priority handicap so the decode chain always wins the CPU (at equal priority the player causes SDR overflows). |
| `STVT_MPV_VO` | `gpu` | mpv video output driver. |

---

## DVR mode (also: the Pi 4 path)

Recording costs almost no CPU (raw I/Q straight to disk), so any Pi can
record now and decode later — and on a Pi 4 this is the *only* mode:

```bash
tools/stvt_dvr.sh record 34 30 myshow   # record RF34 for 30 minutes
tools/stvt_dvr.sh decode myshow         # offline-decode to a playable .ts
tools/stvt_dvr.sh watch  myshow         # play it
tools/stvt_dvr.sh auto   34 30 myshow   # all three
tools/stvt_dvr.sh list                  # recordings + disk space
```

Mind the disk: raw I/Q is ~1.9 GB/min (cs16). The I/Q is deleted after a good
decode (keep it with `STVT_DVR_KEEP_IQ=1`); for long shows point
`STVT_DVR_DIR` at a USB SSD. `STVT_DVR_RAM=1` records short clips to RAM for
guaranteed-clean capture.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| **No sound** | Sound goes ALSA-direct to HDMI0 by default (PipeWire can't drive the Pi 5's HDMI audio — it shows only a "Dummy Output"; this is normal). TV on the other port? `STVT_AUDIO_DEV=alsa/hdmi:CARD=vc4hdmi1,DEV=0`. Also check the TV isn't muted. |
| **Blocky / glitchy video** | It's almost always the **channel**, not the software — some stations decode 99.99 % clean, others 65 % from the same antenna. Run `tools/stvt_dvr.sh scan ...` and use what scores ≥ 98. Then try `STVT_IFGR` ±4. |
| **"No devices found" / chain can't open the SDR** | Unplug and replug the RSPdx USB (a wedged API service survives everything else). Then check `systemctl status sdrplay`. |
| **Restart loop at startup, picture never comes** | Check `vcgencmd get_throttled` — must be `0x0`. Anything else = power/cooling problem; fix that first. |
| **A brief freeze + jump every ~hour** | That's the `live.ts` rotation blip (see `STVT_ROTATE_GB`). The player recovers in ~2 s on its own. |
| **Player window gone but audio continues** | The supervisor relaunches it within ~20 s; if not, restart with the one-liner in *Stop everything* + `tools/stvt_run.sh`. |
| **Everything was fine yesterday, garbage today** | RF changes day to day. Re-run `scan`; antennas move, trees grow leaves, gain wants re-touching. |

Logs live at `/tmp/stvt_run.log` (supervisor),
`tools/data/tv_live/tv_tuner.tv_live.log` (chain), `/tmp/stvt_mpv.log` (player).

---

## How it fits in this repo

| Branch | Target |
|---|---|
| `main` | Windows |
| `wsl-port-stvt-v2` | Windows + WSL |
| `linux-port-stvt-v3` | Linux x86 desktop |
| **`pi-port-stvt`** | **Raspberry Pi (this README)** |

Pi-specific docs with the measurement history: `docs/raspberry_pi_setup.md`
(feasibility methodology), `docs/pi_dvr.md` (DVR design + channel-quality
findings), `docs/pi_split_decode.md` (Pi-as-SDR-server alternative).
