---
name: sdr-usb-throughput-starvation
description: "USB cables break the SDR two ways: STARVATION (long passive extension caps at 2 MS/s, overflows at 6-8, zero video all channels) and LEAKAGE (2026-07-04: cable passes a 3s volume probe at ~96% yet drops micro-bursts — MER stays 19 dB but gaps go 0-3 → 27-33/min). Volume probe alone is NOT sufficient; gap rate during real decode is the continuity meter. Fix: short direct USB 3.0, extend the antenna with coax instead."
metadata: 
  node_type: memory
  type: reference
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

**2026-06-30, the lesson that cost ~2 hours:** an ATSC decode that had worked
(42 MPEG seq-headers on RF31) degraded over a session to **0 video on every
channel** while every channel still showed a **strong shelf (+17 dB) and a clean
carrier lock** (mean|x|≈0.006). We chased the amp (power-cycled it), antenna
aim (full rotation, quality_finder), multipath, RS/EQ modes, and gain
distribution — all red herrings. The real cause was **USB throughput
starvation**: the SDRplay RSPdx was on a **long passive USB extension cord**
(run out a window for reach) and could no longer sustain the 6-8 MS/s that ATSC
needs.

**The fingerprint (recognize this fast next time):**
- `0 video (0 seq-headers) UNIFORMLY across all channels`, but carriers/shelf
  still strong and the FPLL still locks clean. Multipath is channel-specific;
  a *uniform* collapse across the whole band with healthy carriers is a
  **receiver/plumbing** problem, not RF.
- Continuous **`OOOO` overflow markers** printed by SoapySDR/SDRplay, and
  channel scans that **hang / time out** instead of completing.
- A gradual **decline** over the session (42→34→10→…→0 headers) as the marginal
  link degraded, not random fade up-and-down.
- A physical **USB replug does NOT fix it** if you replug into the same slow
  path (long cord / USB-2 port / hub). Re-seating onto a **fast/direct port**
  does.

**The decisive diagnostic — multi-rate throughput probe** (run this EARLY when
overflows or uniform 0-video appear). Open the device, and for rate in
(2e6, 6e6, 8e6): stream 4096-sample reads for ~2-3 s, count `ret==4096`
good reads vs `ret<0` overflow. Result that nailed it:
```
rate=2MS/s  good_reads=883   overflow=0     <- fine
rate=6MS/s  good_reads=0     overflow=7     <- chokes
rate=8MS/s  good_reads=0     overflow=7     <- chokes  (ATSC needs this)
```
Works at 2 but dies at 6-8 = USB can't carry the rate = **it's the cable/port,
not the SDR, antenna, amp, or RF.** (A healthy link probes `good_reads≈5600,
overflow=0` at 8 MS/s.)

**Fix:** SDR on a **short cable straight into a USB 3.0 port** (blue), no long
passive extension, no hub. If reach is needed, keep the SDR near the PC and run
**coax** to the antenna instead, or use a *powered/active* USB 3.0 extension.

**2026-07-04 SECOND FAILURE MODE — the LEAKY cable.** A different long USB
cable *passed* the multi-rate probe (7.67 of 8 MS/s delivered, 0 timeouts,
0 errors over 3 s windows) and the chain locked with MER 18.4–19.3 — yet the
picture glitched constantly. A/B on the same channel/antenna/hour:
**short cable 0–3 gaps/min, long cable 27–33 gaps/min, everything else
identical.** The drops are micro-bursts: too brief to rail the FPLL (max|x|
stayed ~0.28, no rails), but each one snaps sync = missing-packet class.
The probe's "96% delivered" was NOT Python overhead — it likely WAS the
drops. **Volume probes can't clear a cable; only the gap rate during real
decode can.** Also confirmed twice today: the FIRST radio session after any
hot-replug comes up wedged (either `sdrplay_api_Fail` on open, or opens but
delivers zero samples/no fpll lines) and the SECOND works — restart
SDRplayAPIService after every replug and never diagnose from attempt #1.
Lesson pushed to science.md field results (commit cea8be9).

**How to apply:** the moment you see overflow markers, scans that won't finish,
or 0-video-with-strong-carriers across the whole band, STOP tuning RF and run
the throughput probe. Don't power-cycle the amp or re-aim the antenna first —
those wasted the most time here. Related but distinct from
[[sdr-firmware-replug]] (firmware wedge after many restarts — a replug fixes
that; a replug into the same slow port does NOT fix throughput starvation).

Related: [[directional-antenna-atsc-2026-06-30]], [[sdr-firmware-replug]],
[[real-quality-metric-ffmpeg-fps]], [[stvt-play-hd-ffmpeg-probe-lies]],
[[dont-hammer-chain-cpu]].
