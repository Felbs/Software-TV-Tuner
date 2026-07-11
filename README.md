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

Latest additions (each earned its place through a live A/B):

- **Turbo decoding (stage 2b, "trellis pinning")** — on an RS codeword
  failure the decoder returns to the post-equalizer soft symbols and
  re-runs a full-traceback Viterbi over the affected trellis spans
  with bytes of *successfully decoded* codewords pinned as branch
  constraints, then retries RS+GMD. Converts 50-70% of failed packets
  on marginal signals at ~1-4% CPU, zero measured miscorrections. A
  failure-rate EMA gate stands it down above ~4% channel failure (an
  expensive per-failure rescue path MUST be rate-gated or it
  self-amplifies exactly when the channel is worst — we measured the
  death spiral before adding the gate). `STVT_TURBO=1`, requires the
  SOVA reliability plane (`STVT_SOVA=1`).
- **~10-second channel changes** — three cooperating layers: the SDR
  session persists across retunes (runtime `set_frequency` /
  `set_antenna` on the Soapy source instead of teardown + vendor
  re-handshake; command-file protocol with ack and fallback gates);
  the player tunes from a per-program **PID cache** seeded by every
  scan (a first visit to any channel starts warm); and channels the
  quality model knows are healthy skip the cautious decode-margin
  re-measure. Measured: same-mux hop ~3 s, cross-mux ~9.5 s to
  flowing video; every fallback lands on the classic path.
- **The Knob of Time** — a learned model of diurnal variation:
  per-channel, per-antenna 24-hour quality curves built from the
  decoder's own telemetry (one row per watched minute plus one per
  scan dwell), recency-weighted-median estimator with confidence
  tiers. Full math: [`docs/knob_of_time.md`](docs/knob_of_time.md).
  Optional overnight sweep-trainer, plus a "frontier patrol" that
  re-tries any (channel, antenna) pair whose estimate sits within
  ~2 dB of the decode cliff — which is how two written-off antennas
  got un-written-off (see field results below).
- **Deep Tune, the channel doctor (panel button)** — for any channel,
  watchable or not: baseline, antenna race, bounded gain grid (regime
  and IFGR seed only — the hardware AGC servo owns level tracking),
  failure-class diagnosis, plain-language verdict. Winning recipes
  persist per channel and every future tune consults them.
- **Audited self-knowledge** — the Market Brain card shows how much of
  the local market is mapped and, critically, *audited* forecast
  accuracy: each watched minute on a confidently-modeled channel logs
  a (predicted, measured) MER pair; the card reports the rolling mean
  absolute error. The model's accuracy is a measurement, not a claim.
- **Honest quality labels** — guide badges and the status chip are
  driven by *measured packet loss*, not signal-strength proxies: fast
  faders alias any MER median (sub-second fades against the ~41 Hz
  field-sync sampling) and can read "flawless" while losing 10%/min.
- **Antenna lifecycle controls** — NEW ANTENNA restarts a port's
  learned history when you physically swap hardware (epoch marker;
  old rows archived, never deleted); reset-all-learning is the
  double-confirmed factory reset of every learned artifact.

Field results from the reference market (same wires, better math): a
scanner discone with a long "can't do ATSC" record decoded its VHF-hi
mux nine-for-nine overnight once the modern stack retried it; a
passive panel antenna with a recorded "SNR-limited, undecodable"
verdict locked three muxes and played 0.02%-loss video on its first
modern scan. Moral for fellow experimenters: **re-try condemned
hardware after every decoder improvement — the cliff moves.**

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

## The web UI — TV Tuna panel

Prefer clicking to typing? There's a browser dashboard that wraps the
whole tuner. Start it and open the page:

```powershell
python adaptive-tv\tv_tuna_panel.py
# then open http://localhost:8642 in any browser
```

**Guide tab** — a channel grid built from your last scan; click a
station to tune, and the video opens in its own player window.
**CH+ / CH−** buttons (or ↑/↓ arrow keys) surf the tuneable stations
in guide order on the fast-tune stack. An **antenna picker** lets you
tell the tuner which port's antenna to use (your manual pick always
outranks learned recipes); **🔌 NEW ANTENNA** tells the model you
physically swapped the hardware on a port. **📡 SCAN CHANNELS**
re-surveys the airwaves — scans are planner-ordered from learned
history, refresh the guide's measured-loss badges, and seed the PID
cache for every program in the market; **REC** records the current
channel. **🔬 DEEP TUNE** appears beside the status line for the
tuned channel — including failed tunes, where the doctor earns its
keep.

**NERD tab (Stats for Nerds)** — the live instrument panel, all reading
straight off the decoder:

- **Live MER + signal telemetry** — the decodability dial in dB, updated
  ~1 Hz, plus FPLL lock and equalizer state.
- **🎯 SIGNAL FINDER** — aim-by-ear: a tone whose pitch tracks MER, so
  you can rotate an antenna and hear it lock (Bluetooth-headphone safe).
- **📏 FLATNESS** — in-band ripple meter; a flat band means clean
  multipath, a rippled one means reflections to aim out.
- **🌐 ALL TOWERS** — hops every tower and plays a chord so you can find
  the aim that's fair to all of them at once.
- **📡 ECHO X-RAY** — the channel's impulse response: the main path plus
  every reflection, so you can see multipath instead of guessing.
- **🩺 REPLAY-HEAL (last 30 s)** — snapshots the last ~30 s you just
  watched from the decoder's own IQ ring and re-decodes it offline
  several ways, splicing the best of each pass. Offline means no
  real-time deadline — the one place the heaviest math (e.g.
  decision-directed equalizer tracking) is allowed to live, because
  we measured that running it live costs source overflows.
- **🔬 TUNA SCIENCE** — plain-language cards with your rig's live
  numbers: the **🕰 Knob of Time** card (24 per-hour bars that GROW
  with training data and turn green only when that hour reliably
  decodes — knowledge and verdict are deliberately separate metrics),
  **TURBO RESCUE** (live count of packets recovered by re-decode),
  **REALTIME HEALTH** (source-overflow count; anything above zero
  means the decoder missed its deadline and samples died), and the
  **🧠 MARKET BRAIN** (market coverage + audited forecast accuracy,
  with the reset-all-learning control).

The panel also runs a **chain doctor**: if the vendor SDR service
crashes mid-watch (it happens), the doctor notices the silent chain
death within ~40 s and retunes automatically, with honest banners and
a heal-rate cap.

Everything the panel reports is measured on *your* signal, at *your*
location — nothing is hardcoded to a market.

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
| Steady drizzle of loss on EVERY channel and antenna, MER high, glitch every few seconds | grep the chain log for `OsO` — those are the SDR driver printing **overflow**: the decoder missed its real-time deadline and samples were dropped at the source. Each ~5 ms drop breaks three stages' alignment at once (~300 packets). Cause is CPU load in the chain, not RF and not cables — turn off optional DSP knobs one at a time (a live A/B with the overflow count as the judge). Signature in the raw IQ: pilot *phase* steps with *flat* amplitude |
| Same drizzle, but zero `OsO` in the log | now suspect the USB path: leaky cables pass volume probes while dropping samples under real load. Short direct USB 3.0, different cable, different rear port. Judge by gap-rate during real decode, never by a throughput test |
| Tempted by the impulse blanker (`STVT_NB=1`)? | measured verdict: a 13-arm threshold sweep on a real impulse storm was flat — it doesn't help, and **threshold ≤2.0 silently replaces the whole output with null padding** (full-rate file, zero content). It stays off for a reason; turbo decoding is the impulse weapon that actually measures |
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

Three field lessons that shaped this project, offered to anyone
building live SDR pipelines:

1. **Offline tests cannot validate live DSP.** A feature that wins big
   on recorded signals can still ruin live decode by missing the
   real-time deadline — the driver drops samples, and a dropped sample
   looks like interference in every downstream instrument. Gate every
   live promotion on the source-overflow count, not on replay quality.
   (We learned this the hard way: our best-measuring equalizer upgrade
   was also our mystery glitch.)
2. **The pixels are a measurement layer.** Transport-stream metrics
   can be perfect while the *player's* error-concealment flags smear
   the picture. If viewers say it looks worse and your TS metrics
   disagree, audit the presentation pipeline before the decoder.
3. **Failed-packet rescue rate is a diagnostic.** Turbo decoding
   converts ~70% of impulse-storm failures, ~20% of fading failures,
   and ~0% of sample-loss failures — so the conversion percentage
   tells you *which disease* a channel has before you pick a cure.

## Repo layout

```
gr-atscplus/            GNU Radio OOT module (custom C++ ATSC blocks)
tools/                  tuner CLI, live chain, DVR suite, PSIP/EPG,
                        players, watchdogs  (tv_tuner.py is the entry)
adaptive-tv/            universal antenna calibration + diagnostics
                        (mer_meter, mer_gain_cal, ch_scan, config_
                        shootout, quality_judge, auto_tv, ...) plus
                        tv_tuna_panel.py (web UI) and time_knob.py
                        (learned per-channel hour curves)
docs/                   science explainer + capture recipe
bootstrap.sh            Linux one-shot setup
```

## License

GPL-3.0-or-later (inherited from gr-dtv).
