---
name: stvt-play-hd-ffmpeg-probe-lies
description: "stvt_play_hd.py uses ffmpeg -probesize 5M -analyzeduration 5M, which on today's RF36 multi-program TS reports 'Invalid frame dimensions 0x0' continuously even though the chain output is perfect. Verify chain health with tsanalyze (file copy), not the player's complaints."
metadata:
  node_type: memory
  type: feedback
  originSessionId: ubuntu-pi-port-2026-06-13
---

**Rule:** if `stvt_play_hd.py` reports `[mpeg2video] Invalid frame dimensions
0x0` continuously, **do NOT** assume the chain is broken. Check with
`tsanalyze` on a file copy first.

**Why:** 2026-06-13 the live-watch verification of the FPLL fold port spent
~20 minutes diagnosing a "chain is producing noise" theory (SDR replug, gain
sweep RFGAIN_SEL=4→5→6, channel swap RF34→RF36, even reverting to pre-port
code). All of it failing the same way: 100% "Invalid frame dimensions 0x0"
in the player log.

Final ground truth from `tsanalyze` (TSDuck) on a copy of live.ts:
```
Bytes: 283,764,192   Services: 7
TS packets: 1,509,384   With invalid sync: 0   With transport error: 0
Service 0x0003: WTTG-DT, MPEG-2 1280x720p @25Hz, 6.1 Mbps  (clear, 0 errors)
```
The chain was **perfect** the whole time. VLC opened directly on `live.ts`
played the video instantly.

**The actual culprit:** `tools/stvt_play_hd.py` builds an
`ffmpeg ... -probesize 5M -analyzeduration 5M ... -map 0:p:3 -c copy
-f mpegts pipe:1 | VLC stdin` pipeline. The 5M probesize is too tight for
today's multi-program TS (7 services, ~24 Mbps mux); ffmpeg can't reliably
find an MPEG-2 sequence header for service 3 in the first 5MB and falls back
to "Invalid frame dimensions" forever. See [[pipe-buffer-breakthrough]] from
the Ubuntu wing — same probe-headroom failure mode.

**Verify chain health using:**
```bash
cp Z:/src/magic-tv-decoder/tools/data/tv_live/live.ts /tmp/copy.ts
tsanalyze /tmp/copy.ts | head -60
```
Look for:
- `With transport error: 0` (chain is clean)
- A service listed with MPEG-2 Video and a non-trivial bitrate (e.g. 6.1 Mbps)
- 0 `Suspect and ignored` packets

**Reliable playback recipe when stvt_play_hd is misbehaving:**
```powershell
& "C:\Program Files\VideoLAN\VLC\vlc.exe" `
  --file-caching=3000 --ts-es-id-pid `
  Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts
```
VLC's native MPEG-TS demuxer handles live/growing files better than ffmpeg's
short-probe pipeline. (No `--programs=3` filter — that one's documented as
unreliable on growing TS in [[vlc-program-filter-unreliable]].)

**How to apply:** when the user reports "the player shows nothing" or
similar, the FIRST diagnostic is `tsanalyze` on a copy of live.ts, NOT
restarting the chain / replugging the SDR / sweeping gains. Save hours.

**Fixing it for real:** `stvt_play_hd.py` should bump `-probesize` and
`-analyzeduration` to at least 50M (~10s buffer at 24 Mbps). That mirrors
what [[multirec-analyze-buffer]] already does for multi-program demux.
Pending — captured here as the known cause.

Related: [[pipe-buffer-breakthrough]], [[multirec-analyze-buffer]],
[[vlc-program-filter-unreliable]], [[noise-fingerprint-check]] (proved
NOT noise this time — fingerprint showed structured data).
