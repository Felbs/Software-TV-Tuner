# Magic TV Decoder

Open-source software ATSC 1.0 (broadcast HDTV) receiver. Pure software,
runs on a laptop, decodes free over-the-air HD television from radio
waves picked up by a $100 SDR and a horizontal antenna.

The C++ heart is a fork of GNU Radio's `gr-dtv` plus 5 experimental
blocks (called `gr-atscplus` for historical reasons — that's what the
GNU Radio out-of-tree module is named on disk). The Python wrappers,
recipe scripts, and live-streaming pipeline are this repo's own work.

If the words **8-VSB**, **Hilbert transform**, **trellis-coded
modulation**, **Reed-Solomon**, or **multipath equalizer** mean
nothing to you yet but you want them to, jump straight to
[`docs/science.md`](docs/science.md) — that's a long-form explainer of
every signal-processing step, written for curious readers without an
RF engineering background.

> **Milestone:** software decode of a North-American UHF ATSC station at
> visual quality matching a hardware HDHomeRun reference on the same RF
> moment, using an SDRplay RSPdx + a horizontally-polarized TV antenna +
> the proven gain settings + the `fpll_a002_tau20` combo. **62.2%
> RS-clean, ~1100 HD frames + 1100 SD frames per 60-sec capture** on the
> reference channel. See [`docs/proven_capture_recipe.md`](docs/proven_capture_recipe.md)
> and [`results/`](results/) for the data.
>
> **Live streaming also working** — continuous capture-decode-broadcast
> pipeline serves decoded TS over `tcp://localhost:5559`. VLC with
> `--demux=ts --network-caching=10000` reads the TCP stream forever,
> ~30-45 sec lag end-to-end.

## Quick start

You need a **horizontally-polarized TV antenna** (a discone won't work
— see "Why polarization matters" below), an SDRplay RSPdx (or
compatible SDR), and a Linux box with GNU Radio 3.10+ installed. Pick
a strong UHF station in your area (find one via `tvfool.com` or the
FCC ATSC database for your address); the example below uses an
arbitrary 593 MHz UHF channel, **change it to your station's
frequency**.

```bash
# 1. Capture 60 sec of IQ from your antenna at your station's frequency
rx_sdr -d "driver=sdrplay" -a "Antenna A" \
       -f 593000000 -s 8000000 \
       -g "IFGR=59" -t "rfgain_sel=5" \
       -F CS16 -n 480000000 capture.cs16

# 2. Build the OOT module (one-time)
./bootstrap.sh

# 3. Decode + scrub
python3 run_combo.py capture.cs16 out.ts fpll_a002_tau20
python3 ts_tei_scrub.py out.ts out_clean.ts

# 4. Watch
vlc out_clean.ts
```

The two non-obvious capture values (`rfgain_sel=5`, `IFGR=59`) make
the difference between 100% TEI=1 (ADC saturation, signal looks dead)
and 60%+ RS-clean watchable TV. See [`docs/science.md`](docs/science.md)
for *why*.

## Forked blocks

| Block | Status | Notes |
|---|---|---|
| `atsc_fpll_tight` | ✓ working, +2pp wins | Parameterized alpha + AFC tau. Best params: alpha=0.002, tau=20µs |
| `atsc_viterbi_soft` | ✓ working, neutral-positive | Soft-decision Viterbi; small win on top of tight FPLL |
| `atsc_sync_tunable` | ✓ working, neutral | Lock thresholds + hysteresis; tied stock on the test capture |
| `atsc_fs_checker_inst` | ✓ working | Instrumented (PN511/PN63 histograms to stderr) |
| `atsc_equalizer_long` | ✗ **broken**, 0.3% RS-clean | 256-tap DD-LMS, regression of unknown cause; do not use |

`combos.yaml` defines 29 named combos (stock baseline + 28 forks). A
sweep against a real-RF capture produced this leaderboard:

| Rank | HD frames | SD frames | Clean% | Combo |
|---|---|---|---|---|
| 1 | 1113 | 1094 | 62.2% | **fpll_a002_tau20** |
| 2 | 1112 | 1094 | 62.2% | fpll_soft_a003_tau10 |
| 3 | 1111 | 1094 | 62.1% | tight_fpll_soft_vit |
| ... | | | | |
| stock | 1083 | 1077 | 60.8% | gr-dtv baseline |

## Build

Requires Linux (Ubuntu 24.04 tested) with `gnuradio` (3.10.9+),
`gr-dtv`, `libvolk`, `pybind11`, `cmake`. Use Python 3.12.

```bash
./bootstrap.sh
```

Installs missing prerequisites, builds the OOT module under
`gr-atscplus/build/`, installs to `/usr/local`, verifies the Python
binding lists all 5 forked blocks.

## Regression sweep

After any C++ change to a forked block, run the local sweep against
your captured IQ to make sure stock didn't regress and check whether
your change actually helps:

```bash
bash scripts/run_local_sweep.sh /path/to/capture.cs16
```

Outputs RS-clean %, SD frames, HD frames per combo and a sorted
leaderboard. ~15 minutes for all 29 combos on a 60-sec capture.

## Repo layout

```
gr-atscplus/        forked OOT module (5 C++ blocks)
combos.yaml         29 named combo configurations
run_combo.py        parameterized single-decode runner
benchmark_synth.py  synthetic IQ generator + combo sweep (currently broken — synth IQ produces 0% on stock too; investigation pending)
scripts/run_local_sweep.sh  real-IQ regression sweep with SD/HD frame counts
docs/               capture recipe + radio-science explanation + findings
results/            scoreboard outputs by date
bootstrap.sh        Linux setup + build + install
```

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

## How does this actually work?

See [`docs/science.md`](docs/science.md) for a step-by-step
explanation of the radio science: 8-VSB modulation, why VSB needs
the Hilbert transform, the FPLL carrier-lock loop, equalizer
training vs decision-directed adaptation, soft- vs hard-decision
Viterbi, Reed-Solomon, why gain settings matter so much, and how
to tell from `atsc_fs_checker_inst` output which step in the
pipeline is broken when something goes wrong.

## License

GPL-3.0-or-later (inherited from gr-dtv).
