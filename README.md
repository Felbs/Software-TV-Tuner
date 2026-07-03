# Software TV Tuner (STVT)

A free and open source software TV tuner: watch over-the-air ATSC
broadcast television on any SoapySDR-supported SDR. A custom GNU Radio
module (`gr-atscplus`) decodes 8-VSB into a live MPEG-TS; a CLI
(`tv_tuner.py`) scans, builds a live TV guide from broadcast PSIP/EIT,
tunes, plays, records to MP4, re-streams to RTMP, and overlays closed
captions.

New in this release: **`adaptive-tv/` — a universal tuning layer that
calibrates itself to any antenna.** It measures the live MER
(Modulation Error Ratio) straight out of the decoder's own equalizer,
grid-searches the gain settings, surveys channels, A/Bs the recovery
options, and tells you honestly — in dB — whether an antenna can
decode at your location and what's limiting it if not. See
[The science](#the-science) for how.

---

## Install — Windows (~10 minutes)

**You need:**

1. **GNU Radio 3.10+** — easiest via
   [`radioconda`](https://github.com/ryanvolz/radioconda) (free).
2. **A SoapySDR-supported SDR** — reference setup is an SDRplay RSPdx
   (install the SDRplay API v3 driver from sdrplay.com). RTL-SDR,
   HackRF, Airspy, BladeRF also work (see table below).
3. **ffmpeg** — [full build](https://www.gyan.dev/ffmpeg/builds/)
   extracted to `C:\ffmpeg\`.
4. **Any antenna.** Amplified/directional TV antennas work best, but
   the software adapts to whatever you have — that's the point.

**Steps:**

```powershell
# 1. Clone
git clone https://github.com/Felbs/Software-TV-Tuner.git
cd Software-TV-Tuner

# 2. Build the C++ decoder module (VS 2022 BuildTools + NMake)
gr-atscplus\_build.bat

# 3. Verify the blocks load
python -c "from gnuradio import atscplus; print(dir(atscplus))"

# 4. Player runtime deps
python -m pip install opencv-python sounddevice

# 5. Run
python tools\tv_tuner.py
```

> Building after editing `gr-atscplus` C++? `_rebuild.bat` compiles but
> does **not** install — follow it with `cmake --install` or Python
> imports the stale module.

## Install — Linux (~5 minutes)

Tested on Ubuntu 22.04/24.04 bare metal. `bootstrap.sh` does the whole
setup: apt-installs GNU Radio + ffmpeg + SoapySDR, builds and installs
gr-atscplus, pip-installs player extras.

```bash
git clone https://github.com/Felbs/Software-TV-Tuner.git
cd Software-TV-Tuner
chmod +x bootstrap.sh && ./bootstrap.sh
python3 tools/tv_tuner.py
```

**SDRplay on Linux** needs the vendor API + SoapySDRPlay3 built from
source:

```bash
wget https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-3.15.2.run
chmod +x SDRplay_RSP_API-Linux-3.15.2.run && sudo ./SDRplay_RSP_API-Linux-3.15.2.run
sudo systemctl enable --now sdrplay
sudo apt-get install -y libsoapysdr-dev
git clone https://github.com/pothosware/SoapySDRPlay3.git
cd SoapySDRPlay3 && mkdir build && cd build
cmake .. && make -j"$(nproc)" && sudo make install && sudo ldconfig
SoapySDRUtil --probe   # should list your RSP device
```

**WSL2 is build-only**: the chain builds and locks, but WSL2's USB/NAT
passthrough loses ~1.8% of samples, which Reed-Solomon can't survive.
Run natively.

## Run

```powershell
# Interactive: guide, channel picker, live channel-changer
python tools\tv_tuner.py

# Direct tune + play
python tools\tv_tuner.py --rf 36

# Subchannel select / record / stream / captions
python tools\tv_tuner.py --rf 34 --program 1
python tools\tv_tuner.py --rf 36 --no-play --record news.mp4
python tools\tv_tuner.py --rf 36 --stream twitch
python tools\tv_tuner.py --rf 36 --cc
```

At the interactive prompt: row number or `5.1` tunes, `g` refreshes
the guide, `i 7` inspects a row, `c` cycles captions
(OFF → English → Spanish), `q` quits.

## Tune ANY antenna — the universal layer (`adaptive-tv/`)

The core problem with SDR TV is that every antenna + amp + cable
combination needs different settings, and the difference between
"perfect TV" and "nothing at all" is a ~15.2 dB SNR cliff you
couldn't see... until now. These tools read the decoder's own
equalizer error as a live **MER meter** and calibrate around it:

```powershell
# THE one command: sweep -> classify every carrier -> calibrate gain ->
# judge real decoded quality -> honest verdict + saved antenna profile
python adaptive-tv\tune_antenna.py --name my-antenna
python adaptive-tv\tune_antenna.py --antenna "Antenna B" --biast   # LNA port

# Live MER dashboard on one channel: are we above the 15.2 dB cliff?
python adaptive-tv\mer_meter.py --rf 31

# Aim-by-ear: continuous tone, pitch = MER (880 Hz = decodable).
# Bluetooth-headphone safe. Rotate the antenna for the highest pitch.
python adaptive-tv\mer_meter.py --rf 31 --tone

# Which channels does this antenna see? (per-port carrier scan)
python adaptive-tv\ch_scan.py --antenna "Antenna A"

# Find the optimal gain for THIS antenna chain (grid search on MER)
python adaptive-tv\mer_gain_cal.py --rf 31

# Squeeze a marginal signal: A/B the recovery configs, judged by
# actual decoded fps + error rate
python adaptive-tv\config_shootout.py --rf 31 --ifgr 36 --rfgain 2

# Powering a bias-tee LNA? add --biast to any of the above
# (RSPdx: bias-tee output is on Antenna B only)
```

Supporting diagnostics: `quality_judge.py` (0–100 decode score via
ffmpeg null-decode), `gain_sweep.py` (flat header-count vs gain ⇒
SNR-limited, rising ⇒ gain-starved, falling ⇒ overload),
`lna_probe.py` (per-port signal + is-the-LNA-actually-powered),
`stress_test.py` (quality time-series + telemetry correlation),
`play_marginal.py` (error-concealment player for cliff-edge signals).

**Read the verdicts honestly.** If calibration tops out at MER 10 dB,
that antenna is 5 dB short at that location and no software setting
will fix it — the tools tell you whether the wall is aperture,
overload, multipath, impulse noise, or plumbing, so you fix the right
thing. `tune_antenna.py` classifies every carrier automatically:
**CLEAN** (decodes), **IMPULSE** (good MER but bursty interference —
often the PC itself; move the antenna away from electronics),
**BELOW-CLIFF** (honest dB deficit), **PHANTOM** (a strong shelf that
never field-syncs is not ATSC — don't chase it). Hot amplified/LNA
chains that overload the whole normal gain range are rescued
automatically by extending the search deep into attenuation.

## What SDRs work

| SDR | Notes |
|---|---|
| **SDRplay RSPdx** | reference. 8 MS/s, 14-bit, 3 antenna ports, bias-T on port B |
| **SDRplay RSP1A / RSPduo** | works |
| **RTL-SDR** | strong stations only (max ~2.4 MS/s < ATSC bandwidth) |
| **HackRF One** | works; gain knobs are LNA+VGA instead of IFGR |
| **Airspy R2 / Mini** | works |
| **BladeRF** | works |

`SoapySDRUtil --probe` must list your device before anything else will
work. Configure port/gain defaults in `tools/config.py`
(`tools/probe_sdr.py` prints your device's exact antenna names and
gain ranges).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `--probe` shows no devices | SoapySDR vendor module not installed |
| Scan fails but probe works | another process holds the SDR — kill stray `tv_live.py` |
| Carriers found, no decode, **every** channel | plumbing, not RF: run the multi-rate USB probe (`adaptive-tv` docs). Long/passive USB extensions carry 2 MS/s but starve at 8 MS/s. Short direct USB 3.0 only |
| File grows at full rate but no video | overload garbage — growth ≠ decode. Only MPEG seq-headers count. Recalibrate gain (`mer_gain_cal.py`) |
| Pilot locks, zero data, gain doesn't help | signal below the 15.2 dB data cliff — antenna/aperture problem, measure with `mer_meter.py` |
| Active antenna/LNA reads dead | it isn't powered. Bias-tee LNAs: right port (`--biast`, RSPdx = Antenna B), no DC-blocking filter between SDR and LNA, check orientation (IN = antenna side) |
| Glitchy picture on strong signal | multipath. Try other channels (`ch_scan.py` + `mer_gain_cal.py` per channel), aim with `mer_meter.py --tone`, reposition antenna higher/outside |
| Periodic glitches (every few s) despite clean MER and zero RS errors | impulse noise snapping sync — whole mux slices are never emitted (check continuity-counter gaps, not error counters). Fix: `STVT_NB=1 STVT_NB_THRESHOLD=2.0` (impulse blanker; 2.0 ≈ just above 8-VSB's crest factor — going below ~1.9 blanks the signal itself) |
| SDR dead after hours of restarts / hot attic | thermal or firmware wedge: cool it / replug. Never mount the SDR box in a hot attic — run coax up, not USB |

## The science

[`docs/science.md`](docs/science.md) explains every step for readers
without an RF background: 8-VSB, the Hilbert transform, FPLL carrier
recovery, LMS equalization, soft-decision Viterbi, Reed-Solomon, the
field-sync validation fix — and (new) **section 12.5: MER**, the
equalizer-derived signal-quality dial that powers the universal tuning
algorithm, plus the field-measured antenna/LNA/gain lessons behind it.

[`docs/proven_capture_recipe.md`](docs/proven_capture_recipe.md) has
the reference capture settings.

## Repo layout

```
gr-atscplus/            GNU Radio OOT module (custom C++ ATSC blocks)
tools/                  tuner CLI, live chain, DVR suite, PSIP/EPG,
                        players, watchdogs  (tv_tuner.py is the entry)
adaptive-tv/            universal antenna calibration + diagnostics
                        (mer_meter, mer_gain_cal, ch_scan, config_
                        shootout, quality_judge, auto_tv, ...)
docs/                   science explainer + capture recipe
bootstrap.sh            Linux one-shot setup
```

## License

GPL-3.0-or-later (inherited from gr-dtv).
