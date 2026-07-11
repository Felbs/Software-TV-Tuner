---
name: directional-antenna-atsc-2026-06-30
description: "The shed directional antenna DOES decode DC ATSC — RF31 (575 MHz, 720p HD DC mux) on Antenna A WITH its own amp. It is 'amplified only': passive it reads dead floor (+3 dB), amp-on it reads +17-19 dB strong carriers. Decode was proven (42 seq-headers peak) but glitchy/intermittent due to multipath at an indoor-window position. Same session found the [[sdr-usb-throughput-starvation]] trap."
metadata:
  node_type: memory
  type: project
  originSessionId: adaptive-tv-directional-2026-06-30
---

**2026-06-30 (downstairs, directional antenna via long USB cord out the
window).** Contrast with [[lna-passive-antenna-result]]: the user's **shed
directional amplified antenna** genuinely **decodes DC ATSC**, the passive
Total Vision never did.

**Key facts established:**
- **It is "amplified only."** Passive (amp removed) it reads **dead floor**:
  every channel +2 to +4 dB shelf, rawRMS ~0.004. Amp ON: strong carriers —
  **RF31 +17-19 dB**, RF15 +17-19, RF21 +13, RF36 +12, RF34 +10, rawRMS up to
  ~0.017 (≈10× passive, comparable to the attic antenna that decodes). So the
  amp is **mandatory**; don't bother testing this antenna passively.
- **It's on Antenna A** (the RSPdx full-range port), NOT Antenna C. Early port
  scans MISLED me: with the amp OFF, all ports A/B/C read floor and C happened
  to read marginally higher, so I wrongly theorized port-C rolloff. **Lesson:
  scan with the amp ON before drawing port conclusions.** ([[discone-vhf-hi-atsc-works]]
  port-C-rolloff is real for the *discone*, but wasn't the issue here.)
- **RF31 = 575 MHz = a real DC HD mux**: Program 1 is **1280×720 @ 59.94 fps**
  MPEG-2 + AC-3, plus several SD subchannels. This is the channel to use.
- **Decode is PROVEN but glitchy/intermittent**: peak 42 MPEG seq-headers in
  ~22 s (watchable-with-concealment), but fluctuates hard (42→22→3→0) as
  multipath fades in and out at the indoor-window position. Carrier locks clean
  throughout (mean|x| 0.002-0.010); the DATA is what fades. **shelf/carrier
  strength does NOT predict decode** — only the MPEG seq-header count does.

**Winning recipe (reproduce next session):** amp ON → Antenna A → RF31,
`STVT_IFGR=40 STVT_RFGAIN_SEL=3 STVT_EQ=long STVT_VITERBI=soft
STVT_RFNOTCH=1 STVT_DABNOTCH=1 STVT_RS=stock STVT_SPS=1.1 STVT_RRC_SYMS=8
STVT_TEISCRUB=1 STVT_EQ_LKG=1`. Play the marginal signal with
`play_marginal.py 1` (mpv error-concealment). Gain window is narrow: IFGR 40
is the sweet spot — IFGR≥42 drops lock intermittently, IFGR≥48 starves (in_rms
→6, no lock); pulling RF gain out (rfgain_sel 4-6) locks the carrier 100% but
**kills the data** (0 fps), so keep rfgain_sel=3.

**What was NOT solvable in software (the remaining wall):** heavy **multipath**
at the indoor-window location — 8-10 dB of in-band ripple (measured), which
notches the 6 MHz channel and overwhelms FEC even with a +17 dB shelf. Aiming
by strength is useless (shelf pegs at +24). Aiming by the ripple/flatness proxy
(`quality_finder.py`) didn't track decode well either. The **ground-truth aim
signal is the live seq-header rate** — monitor headers-gained-per-few-seconds
while slowly rotating/sliding; a good spot makes ~2 hdr/s, a bad one ~0. Real
fix for steady watchable TV: get this directional antenna **outside/higher with
a clear path to the DC towers (S/SW)**, off the window sill.

**New diagnostic tools built this session** (`Z:\src\adaptive-tv\`):
`ch_scan.py` (sweep all DC UHF channels, shelf+rawRMS per port),
`lock_test.py` / `chan_quality.py` (per-channel FPLL-lock + fps/v_err),
`gaindist_sweep.py` (trade RF vs IF gain to test intermod),
`eq_sweep.py` (compare equalizers — only `long` produced frames here;
cma/multifs/pilot_dd lock but yield 0 video),
`quality_finder.py` (aim-by-ear on in-band spectral RIPPLE = multipath proxy,
Bluetooth-safe continuous tone; useful concept but ripple ≠ decode, prefer the
header-rate monitor), and the multi-rate USB throughput probe from
[[sdr-usb-throughput-starvation]].

**UPDATE 2026-07-01 (MER-era re-test, see [[mer-dial-universal-algorithm]]):**
the full amp/LNA matrix on this antenna, measured by MER on RF31:
**amp only = 15.79 dB (DECODES, champion)**; amp + dead LNA = 14.30 (insertion
loss killed last night's margin — the "filter" inline on 6/30 was the unpowered
RTL-SDR-Blog LNA!); powered LNA only = ≤6.5 (wideband intermod soup); amp +
powered LNA = 13.48 at the SDR's MINIMUM gain, unfixable overload. **Rule: this
antenna runs amp-only, nothing else inline.** New winning recipe 2026-07-01:
rfgain_sel=2 IFGR=36 (NOT 3/40 — the sweet spot moves with any path/position
change; always re-run mer_gain_cal) + config-shootout champion
`STVT_RS=erasure STVT_RS_ERASURES=20 STVT_EQ_QUALITY_BAD_RMS=8` → quality_judge
84 "watchable", sustained play, but scores breathe 22↔84 with the multipath
fade cycle at +0.6 dB margin. Cable-quality still needs the antenna
outside/higher — aim with `mer_meter --tone` (ground truth), not ripple.

Related: [[sdr-usb-throughput-starvation]] (the trap that ate most of this
session), [[lna-passive-antenna-result]] (the passive antenna that CAN'T decode),
[[mer-dial-universal-algorithm]] (the dial + algorithm born from this antenna),
[[discone-vhf-hi-atsc-works]], [[real-quality-metric-ffmpeg-fps]],
[[marginal-antenna-branch-2026-06-20]] (mpv concealment recipe for glitchy playback).
