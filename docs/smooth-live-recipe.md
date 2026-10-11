# Smooth live HD on a CPU-bound box — the measured recipe

The live ATSC chain is a **sequential DSP pipeline** (matched filter → FPLL →
equalizer → Viterbi → RS). On a 6-core-class x86 box it is CPU-bound: even fully
leaned out it decodes RF at roughly **90–95 % of real time**, and that shortfall
shows up as periodic glitches. This is the config that makes it watch *smoothly*
anyway, plus the numbers that justify each knob. Validated on a Ryzen 1600X
(6c/12t) + SDRplay RSPdx, 2026-07.

## The recipe

Set these on top of `tools/stvt_run.sh` (it already forces the lean chain —
`STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long` — and the FPLL CPU folds):

```sh
export STVT_RXF_FUSED=1     # fuse resampler + matched filter → one fixed-ratio stage
export STVT_SPS=1.1         # lean matched filter (stvt_run's default 1.3 is heavier)
export STVT_RRC_SYMS=4      # short RRC half-span
export STVT_CACHE_SECS=12   # player time-cushion: rides out re-acquire dips
export STVT_MPV_HWDEC=auto  # GPU video decode frees the chain's CPU cores
export STVT_MPV_SMOOTH=1    # player-side smoothing
export STVT_MPV_EC=1        # error concealment (interpolate lost macroblocks)
export STVT_CC=1            # closed captions on (extracts embedded EIA-608)
# Do NOT set STVT_CPU_ISOLATE=1 on a 6-core box — see below.
tools/stvt_run.sh 36 3
```

The per-antenna gain (`STVT_IFGR` / `STVT_RFGAIN_SEL` / `STVT_ANTENNA`) is
**machine-specific** — generate it with `tools/stvt_autocal.py`, which writes
`~/.stvt_autocal.env`. `stvt_run.sh` sources that file when `STVT_IFGR` is unset,
so the cleanest setup is to put *all* of the knobs above into
`~/.stvt_autocal.env` alongside the gain and then just run `tools/stvt_run.sh`.

## Why each knob — measured (RF36, lean chain, live.ts growth = real-time %)

| Change | Real-time rate | Note |
|---|---|---|
| bare lean, no fusion | 91.6 % | this box's floor at the leanest tier |
| **+ `STVT_RXF_FUSED=1`** | **95.5 %** | the arbitrary polyphase resampler was the front-end CPU hog; the fused fixed-ratio resampler-with-RRC-taps replaces it. Locks clean. |
| end-to-end, with player, **no CPU isolation** | **95.4 %** | GPU decode keeps mpv at ~15 % CPU so the chain gets the cores |
| with `STVT_CPU_ISOLATE=1` | 85 % | **worse** — pinning the chain into 4 physical cores forces SMT-sibling contention on a sequential pipeline. Leave isolation OFF here. |

`STVT_EQ=stock` vs `long` made **no** CPU difference (95.4 vs 95.5 %), so keep
`long` for its multipath robustness — the equalizer is not the live bottleneck at
`SPS=1.1`; the front-end resampler was.

The remaining ~4.6 % is the hardware ceiling (confirmed repeatedly — a sequential
DSP path can't be parallelized away). The **12-second cache cushion** plus
**error concealment** absorb it, so the *picture* is smooth even though the chain
runs just under real time. A hardware-demod tuner (HDHomeRun) or a faster box
(N100/N305) is the only way to reach a true 100 %.

## Captions

ATSC carries EIA-608/708 captions **embedded in the MPEG-2 video** (A/53), not as
a separate stream. `STVT_CC=1` passes `--sub-create-cc-track=yes` to mpv, whose
ffmpeg CC decoder extracts them into a selectable sub track. Verify a broadcast
actually carries them with:

```sh
ffmpeg -f lavfi -i "movie=prog.ts[out0+subcc]" -map 0:1 out.srt   # readable text = CC present
```

Note: `tools/atsc_cc.py` (the OSD-bridge decoder) only works on 1080i/30 fps and
is **silent on 720p60** — use mpv's built-in CC path (above) for 60p programs.

## Endurance

An 8.5-hour overnight soak of this exact config (`~/stvt_soak.sh`, 171 samples):
**0** chain restarts, **0** player/supervisor drops, **0** SDR wedges, flat
chain RSS (no leak), 79–84 °C (no thermal throttle), sustained ~95 %. The
`stvt_run.sh` single-instance flock guard prevents the two-supervisors-fighting-
over-the-SDR failure mode.
