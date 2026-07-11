---
name: discone-vhf-hi-atsc-works
description: "Counter-finding to [[fm-sweep-as-chain-control]]: discone on port C CAN decode ATSC at VHF-hi (RF7-13, 174-216 MHz) even though it CANNOT at UHF. Confirmed 2026-06-22 on RF7 DC mux — 0 transport errors on 1.76M packets, FPLL clean lock mean|x|=0.04, full 6-program TS recovered. Picture marginal (glitchy/crashes VLC) due to multipath, but the chain is solid."
metadata:
  node_type: memory
  type: project
  originSessionId: tv-experiment-discone-2026-06-22
---

Yesterday ([[fm-sweep-as-chain-control]]) we concluded "discone + port C can't do ATSC" based on UHF tests (RF34/36 returning 0 carriers + RF36 live-chain test getting `mean|x|=0.00000` dead FPLL). That conclusion was **incomplete** — it was right for UHF (470-608 MHz) but not the whole story.

**Today's experiment** (2026-06-22): user requested live TV via discone-on-C as an experiment. RF7 (177 MHz VHF-high) showed pilot_snr=13.3 in scan (still below the +30 dB lock gate), but live tv_live.py at RF7 produced:
- FPLL **clean lock** within ~30 sec: mean|x|=0.040, NCO stable at -2.69 MHz ±10 Hz
- live.ts grew at **19.8 Mbps** (full ATSC payload rate, real-time)
- After ~150 seconds: 581 MB of TS, 1.76M packets
- `tsanalyze`: **0 transport errors**, 6 services identified by PSIP name (RF7 DC mux: WHUT 32.1, PBSKids 32.2, WJLA 7.1, Charge! 7.2, Comet 7.3, +1)
- VLC at `--programs=1` rendered video successfully (PBS / news / etc) — confirms picture is recoverable, not just metadata

**Why VHF-hi works on port C when UHF doesn't:** port C is HF-optimized (≤30 MHz sweet spot) but its rolloff above 30 MHz is gradual rather than a cliff. By UHF (605 MHz) the combined rolloff + polarization mismatch (discone vertical vs ATSC horizontal) is ~35-45 dB total — kills the signal. At VHF-hi (177 MHz) the rolloff component is much less severe (~10-15 dB) so the discone's modest UHF gain + the still-tolerable port-C response = enough margin for the FPLL to engage.

**What did NOT work cleanly:** picture playback. ~4-6% packet loss per frame caused MPEG-2 macroblock errors → glitchy video → VLC eventually starves and stops. This is the SAME marginal-signal scenario Ubuntu solved on `marginal-antenna-tuning` branch ([[marginal-antenna-branch-2026-06-20]]) with mpv error concealment + smooth profile — that fix has NOT been ported to Windows.

**How to apply:**
- The scanner cold-test is more conservative than live-chain operation. If pilot_snr ≥ ~12 dB on a target channel, force tv_live at it directly even if scan reports lock=False — the equalizer needs continuous data to converge.
- Don't write off port C for TV entirely — the rolloff is frequency-dependent, not binary. VHF-hi (174-216 MHz) is the discone+C sweet spot for ATSC.
- For watchable playback on marginal signals, the Windows branch needs the Ubuntu `marginal-antenna-tuning` recipe ported: `STVT_MPV_EC=1 STVT_MPV_SMOOTH=1 STVT_CACHE_SECS=8 STVT_CPU_ISOLATE=1 STVT_MPV_HWDEC=no`. Until then, expect crashy playback even when chain is clean.
- Antenna A with a real TV antenna remains the optimal config (per [[fm-sweep-as-chain-control]]).

**Discone model (confirmed 2026-06-28):** it's a **Comet DS-150S** — wideband discone,
RX 25–1500 MHz (per the box; some retailer listings say 1300), TX 6m/2m/440/900/1200,
vertical pol, omni, SO-239, ~4'8", 65 ft RG-58 included. KEY: the antenna covers the
whole RSPdx range, so the UHF ATSC failure is
purely the **port-C rolloff above ~30 MHz** (HF-optimized port), NOT the antenna. Moving
the DS-150S to port A or B should restore UHF reach. (Discones have no gain spec — they
trade gain for bandwidth.)

Related: [[fm-sweep-as-chain-control]] (corrected partial — applies to UHF, not VHF-hi),
[[marginal-antenna-branch-2026-06-20]] (the cure for glitchy playback on marginal signals),
[[stvt-play-hd-ffmpeg-probe-lies]] (why VLC's "Invalid frame dimensions" wasn't proof of chain failure),
[[noise-fingerprint-check]].
