---
name: noise-fingerprint-check
description: "Before iterating chain config or DSP knobs, fingerprint live.ts to see if the chain is producing pure noise. If noise, no chain config tweak will help — the SDR firmware needs a physical replug. Saves hours of bogus knob-sweeping."
metadata:
  node_type: memory
  type: feedback
  originSessionId: ubuntu-mempalace-import-2026-06-13
---

**Rule:** before iterating chain config / DSP knobs in response to "video gone"
/ "chain isn't working," fingerprint live.ts. If output is pure noise, no
software lever will fix it.

**Why:** 2026-05-22 evening (Ubuntu rig) burned hours sweeping chain configs
(FPLL, EQ, RS, gain, NB, AGC) trying to coax MPEG-2 markers out of the chain.
"Winner" configs produced ~1500-3000 picture markers per 50MB which looked
like "partial decoding". Turned out those markers were RANDOM: every 4-byte
pattern `0x00 0x00 0x01 XX` occurs ~5000 times in 322MB of random data
(322e6 / 256³ ≈ 4912). The chain was producing pure noise the whole time —
SDR firmware in a bad state after multiple daemon restarts. Only fix was
physical USB replug. [[working_recipe_2026_05_22]] on Ubuntu for full repro.

**How to detect (POSIX / bash; adapt for PowerShell on Windows):**
```
LC_ALL=C grep -aoP '\x00\x00\x01.' live.ts | LC_ALL=C sort | LC_ALL=C uniq -c | sort -rn | head -10
```
If ALL 256 suffix bytes have similar counts (within 10% of each other), output
is uniform random = chain is producing noise. If distribution is skewed (a few
bytes dominate), there's real MPEG-TS structure → keep trying chain configs.

**Quick alternative via ffprobe:**
```
ffprobe -v warning -analyzeduration 60M -probesize 200M -f mpegts live.ts 2>&1 | grep -c 'Invalid frame dimensions'
```
Many "Invalid frame dimensions 0x0" with no codec params = noise output.

**What's NOT helpful in noise state:** DSP knob sweeps, ffmpeg/mpv probe
tuning, libx264 re-encode. Encoder needs dimensions which aren't there.

**What CAN help:** physical SDR USB replug (per the Ubuntu recipe), waiting
for RF conditions to improve, trying a different RF channel,
restarting the SDR daemon.

**How to apply:** when the user reports "chain decoded but no video" or
"video stopped," run the fingerprint FIRST. Don't sweep DSP knobs blindly.
Same applies on Windows SDRplay setups — the firmware-stuck failure mode
isn't Linux-specific.

Related: [[scanner-chain-env-gotcha]], [[linux-chain-stability-recipe]].
