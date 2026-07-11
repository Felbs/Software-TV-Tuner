---
name: real-quality-metric-ffmpeg-fps
description: "When measuring decode quality, DON'T trust HD_READS / grep counts / chain-side metrics. Always use ffmpeg null-sink frame counting. Half a Fable-era session was burned chasing equalizer changes because HD_READS=0 looked like 'no video' when actual decode was ~21 fps."
metadata:
  node_type: memory
  type: feedback
  originSessionId: ubuntu-mempalace-import-2026-06-13
---

**Rule:** the only trustworthy quality metric for ATSC decode is **ffmpeg
null-sink frame count**. Do NOT trust chain-internal counters or grep-based
resolution sniffers as a quality measure.

**Why:** Ubuntu session 2026-05-2x spent ~half a session chasing equalizer /
sync algorithm changes because `HD_READS=0` from the chain's parse script
looked like "no video decoded." Actual video was decoding at ~21 fps the
whole time — the metric was lying. The HD_READS counter greps ffmpeg output
for resolution strings (`1920x1080|1280x720`) which:
- Only appear when ffprobe successfully **reports** stream parameters
  (depends on probesize/analyzeduration, not actual decode rate)
- Are absent even for known-good streams when probe windows are short
- Show 0 for HDHomeRun ground-truth recordings — the reference is broken too

**The actual measurement command:**
```
ffmpeg -hide_banner -loglevel warning -stats \
       -analyzeduration 100M -probesize 100M \
       -t 25 -i <ts_file> \
       -f null - 2>&1 | grep "frame="
```
`frame=N` / `time=X` = real decoded-fps. Reference points (from the Ubuntu
rig, RF34 strong):
- HDHomeRun (HW ASIC): ~29 fps (the ceiling)
- Pre-patch Linux chain: ~15 fps (~50% of HDHR — was being called "glitchy")
- Post-patch: ~23 fps (~80% of HDHR — visibly better)

**How to apply:** any time the user asks "is the decode working / better"
on Linux, Windows, or Pi — quote the ffmpeg null-sink fps, not a chain
counter or visual hand-wave. Same gauge across platforms = comparable
results. Bonus: the chain config that maximises this number is often
DIFFERENT from the one that maximises the chain's own internal metrics.

Related: [[x86-cpu-savings-ceiling]], [[vlc-program-filter-unreliable]]
(another "the player's signal isn't the ground truth" lesson).
