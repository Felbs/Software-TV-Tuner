# All-on-the-Pi DVR (record → decode → watch)

The Pi 4 can't decode ATSC **live** (~0.33–0.46× real-time — a hard core-count
floor, proven; see memory `pi4_arm_lever_sweep`). But you can still watch TV with
**only the Pi and the SDR**, no second machine, by time-shifting:

1. **Record** raw IQ from the SDR (real-time, ~0 CPU — the SDR just dumps samples).
2. **Decode** that IQ offline on the Pi (slower than real-time, but it finishes).
3. **Watch** the resulting transport stream with mpv.

It's a DVR, not live: a 30-min show takes ~30 min to record + ~70 min to decode,
then you watch. Everything runs on the Pi.

## Use it

```bash
tools/stvt_dvr.sh auto 15 30 tonight     # record 30 min of RF15, then decode
tools/stvt_dvr.sh watch tonight          # play it
# or step by step:
tools/stvt_dvr.sh record 15 30 tonight
tools/stvt_dvr.sh decode tonight
tools/stvt_dvr.sh list                    # recordings + free disk
```

## Channel choice matters — a lot

Measured 2026-06-09: decode quality is dominated by which RF channel you record.
On this antenna **RF34 and RF36 decode at ~99.99% segment alignment (clean 1080
HD)**, while **RF15 sits at ~65% (glitchy)** no matter how you tune gain/EQ — it's
an impaired channel (multipath). If a recording looks blocky, **try a different
channel first** before touching anything else. Good capture gain here is
`STVT_IFGR=50` (IFGR=59 can clip a strong channel). Quick health check on a 8s
RAM capture: decode and `grep segs_aligned` in the log — want >95%.

## Disk is the binding constraint

Raw IQ is **CF32 = ~3.84 GB/min (~230 GB/hr)**. With ~100 GB free that's only
**~26 min** of recording. The decoded `.ts` is tiny by comparison (~65 MB/min),
so `stvt_dvr.sh` **deletes the IQ after a good decode** and keeps only the `.ts`
(override with `STVT_DVR_KEEP_IQ=1`).

For longer shows, record to a **USB SSD/HDD** (a Pi peripheral — still "all on the
Pi"): `STVT_DVR_DIR=/mnt/ssd/dvr tools/stvt_dvr.sh auto 15 60 movie`. A future
`--format cs16` option will halve the IQ size (to ~115 GB/hr) once its amplitude
scaling is verified against the SDR.

## Decode speed / quality

`STVT_DVR_EQ=long` (default) = best quality, ~0.33×. `STVT_DVR_EQ=stock` = ~30%
faster (~0.43×, shorter wait) at a small quality cost — the lever sweep's fastest
config that still decodes. Offline you're not racing the clock, so `long` is the
sensible default; use `stock` if the decode wait is annoying.

## Playback

ATSC video is **MPEG-2**, which the Pi 4 cannot hardware-decode, so mpv decodes it
in software (~1–1.5 cores — fine, since the ATSC chain isn't running during
playback). Multi-program muxes can make mpv pick a bad SD track; if the picture is
tiny/garbled use the HD picker: `tools/stvt_play_hd.sh ~/stvt_dvr/<name>.ts`.

## Optional: overclock to shorten the decode wait

A Pi 4 with good cooling runs stably at 2.0–2.2 GHz (~1.2×), cutting a 70-min
decode to ~58 min. **Needs a reboot — apply by hand, don't automate.** Add to
`/boot/firmware/config.txt`:

```
over_voltage=6
arm_freq=2000      # try 2000; 2147 is the common stable max with active cooling
```

Reboot, then confirm it isn't throttling under load: run a decode and check
`vcgencmd get_throttled` stays `0x0` and `vcgencmd measure_temp` < ~80°C. Back the
numbers off if you see throttling. This does NOT make live decode possible — even
at 2.2 GHz the ceiling is ~0.52×; it just speeds the offline decode.

## Why not "decode live at lower quality / an SD subchannel"?

Doesn't help: the 8-VSB **demod** cost is for the whole 19.4 Mbps mux, fixed
regardless of which program (HD or SD) you ultimately watch. You can't make the
demod cheaper by picking a smaller program. Recording the raw IQ and decoding
offline is the only all-on-Pi path.
