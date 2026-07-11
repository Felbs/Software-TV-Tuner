---
name: lna-passive-antenna-result
description: "Resolved 2026-06-29: an external LNA improved the passive Total Vision EZ-HD antenna from marginal to a CLEAN pilot lock but STILL zero ATSC data decode — it's data-cliff/SNR limited, and an LNA fixes noise figure (cleaner pilot) but cannot add the wideband SNR needed to cross the 8-VSB data cliff. The same LNA on a real (but collapsed) attic antenna DID decode. Antenna aperture/capture is the wall, not amplification."
metadata:
  node_type: memory
  type: project
  originSessionId: adaptive-tv-lna-2026-06-29
---

**INVALIDATED 2026-07-01 — THE LNA WAS NEVER POWERED IN THIS TEST.** The LNA is an
RTL-SDR Blog Wideband LNA: **bias-tee powered ONLY, no external power input.** The
6/29 "externally powered (bias-T made no difference)" conclusion was a misread:
the test ran on **Antenna A, which has NO bias-tee on the RSPdx** (bias-T exists on
Antenna B only), so toggling `biasT_ctrl` did nothing and the LNA was a passive
attenuator all session. Proof (2026-07-01): on Antenna B, biasT 0→1 raised the noise
floor **+22.2 dB** — the LNA visibly waking. Every "LNA" result below therefore
measured a DEAD LNA. Kept for the diagnostics/tools; the passive-antenna-with-LIVE-LNA
experiment re-ran 2026-07-01 **with the LNA genuinely powered** (bias-T on
Antenna B): passive Total Vision best = **RF31 MER 10.14 dB, RF15 8.65, RF18 0
— still ~5 dB below the 15.2 cliff.** So the 6/29 APERTURE conclusion survives,
now with honest numbers: 22 dB of real low-noise gain lifts signal and noise
together; the missing wideband SNR is capture area, full stop. ALSO proven 2026-07-01 on the directional antenna: LNA-only = MER ≤6.5 dB
(wideband intermod soup), amp+LNA = unfixable overload even at min SDR gain; the
antenna's own TV-band amp alone (MER 15.79) beats every LNA combination — never stack
a wideband LNA behind an amplified antenna.

The LNA we recommended (see [[antb-wsl-task]], [[adaptive-tv-project]]) arrived. Tested
on Windows 2026-06-29. ~~The LNA is **externally powered** (bias-T 0→1 made no measured
difference — `lna_probe.py`), so no `biasT_ctrl` needed.~~ **WRONG — see above.**

**Two configs tested, both as: antenna → LNA → SDR Antenna A.**

1. **Attic antenna, COLLAPSED elements (its own amp removed, LNA in its place):**
   - Shelf RF34 = +6.4 dB, rawRMS ~0.019. DECODES. Calibrated IFGR=40 (auto), 16–17
     seq-headers, plays HD (prog 3/5 = 1920×1080) + SD subs. Quality glitchy (~50/100,
     ~24 fps, score bounces 33–59). `gain_sweep.py`: header count **FLAT** across
     17→37 dB of gain → SNR/capture limited, NOT gain-starved. More amp won't help.
     The glitching is the collapsed element capturing little; **extending it is the
     lever**, not electronics.

2. **Passive Total Vision EZ-HD, exterior window sill:**
   - Shelf probe (IFGR=40): carriers present — RF36 **+7.7 dB** (strongest), RF17 +6.6,
     RF33 +6.1, RF31 +5.7. Shelf ≈ attic's, but rawRMS ~0.003 (≈10× LOWER absolute
     capture than the attic antenna).
   - Decode on RF36: IFGR=38 → dead (`mean|x|=0.00000`, `in_rms=5.8`, no lock).
     IFGR=28 → **clean pilot lock** (`mean|x|≈0.005`, `in_rms=23`, NCO tracking) but
     **0 transport data after 62 s**. IFGR=24 → overload (`mean|x|` rises to 0.075,
     `max|x|` rails 1.05), still 0 data. `multifs_dd` EQ + erasure RS@20 didn't help.
     RF17/33/31 didn't even pilot-lock as well. **Result: pilot locks, data never does.**

**Verdict (the central ANT-B question, answered):** the LNA *measurably helped* — it took
the passive antenna from "barely locks" to a **clean pilot lock** (that's the noise-figure
improvement an LNA buys). But pilot lock was never the blocker; **wideband 8-VSB DATA
recovery is**, and that's raw SNR the LNA can't manufacture. A clean narrowband pilot with
`mean|x|≈0.005` AND zero TS output is the unmistakable fingerprint of a signal sitting
right below the data cliff. Consistent with the attic `gain_sweep` (amplification ≠ SNR).

**AIMING experiment (same session, the real test of the cliff):** had the user aim the
passive antenna by ear with a continuous-tone finder. The shelf rose **+6.5 → +13 dB**
(aiming adds REAL signal, unlike gain) and the carrier lock went from the phantom
`mean|x|=0.005` to a genuinely **clean `mean|x|=0.025` with healthy `max|x|=0.21`** —
as clean as the attic antenna that decodes. **And STILL zero video headers**, across a
full RF-gain×IF-gain re-sweep AND every data-directed equalizer (pilot_dd, pilot_dd_soft,
cma, multifs_dd) + erasure RS@20. So: aiming + LNA bought a beautiful CARRIER lock but
the WIDEBAND DATA is still under the cliff. This is the hard wall — a small passive indoor
antenna can't deliver the wideband SNR for ATSC data here, no matter how clean the pilot.
Gain is touchy at the peak: RFsel=2 overloads to garbage (`max|x|`→π/2, file grows full-
rate with 0 headers = the classic overload-garbage trap), RFsel=4-5 won't lock; only
RFsel=3 / IFGR~32 gives the clean carrier — but clean carrier ≠ decodable data.

**Bluetooth-safe finder:** `winsound.Beep` (discrete beeps) CUTS OUT on Bluetooth
headphones — the BT link power-saves in the gaps and clips each beep. Fix = `tone_finder.py`
(NEW, `Z:\src\adaptive-tv\`): a CONTINUOUS sounddevice sine whose pitch glides with the
shelf, so the audio stream never idles. Use this for aim-by-ear, not `find_signal.py`.

**How to apply / what would actually work:** the wall is **antenna aperture/capture**, not
gain. The passive Total Vision is physically too small to grab enough wideband energy at
this location. Real levers: (a) a real outdoor/attic UHF antenna with actual gain+aperture,
(b) the [[discone-vhf-hi-atsc-works]] Comet DS-150S on port A/B + LNA for UHF (more capture
than the Total Vision), (c) much better siting/height/aim. An LNA is only worth adding to
an antenna that already captures near the cliff (like the attic one) — it can't rescue a
tiny passive indoor antenna that's well below it. **2026-07-02 FINAL NAIL: even that carve-out is
dead.** On the attic antenna (bare = MER 19.5, already +4 above cliff in-room),
adding the powered LNA gained ZERO MER (19.3), cost quality (26 vs 100), and its
wideband floor blinded the carrier scan (real channels RF31/36 vanished, phantoms
survived). Verdict across every antenna tested: **the RTL-SDR Blog wideband LNA
never once improved decode.** Below cliff it can't bridge; above cliff it only
hurts. Its remaining use is as a lab tool, not part of any TV chain.

**New diagnostic tools this session** (`Z:\src\adaptive-tv\`): `gain_sweep.py` (flat
header-count-vs-gain ⇒ SNR-limited; rising ⇒ gain-starved; falling ⇒ overload),
`lna_probe.py` (per-port shelf + bias-T on/off to detect a bias-T-powered LNA), and
`tone_finder.py` (Bluetooth-safe continuous-tone aim-by-ear).

**NEXT EXPERIMENT (user will try later, 2026-06-29 break):** the user has a **directional,
amplified antenna on the shed** — that's the promising path (directivity + aperture + gain
= real wideband SNR, the exact thing the passive antenna lacks; analogous to the attic
ACTIVE antenna that decodes clean). Setup rules when it's hooked up: (1) **power its amp**,
antenna **straight into the SDR — do NOT stack our LNA in front** (double-amp = overload).
(2) It's directional → **aim it at the DC towers** (generally S/SW); put `tone_finder.py`
on it (RF36 or rescan) so the user peaks it by ear like they did the passive one.
(3) Run **LOW SDR gain** (high IFGR ~45, RFGAIN_SEL ~3-4) so the antenna's amp doesn't
overdrive the front end into the garbage-growth trap. Expect this one to actually decode.

Related: [[antb-wsl-task]], [[adaptive-tv-project]], [[discone-vhf-hi-atsc-works]],
[[fm-sweep-as-chain-control]], [[noise-fingerprint-check]].
