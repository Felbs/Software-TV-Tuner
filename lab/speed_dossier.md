# STVT SPEED DOSSIER — fast lock, fast scan, whole-band TV

**Date:** 2026-07-29 · **Scope:** read-only research. No SDR, daemon, or DSP source was touched.
**Rail used:** `tools/scan_lab/fixtures/` (35 × 200 ms @ 8 MS/s real OTA captures), the archived
chain logs in `tools/data/tv_live/scan_rf*.log`, the gr-atscplus C++ sources, and git history.

---

## 0. The one-paragraph answer

**The DSP is not slow. It was never slow.** Measured from our own chain logs, a warm-started
decode reaches final MER inside **~250 ms** of samples arriving, and the pilot-detection maths
needs **2.05 ms** of samples per channel — not the 100 ms we dwell today. What actually costs
seconds is *everything around the DSP*: 4.3 s of Python/GNU-Radio import, 3.6 s of SDRplay
driver open, 3.2 s of process kill, a mandatory 4 s inter-channel sleep, and policy timers
(25 s growth window, 8 s dwell, ffprobe budgets). **The DSP is between 1 % and 4 % of a channel
change** (§3.1b).

**The single highest-value change in this entire dossier is a ~30-line fix that lets the
already-built persistent-retune path keep the already-built warm-start tap cache** — which today
it explicitly throws away (`tools/tv_live.py:924-937`). That alone turns an 11–15 s channel
change into a sub-second one. ATSC's own A/74 §5.6 recommends per-channel stored configurations,
and Sarnoff patented exactly our tap cache in 1997 (expired 2017) — we are on well-trodden ground.

Whole-band TV on an RSPdx is **physically impossible** (10 MHz of aperture vs 138 MHz of UHF).
The honest consolation prize is 2 *pilots* per tune for scanning; the honest hardware answer is
"buy a second tuner, not a wider one" (§1.4) — and specifically **not** an RSPduo or an RX888,
whose headline bandwidths both evaporate at TV frequencies.

**Three things worth knowing that came out of the literature (§5):**
1. Our carrier **capture range is ±6.4 kHz — 5× narrower than stock gr-dtv's ±31.8 kHz.** That is
   a real failure mode, and the FFT pilot estimate (§3.3, measured accurate to **±1–5 Hz in
   2.05 ms**) closes it.
2. There is a published, 23-year-old fix for exactly our cold-start bottleneck — **equalizer
   "data recycling"** (Oh et al., GLOBECOM 2003) — that we do not have and that costs almost
   nothing to add.
3. Our target to beat on scanning is **HDHomeRun's ~80 s per market**. The plan here projects
   **~26 s**.

---

## 1. WHOLE-BAND TV — the honest physics

### 1.1 The aperture arithmetic

| Quantity | Value | Source |
|---|---|---|
| ATSC channel width | 6.000 MHz | [A/53 Part 2](http://www.atsc.org/wp-content/uploads/2021/04/A53-Part-2-2011.pdf) |
| US UHF TV band (RF 14–36, post-repack) | 470–608 MHz = **138 MHz** | `tools/tv_tuner.py:517-531` |
| US VHF-hi (RF 7–13) | 174–216 MHz = **42 MHz** | same |
| RSPdx tuners | **1** | `tools/stvt_capacity.py:48` |
| RSPdx max aperture | **10 MHz** (10.66 MS/s max) — **but at 8-bit**. Bit depth slides with rate: 14-bit only ≤6.048 MSPS, 12-bit ≤8.064, 10-bit ≤9.216 | [SDRplay](https://www.sdrplay.com/rspdx/), [RSPduo tech info](https://www.sdrplay.com/wp-content/uploads/2018/06/RSPDuo-Technical-Information-R1P1.pdf) (same ADC family) |
| **what we actually run** | **8 MS/s → 12-bit** | `tools/tv_live.py:171` |

**138 MHz ÷ 10 MHz = 13.8 apertures.** To see UHF simultaneously we need **14× more radio**.
VHF-hi needs 4.2×. There is no signal-processing trick that manufactures bandwidth — the
`whole_band.py` fast-convolution channelizer works for MW because 2 MS/s covers the **entire**
530–1700 kHz band with room to spare. It does not port to TV because TV is ~70× wider than any
single-tuner SDR we own.

Note also the **hidden cost of pushing the RSPdx wider**: going from our 8 MS/s to the full
10.66 MS/s trades 12-bit for 8-bit — you gain 33 % bandwidth and lose **24 dB of ADC dynamic
range**, on a signal class whose whole problem is a local 100 kW transmitter sharing an ADC with
a 70-mile fringe station.

**Verdict: NO. You cannot channelize TV. Say so plainly and move on.**

### 1.2 What *does* fit: 1.67 channels, or 2 pilots

A 10 MHz window holds one 6 MHz channel + 4 MHz of guard. It does **not** hold two channels
(that needs ≥12 MHz, and realistically 14 with guard).
But two *pilots* of adjacent channels are exactly 6 MHz apart, so parking the LO midway between
channel N and channel N+1 places both pilots at **±3.000 MHz** from window center — comfortably
inside even our current 8 MHz.

**Measured (`scratchpad/wideband_proof.py`, real fixtures, upsampled + frequency-shifted):**
the geometry itself is *free* — pilot sharpness is **bit-identical** whether the pilot sits at
−2.690 MHz (today), +0.310 MHz, or −5.690 MHz:

```
RF 9: no-shift  30.1   +3MHz slot  30.1   -3MHz slot  30.1
RF36: no-shift  29.5   +3MHz slot  29.5   -3MHz slot  29.5
RF31: no-shift  29.5   +3MHz slot  29.5   -3MHz slot  29.5
(8/8 channels identical to 0.1 dB)
```

**The open risk is adjacency, not geometry.** My synthetic two-channel test summed two
*8 MHz-wide* fixtures 6 MHz apart, which double-counts 2 MHz of guard band and is therefore
invalid — it produced two spurious degradations (RF9 → 4.1 dB sharp, RF36 → 15.2 dB) that the
isolation test above proves are artifacts of my construction, not of the idea. **This must be
settled with one real wideband capture** (see §6). If it holds, phase-1 scan tune count halves.

### 1.3 Partial parallelism worth quantifying

| Idea | Gain | Honest caveat |
|---|---|---|
| 2 pilots / tune (scan only) | 35 tunes → **18 tunes**, ~2× on phase 1 | needs the real-capture check in §6 |
| 2 channels *decoded* / tune | **impossible** | needs ≥12–14 MHz of clean aperture; RSPdx tops at 10 MHz, and at 10 MHz it is an 8-bit ADC |
| VHF-hi 7–13 in chunks | 42 MHz → **5 tunes** at 10 MHz (or 4 pilot-pair tunes) | already roughly what we do; no new win |
| CPU cost of channelizing | **not the limit** | measured overlap-save, 16 MS/s, 2 channels: ~0.8–1.3 cores in numpy (`NFFT=8192`: fwd 0.10 ms + 2×0.05 ms per 0.51 ms block). One ATSC *decode* costs ~5 cores. |

### 1.4 Hardware that would actually unlock simultaneity

**The requirement:** to decode N *adjacent* ATSC channels at once you need **6N + 2 MHz** of clean
aperture *and* ~5 CPU cores per channel (`x86_cpu_savings_ceiling`: "~5 cores of sequential DSP
is what software 8-VSB demod costs, full stop"). On a 32-core/64-thread Threadripper the CPU
supports roughly **6 simultaneous software decodes**. Above that, compute becomes the wall too.

| Radio | Instantaneous BW **at TV frequencies** | ADC | ATSC ch | Price (2026) | Verdict |
|---|---|---|---|---|---|
| **RSPdx** (ours) | **10 MHz**, but bit depth *slides with rate*: 14-bit only ≤6.048 MSPS, 12-bit ≤8.064, 10-bit ≤9.216, **8-bit at 10.66** | 14→8 | **1** | owned ([RSPdx-R2 $239.95](https://www.hamradio.com/detail.cfm?pid=H0-018736)) | today. Note our 8 MS/s runs at **12-bit**, not 14. |
| **RSPduo** | **2 MHz per tuner in dual mode**; 10 MHz single-tuner ([tech info](https://www.sdrplay.com/wp-content/uploads/2018/06/RSPDuo-Technical-Information-R1P1.pdf)) | 14→8 | **0 in dual mode** | [$299.95](https://www.hamradio.com/detail.cfm?pid=H0-016162) | ⚠ **TRAP.** 2 MHz cannot carry a 6 MHz channel — its dual-tuner mode is useless for ATSC. |
| **RX888 MkII** | The famous **64 MHz is HF-only** (direct sampling 1 kHz–64 MHz). Above 64 MHz it downconverts through an R828D and gives **10 MHz** ([vendor](https://www.rx-888.com/rx/)) | 16-bit LTC2208 | **1** | [$152.79](https://opensourcesdrlab.com/products/rx888-mkii-16bit-sdr-receiver-radio-ltc2208-adc-upgrade-rx888-1) | ⚠ **Its headline spec does not apply to TV.** No better than an RSPdx at UHF — and its R828D supply is on a clock ([Rafael stopped production](https://www.rtl-sdr.com/rtl-sdr-blog-v4-end-of-line/)). |
| **LibreSDR "B210 Mini"** (AD9361) | **56 MHz**, 50 MHz–6 GHz, UHD-compatible | 12-bit | **9** | **[$268](https://opensourcesdrlab.com/products/libresdr-b210-mini-ad9361)** | ★ **best $/MHz for real wideband TV** |
| **bladeRF 2.0 micro xA4** | **56 MHz honest** (AD9361 analog LPF). The 122.88 MSPS figure is an **8-bit overclock past a 56 MHz filter** — [don't plan around it](https://destevez.net/2023/02/running-the-ad9361-at-122-88-msps/) | 12-bit | **9** | [$540](https://www.nuand.com/product/bladerf-xa4/) | first-party support |
| **USRP B210** | **56 MHz** (61.44 MS/s) | 12-bit | **9** | [$2,387](https://www.ettus.com/all-products/ub210-kit/) | safest UHD path, 9× the LibreSDR price |
| **Wavelet Lab xSDR** | **90 MHz** spec'd, M.2 2230 | 12-bit | **15** | [$549](https://www.cnx-software.com/2026/02/16/xsdr-a-tiny-m-2-2230-sdr-with-artix-7-fpga-and-lms7002m-rfic/), ships Jul 2026 | 65 % of UHF in one shot |
| **RFSoC 4x2** | whole band, direct RF sampling | 14-bit | **23+** | $2,499 — **academic only** | the only sub-$10k whole-band option, and you can't buy it |
| **USRP X310 + UBX-160** | **160 MHz** over 10 GbE | 14-bit | **26** | [$11,462](https://www.ettus.com/all-products/x310-kit/) + $2,645 | the honest price of "whole UHF band" |
| **second RSPdx** | 2 × 10 MHz, **independent tunings** | 14→8 | **2, anywhere in the band** | ~$240 | ★ the practical answer |
| **HDHomeRun FLEX QUATRO** | n/a — delivers demodulated TS, not IQ | — | **4 independent** | **[$149.99](https://shop.silicondust.com/shop/)** | ★★ if the goal is *scanning and watching*, this beats every IQ option under $500 |

**"Full Band Capture" exists — but not for terrestrial.** MaxLinear's MxL278 digitizes 1.2 GHz of
*cable* spectrum; Broadcom's BCM45308 digitizes the whole 250–2350 MHz *satellite* IF. Broadcom
even holds a patent covering 54–889.75 MHz terrestrial full-spectrum capture
([US 8,928,820](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/8928820)) — **but
it is patent art with no product, and no FBC chip exposes raw IQ.** There is nothing to buy.

**Three honest cautions on the wideband options:**

1. **Bit depth vs. dynamic range.** Going from our 14-bit/10 MHz to 12-bit/56 MHz *loses* 12 dB
   of ADC dynamic range while *increasing* the number of strong in-window signals ~6×. ATSC
   reception in a real market is a desired-to-undesired problem — a local 100 kW transmitter
   sitting 40 dB above a 70-mile fringe station, both inside the same ADC. Our fixtures already
   show a ~30 dB spread across DC channels. **A wider window can make marginal channels
   *worse*.** This must be tested, not assumed.
2. **USB throughput.** 61.44 MS/s × 4 bytes (sc16) ≈ **246 MB/s**, at the practical ceiling of
   USB 3.0 — and this rig has a documented history of USB starvation and leakage
   (`sdr_usb_throughput_starvation`, and `tv_tuner.run_scan`'s own link pre-flight that *aborts
   the scan* below 90 % delivery). A 30× throughput increase is a serious integration risk here.
3. **Adjacency.** A wide window only helps if the channels you want are *adjacent*. In a repacked
   market they usually are not — in DC our live muxes are RF 9, 15, 21, 27, 31, 34, 35, 36.
   A 56 MHz window centred to catch 31/34/35/36 (572–608 MHz) would work; nothing catches
   9 and 36 together at any bandwidth.

> **Recommendation, in order:**
> 1. **If the goal is "watch/record two things at once" → a second RSPdx (~$240)**, or an
>    **HDHomeRun FLEX QUATRO ($149.99)** for four tuners with zero DSP cost. Two independent
>    tunings solve the actual user problem with no aperture, dynamic-range, throughput, or
>    channelizer risk.
> 2. **If the goal is a genuine multi-channel TV channelizer as research** → **LibreSDR B210 Mini
>    ($268)**: 56 MHz = 9 adjacent channels, and the CPU caps you at ~6 decodes anyway, so it is
>    not the limiting factor.
> 3. **Whole-band UHF honestly costs ~$14 k** (X310 + UBX-160). It is not a hobby purchase, and
>    115 CPU cores of demod is not a hobby computer.
>
> **Do not buy an RSPduo or an RX888 for this.** Both have headline specs that evaporate at TV
> frequencies, and both are the natural first guess.

**Recommendation: if the goal is "watch/record two things at once", buy a second tuner, not a
wider one.** A wider SDR only helps if the channels you want happen to be adjacent, which in a
repacked market they usually are not.

---

## 2. FAST SCAN — measured, not estimated

### 2.1 Where the time actually goes today

**Phase 1** (`tools/sdr_sweep.py:217-270`, driven by `tv_tuner.run_scan`):
`settle_sec=0.04` + `dwell_sec=0.10` × 35 frequencies = **4.9 s of radio time**, plus a 2 s USB
link pre-flight, plus two 2–3 s inter-phase sleeps → ~15 s wall.

**Phase 2** (`tools/tv_tuner.py:801-1011`, one fresh `tv_live` process per candidate). Measured
from a real successful run, `tools/data/tv_live/scan_rf31.log`, 2026-07-26:

| Interval | Wall | What it is |
|---|---|---|
| 13:09:52.630 → 13:09:56.888 | **4.26 s** | Python + GNU Radio + SoapySDR/UHD import and device enumeration |
| 13:09:56.888 → 13:09:56.951 | 0.06 s | flowgraph construction |
| → `[eq-long t= 0.21s] fs=8 fs_err_rms=0.5197` | **~0.25 s** | **the entire DSP acquisition** (8 field syncs, already at final error = MER 19.7 dB, warm-started) |
| → 13:10:07.219 | ~10 s | scanner policy: growth window, `dwell_sec`, ffprobe budget |
| + `time.sleep(4)` | 4.00 s | mandated SDRplay release (`tools/tv_tuner.py:1011`) |
| **total per candidate** | **18.6 s** | and `retries=2` makes the worst case 3× that (`tv_tuner.py:1369-1371`) |

With 8–9 DC candidates that is **2.5–5 minutes**, of which **~2 seconds total is DSP**.

> **Phase 1 is not the problem.** Even reducing it to zero saves 5 s out of ~300. Every
> engineering hour should go to phase 2.

### 2.2 Minimum energy/pilot detection time — MEASURED

I re-ran the shipped `sdr_sweep._analyze` recipe on truncated prefixes of all 35 fixtures
(`scratchpad/dwell_sweep.py`). Ground truth = the 8 channels the full 200 ms capture detects
(RF 9, 15, 21, 27, 31, 34, 35, 36); 27 empties.

| dwell | samples | n_fft | TP | FN | FP | worst-case margin |
|---|---|---|---|---|---|---|
| 0.13–0.51 ms | 1 024–4 096 | small | 0 | 8 | 0 | — |
| 1.02 ms | 8 192 | 8 192 | 6 | 2 | 0 | 0.00 dB |
| **2.05 ms** | **16 384** | **16 384** | **8** | **0** | **0** | **+0.76 dB** |
| 4.10 ms | 32 768 | 16 384 | 8 | 0 | 1 | +0.29 dB |
| 16.4 ms | 131 072 | 16 384 | 8 | 0 | 0 | +0.61 dB |
| 200 ms (today) | 1 600 000 | 16 384 | 8 | 0 | 0 | +0.62 dB |

**A single 16 384-point FFT — 2.05 ms of samples — reproduces the full 200 ms result exactly,
with a *better* worst-case margin (0.76 dB vs 0.62 dB).** We are dwelling **49× longer than the
physics requires.**

**Why 2 ms is a floor and not an implementation artifact:** the discriminator is
`pilot_sharpness` — a CW pilot's energy in one bin versus its ±100 kHz neighbourhood. FFT
resolution is Δf = 1/T regardless of sample rate, so a 2 ms window gives ~490 Hz bins and a 1 ms
window gives ~980 Hz. At 980 Hz the pilot's energy no longer stands proud of the neighbourhood
and recall drops to 6/8. Re-optimising thresholds at 1.02 ms recovers 8/8 with **0.13 dB**
margin — mathematically true, operationally fragile. **2.05 ms is the honest answer.**
Longer dwells buy nothing because all three features are *ratio* metrics: more averaging lowers
noise variance in numerator and denominator alike.

This matches and extends the archived optimiser result (git `3426b08`, "F1 = 1.000,
4 ms/channel") — that commit measured the **CPU** cost as ~4 ms/channel; this measurement shows
the **data** cost is ~2 ms/channel.

### 2.3 Retune settle — what we can honestly claim

- `tools/sdr_sweep.py:248-254` already uses **40 ms** with an *active drain loop* (it reads and
  discards samples rather than sleeping), and the fixtures produced by exactly that code achieve
  F1 = 1.0. **40 ms is proven in-tree.**
- The **0.35 s** figure in the brief comes from `lab/tv_pilot_survey.py:53` — a passive
  `time.sleep(0.35)` in a lab script, plus a further `time.sleep(0.15)` after stream activation.
  It is a conservative lab constant, not a measured floor.
- The SDRplay API tunes the synthesiser in tens of microseconds; what costs milliseconds is the
  USB round-trip for the control message plus flushing the driver's ring of pre-retune samples.
  A drain loop (already implemented) is the right mechanism.
- **Cannot be measured further without hardware.** §6 says exactly how to bound it in 90 seconds
  when the radio frees up.

### 2.4 The proposed two-stage scan

**Stage A — pilot sweep.** One SDR session, one 16 384-point FFT per tune.

| variant | tunes | radio time | vs today |
|---|---|---|---|
| today | 35 | 35 × 140 ms = **4.9 s** | 1.0× |
| 2 ms dwell, 40 ms settle | 35 | 35 × 42 ms = **1.47 s** | 3.3× |
| + 2 pilots/tune (§1.2, needs validation) | 18 | 18 × 42 ms = **0.76 s** | 6.5× |
| + settle proven at 15 ms (if §6 shows it) | 18 | 18 × 17 ms = **0.31 s** | 16× |

**Stage B — confirm only where pilots exist.** Replace the fresh-process-per-candidate design
with **one persistent chain retuned down the candidate list** (`STVT_PERSIST_RETUNE=1`, already
built, `tools/tv_live.py:794-844`). Per-candidate cost becomes:

| term | cost | note |
|---|---|---|
| radio re-point | **0.1 s** | measured claim in `tv_live.py:787-790` |
| DSP re-acquire, warm | **~0.25 s** | if §3.5 fix lands; today persist-retune is forced cold ⇒ ~3 s |
| PSI/PMT acquisition | **~0.5 s** true floor, **~2.1 s** as coded | PAT repeats ~10×/s in ATSC, so 5 PATs arrive in ~0.5 s — but `measure_convergence()` gets them by **reading back the last 5 MB of `live.ts`** (`tv_tuner.py:476-510`), and 5 MB at the 19.39 Mbit/s mux rate is 2.07 s of wall. Counting PATs from the stream directly removes 1.6 s. |
| ffprobe program enumeration | **~3.3 s** today | it waits for 8 MB of TS (`tv_tuner.py:713`); at 19.39 Mbit/s that is 3.3 s of wall. Parsing PAT/PMT ourselves (we already ship `atsc_psip.py`) removes this entirely. |
| **realistic per candidate** | **~1–3 s** (warm) / **~4–6 s** (cold) | vs **18.6 s** today |

**Whole-scan projection: ~1.5 s (stage A) + 8 × ~3 s (stage B) ≈ 26 s, versus 2.5–5 minutes
today. 6–11×.**

### 2.5 How that compares to everyone else

| Scanner | Per-channel cost | Full US market (~49 RF, ~10 muxes) |
|---|---|---|
| **w_scan / w_scan2** | **11 s per dead ATSC channel** — [`scan.c`](https://github.com/tbsdtv/w_scan/blob/master/scan.c) has **no `SYS_ATSC` case**, so ATSC falls through to `default`: carrier_timeout 3000 ms + lock_timeout 8000 ms. Up to 33 s at `-t 3`. | **~8 minutes** |
| **dvbv5-scan** | 4.0 s flat cap (40 × 100 ms, [`dvbv5-scan.c`](https://github.com/gjasny/v4l-utils/blob/master/utils/dvb/dvbv5-scan.c)); only tests `FE_HAS_LOCK`, never bails on the staged bits | **~3 minutes** |
| **STVT today** | 140 ms sniff + 18.6 s per candidate | **~3–5 minutes** |
| **HDHomeRun** | **~250 ms if no signal** — [`hdhomerun_device.c`](https://github.com/Silicondust/libhdhomerun/blob/master/hdhomerun_device.c): `if (!status->signal_present) return 1;` — then ≤2.5 s lock, ≤4 s ATSC program detect on survivors | **~80 s** |
| **STVT proposed** | **42 ms sniff + ~3 s per candidate** | **~26 s** |

**The whole commercial industry does exactly the two-stage thing we are proposing.** The
canonical published dwell is **30 ms** of amplitude prescan per frequency
([US 7,113,230](https://patents.google.com/patent/US7113230), Panasonic), and the TV-whitespace
sensing literature says **4–30 ms** suffices even at −20 dB SNR (Kim & Andrews,
[arXiv:0910.1787](https://arxiv.org/pdf/0910.1787): *"t_s = 1 ms and N = 2048 … N_d is 30"*;
IEEE 802.22-10/0073r03 slide 10: energy detection 4–5 ms). **Our measured 2.05 ms is at the
aggressive end of that published range — and it is a pilot-feature detector, not a broadband
energy detector, which is why it can afford to be shorter.**

The one thing HDHomeRun does that we do not is `if (!signal_present) return 1;` — **bail on the
cheap test before paying for the expensive one.** Our phase 1 already computes that verdict; the
gap is that phase 2 then spends 18.6 s per survivor.

---

## 3. FAST LOCK — stage-by-stage budget

Constants below are read from the gr-atscplus sources. **Note the chain does not run at 8 MS/s:**
that is only the RSPdx capture rate. The FPLL/sync run at `ATSC_SYMBOL_RATE × STVT_SPS`
(`tools/tv_live.py:357-358`) = 16.14 MS/s at the default SPS 1.5, or 11.84 MS/s at the
scanner's SPS 1.1.

### 3.1 The theoretical minimum, stage by stage

| Stage | Cold | Warm | What bounds it |
|---|---|---|---|
| RRC / resampler group delay | 0.05 ms | 0.05 ms | FIR only, 8 symbols |
| DC blocker (D = 32) | 0.008 ms | " | 4(D−1)+1 samples |
| **FPLL phase settle (4τ)** | **0.50 ms** | " | α = 1e-3 ⇒ β = α²/4, ζ ≡ 1 (critically damped by construction), τ = 2/α = 2000 samples |
| **FPLL frequency pull-in** | ~1.0 ms @ 5 kHz | " | β = 2.5e-7; **pull-in range only ≈ ±6.4 kHz**, set by the AFC pre-filter τ = 25 µs (`STVT_FPLL_AFC_TAU`) |
| **AGC settle (4τ)** | **7.5 ms** strong → **~80 ms** weak | " | α = 1e-6, ref = 4.0; τ = 1/(A·α) samples ⇒ **scales as 1/signal level**. Slowest front-end element. |
| Symbol timing lock | 0.46 ms | " | EMA α = 0.40 over 832 phase bins ⇒ 95 % in 5.9 segments; threshold 4.0 |
| **First field sync** | **0–24.2 ms** (mean 12.1) | " | one field sync suffices cold — the 313-spacing validator is *bypassed* while unlocked |
| EQ prime + MOD-12 guard | 0.93 ms | " | 1 segment + ≤11 segments, once |
| Deinterleaver fill | 4.0 ms | " | ATSC convolutional interleaver spans 52 segments |
| **Equalizer convergence** | **≈ 3.0 s (~124 field syncs)** | **< 0.25 s (< 10 FS)** | μ = 5e-5, 704 supervised updates per field sync |
| **TOTAL to clean TS** | **≈ 3.05 s** | **≈ 0.15–0.30 s** | |

**The equalizer is 98 % of the cold budget. Everything else combined is under 40 ms.**

### 3.1b …and what a channel change actually costs today

| Term | Classic path | Persist-retune path | Cite |
|---|---|---|---|
| kill old chain + settle sleep | 3.2 s + 2.0 s | 0 | `tv_live.py:786`; `tv_tuna_panel.py:1487` |
| SDRplay driver reopen | 3.6 s | 0 | `tv_live.py:787` |
| Python + GNU Radio import | **4.26 s** | 0 | measured, `scan_rf31.log` |
| radio re-point | — | **0.1 s** | `tv_live.py:787-790` |
| **DSP acquisition** | **~0.25 s warm / ~3 s cold** | **~3 s — forced cold** | §3.4 D1 |
| milestone poll quantisation | up to 2.4 s | — | `tv_tuna_panel.py:1518, 1524` (`sleep(1.2)` ×2) |
| post-lock margin read | 3–12 s | 3 s | `tv_tuna_panel.py:1537-1551`; `:1460` |
| **total** | **≈ 16–28 s** | **≈ 6–11 s** | |

**The DSP is between 1 % and 4 % of a channel change.** That is the whole argument of this
dossier in one row.

### 3.2 Three structural facts that kill most of the proposed levers

1. **`atsc_sync_soft` is not a gate.** `d_emit_when_unlocked = true` by default — segments flow
   from the very first one whether timing is locked or not. There is no "wait for symbol lock"
   to eliminate.
2. **`atsc_fs_checker_inst` already trusts the first PN511 correlation.** Cold, `d_fs_locked`
   is false, so the 313-spacing validator is skipped entirely and the **first** valid field sync
   is accepted. Proposed lever (e) — "reduce the field-sync wait by trusting a strong PN511
   correlation immediately" — **is already implemented.** The flywheel (`ATSCPLUS_FS_COAST`)
   defaults to 0 = off and requires 80 clean syncs (1.94 s) of warm-up before it may engage; it
   is a *robustness* feature, not an acquisition cost.
3. **There is no FPLL lock detector and no output gating anywhere in the chain** except
   `fs_checker`'s `d_field_num != 0`, which costs at most one field (24.2 ms). Nothing is
   waiting on anything.

### 3.3 Lever (a) — FFT one-shot carrier acquisition: **VALIDATED, and better than expected**

I measured the precision of a quadratic-interpolated FFT pilot-frequency estimator on all 8 real
channels, using the spread across independent consecutive blocks of the same capture as the
estimator's own precision (`scratchpad/`, fixtures):

```
   nfft      ms   RF9   RF15   RF21   RF27   RF31   RF34   RF35   RF36
   4096    0.51    11     13     13     57     11     13     10      8   Hz std
   8192    1.02    11      8      5     20      5      4      6      5   Hz std
  16384    2.05     3      4      1      5      1      1      1      1   Hz std
  32768    4.10     1      3      1      3      0      0      0      0   Hz std
```

**A 2.05 ms FFT nails the pilot to ±1–5 Hz.** The FPLL's pull-in range is **±6.4 kHz** — the
estimator is ~1000× tighter than the loop needs. Consequences:

- **Frequency pull-in (~1 ms) can be eliminated entirely** by pre-loading the NCO with the
  measured pilot. Small absolute saving, but…
- **…it removes the ±6.4 kHz capture-range constraint altogether.** Today a transmitter or LO
  more than ~6 kHz off simply never acquires; with a measured seed, capture range becomes the
  FFT search window (currently ±30 kHz in my estimator, trivially widenable). This is a
  **robustness** win disguised as a speed win, and it is the right fix for any future
  "won't lock but the pilot is obviously there" report.
- **Free by-product:** the scan already computes this FFT. The pilot frequency can be handed to
  the chain at no additional radio or CPU cost.

**Bonus finding — two different, both-wrong pilot constants.** The ATSC pilot sits at
lower-band-edge + 309 441 Hz, i.e. channel-centre **− 2 690 559 Hz**. The codebase carries two
different approximations of it, in disagreement with each other and with the spec:

| Where | Constant | Error vs spec |
|---|---|---|
| `tools/sdr_sweep.py:143` (detector) | −2 690 000 Hz | **+559 Hz** |
| `atsc_fpll_tight_impl.cc:43` `(-3e6 + 0.309e6)` (NCO init) | −2 691 000 Hz | **−441 Hz** |
| **measured through this RSPdx** (8 channels) | **≈ −2 690 205 Hz** | +354 Hz = **≈ +0.7 ppm LO error**, consistent across every UHF channel |

Neither is fatal — the detector searches a ±2 kHz window and the FPLL has ±6.4 kHz of pull-in —
but the FPLL's NCO currently starts **~795 Hz below the actual pilot** and has to walk there.
The measured +0.7 ppm is itself a usable per-radio calibration constant, and it is free: the
scan already computes it.

### 3.4 Lever (b)+(c) — per-channel tap presets / channel-state cache: **ALREADY BUILT AND WORKING**

`atsc_equalizer_long_impl.cc` ships a warm-start tap cache:

| Aspect | Detail |
|---|---|
| Env | `STVT_EQ_TAP_CACHE_FILE`, read via `getenv` **at block construction** (`.cc:85`) |
| Path composed by | `tools/tv_live.py:926, 941-943` → `<STVT_EQ_TAP_CACHE>/taps_<ANTENNA>_rf<RF>.bin` |
| Keyed by | **channel AND antenna** — exactly the "perfect-tune table" concept |
| Format | `'TAPC'` magic + `uint32 n` + n × float32 = **1032 bytes** at NTAPS 256 |
| Load gate | all finite and 0.01 < Σt² < 2500 |
| Saved | `d_taps_lkg` (the quality-gated snapshot), atomically via tmp+rename |

**It works.** In the 2026-07-26 scan, 7 of 9 real channels warm-started and the equalizer was at
its final error from the very first telemetry print — zero convergence transient:

```
scan_rf31.log:  [eq-long] WARM START ... (|taps|=1.348)
                [eq-long t= 0.21s] fs=8  fs_err_rms=0.5197
                [eq-long t=51.0s] fs=408 fs_err_rms=0.5217   <- flat all the way
scan_rf26.log:  (no warm start, empty channel)
                [eq-long t= 0.27s] fs=8  fs_err_rms=1.9368   <- noise, never converges
```

**But it has three defects that are each a cheap fix:**

| # | Defect | Cite | Cost of fix |
|---|---|---|---|
| **D1** | **`STVT_PERSIST_RETUNE=1` disables the cache entirely.** The comment says there is "no runtime rebind", so a persistent chain "runs cache-less (cold EQ converge ~3 s is already in the retune budget)". **This is the fast path deliberately giving up the fast equalizer.** | `tools/tv_live.py:924-937` | see §3.5 — small |
| **D2** | The cache persists only **every 1024th field sync = 24.8 s**. Scan dwells are 8–12 s, so a first-ever scan visit usually **never writes** the cache. The warm-start loop doesn't self-prime from scanning. | `.cc:719-723` | one constant + a save-on-stop |
| **D3** | `STVT_EQ_LKG` defaults to **0**, and `d_lkg_valid` is only set by (a) a warm load or (b) `LKG_ENABLED && batch_err_rms < threshold`. **With LKG off, a cold session never writes the cache at all.** `tv_tuner.CHAIN_DEFAULTS` sets it to `"1"` (`tv_tuner.py:305`) so the scanner and panel are fine — but any direct `tv_live.py` invocation (`stvt_run.sh`, lab harnesses, `tv_dual.py`) is silently write-inert. | `.cc:420-423`, `.cc:709-720` | audit + default flip |

### 3.5 ★ THE FIRST BUILD — runtime tap-cache rebind

D1 says there is no runtime rebind. **There almost is one already.** The equalizer ships an
E5 command port polled once per field sync (~21 Hz, one `stat()`):

```c
// atsc_equalizer_long_impl.cc:1170-1210
static const char* CMD_FILE = std::getenv("STVT_EQ_CMD_FILE");
...
} else if (!std::strcmp(cmd, "cache")) {
    if (const char* p = std::getenv("STVT_EQ_TAP_CACHE_FILE")) {   // <- NOT static: re-read every time
```

The `cache` command **re-reads `STVT_EQ_TAP_CACHE_FILE` on every invocation**. Since
`tv_live.py` and the equalizer live in the same process, `os.environ[...] = new_path` in Python
changes what that `getenv` returns. **So the load side already supports runtime rebind with
zero C++ changes.**

The save side does not:

```c
// atsc_equalizer_long_impl.cc:719
static const char* TAP_CACHE_FILE = std::getenv("STVT_EQ_TAP_CACHE_FILE");   // <- static: latched forever
```

**The complete fix:**

1. **C++ (2 lines):** drop `static` from `.cc:719` so the periodic save follows the rebind, and
   add a `save` verb to the command port that writes `d_taps_lkg` immediately (the write code
   already exists 10 lines below — it just needs to be callable on demand rather than on the
   1024-field-sync tick). This also fixes **D2**.
2. **Python (~15 lines in `TVLive.retune()`, `tools/tv_live.py:794-844`):**
   ```
   write "save" to STVT_EQ_CMD_FILE          # persist the OLD channel's taps
   wait one field sync (~25 ms)
   os.environ["STVT_EQ_TAP_CACHE_FILE"] = <new channel+antenna path>
   write "cache" to STVT_EQ_CMD_FILE          # load the NEW channel's taps
   src.set_frequency(...)                     # existing code
   ```
3. **Delete the guard at `tv_live.py:924-937`** that disables the cache under persist-retune.

**Payoff:** channel change goes from *(3.2 s kill + 3.6 s driver open + 4.3 s import + ~3 s cold
EQ)* ≈ 14 s, or the current persist path's *(0.1 s retune + ~3 s cold EQ + 8 s field-sync gate)*,
to **0.1 s retune + ~0.25 s warm re-acquire**. Sub-second channel change on a channel we have
visited before.

**Risk:** low but real. The cache is quality-gated on load (0.01 < Σt² < 2500) and the equalizer
retains its divergence bail (‖taps‖ > 50 → LKG restore → delta reset). The one genuine hazard is
the documented live-debut explosion — stale warm taps adapting while the hardware AGC is still
settling — which is already guarded by `d_fs_trained >= 3` (72.6 ms hold on the DD path,
`.cc:296-299`). **Keep that guard.** Note the AGC settle is 7.5–80 ms depending on level, so a
retune across a large level change (VHF ↔ UHF, antenna switch) should hold the equalizer for
~100 ms rather than 3 field syncs.

### 3.6 Lever (d) — speculative parallel decode across timing hypotheses: **DON'T**

There are no hypotheses left to speculate over.

- **Carrier:** §3.3 gives it to ±1–5 Hz from a 2 ms FFT. One hypothesis.
- **Symbol timing:** `atsc_sync_soft` locks in ~6 segments (0.46 ms) and does not gate output.
  Nothing to parallelise.
- **Field framing:** the first valid PN511 correlation is accepted; expected wait 12.1 ms.
- **Equalizer taps:** the cache gives the answer directly.

Speculative decode would spend 32 threads to save under 25 ms, on a machine whose live chain is
already gated by a *sequential* front end (`x86_cpu_savings_ceiling`). **Ranked last. Do not
build.**

### 3.7 Lever (e) — trust PN511 immediately: **already the behaviour** (see §3.2 fact 2)

The only tunable left here is `ATSCPLUS_PN511_LIMIT` (default 50 of 511 bits, clamped [10, 220];
upstream gr-dtv uses 20) and `ATSCPLUS_PN63_LIMIT` (default 15, upstream 5). We are already
**more** permissive than upstream. Loosening further trades false field syncs for nothing — the
expected wait is already 12.1 ms.

### 3.8 The levers that actually remain, ranked

| # | Lever | Expected speedup | Effort | Quality risk | How to measure |
|---|---|---|---|---|---|
| **1** | **Runtime tap-cache rebind** (§3.5) | **~14 s → ~0.4 s** per channel change | **S** (2 C++ lines + ~15 Python) | Low — existing divergence bail + `d_fs_trained` guard | `retune_stopwatch.py` (already written!) `t_video` before/after, ×12 channels |
| **2** | **Persistent-chain phase 2 in the scanner** | scan 2.5–5 min → ~30 s | M | None (same chain, same config) | full-scan wall clock + channel-count parity vs today's `scan.json` |
| **3** | **Fix D2/D3: save cache every ~128 FS (3 s) + on stop; audit `STVT_EQ_LKG`** | makes #1 self-priming; first-ever visits get warm on the *second* visit instead of never | **S** | None | count `[eq-long] WARM START` lines across a cold-cache full scan, then a second scan |
| **4** | **2 ms dwell in `sdr_sweep`** (§2.2) | phase 1 4.9 s → 1.5 s | **S** (one default) | None — proven identical on fixtures with *better* margin | re-run `scratchpad/dwell_sweep.py`; then one live scan, compare `scan.json` channel sets |
| **5** | **Stop gating on megabytes of TS.** `measure_convergence()` reads back 5 MB (= 2.1 s of mux) to count 5 PATs (`tv_tuner.py:476-510`); `ffprobe_programs()` waits for 8 MB and budgets 10 MB more (`tv_tuner.py:711-742`). Both answers are available from ~0.5 s of stream, and we already ship a stdlib PSI parser. | **−5 s per candidate** | M (`tools/atsc_psip.py` exists) | None | per-candidate wall time; program-list parity vs ffprobe on 20 channels |
| **3b** | **★ Equalizer data recycling** (§5.6, Oh et al. GLOBECOM 2003) — run the supervised LMS N× over the stored field-sync segment instead of once. Attacks the 0.27 % training duty cycle that *is* the cold-convergence cost. | **cold ~3 s → ~3/N s**; makes first-ever visits fast, which the cache cannot | S–M (inside `adaptN`) | Low — same gradient, applied more often; divergence bail already guards it | M4's replay rig with the cache disabled, N ∈ {1,2,4,8}; `fs_err_rms` vs field index |
| **6** | **FFT-seeded NCO** (§3.3) — **upgraded in priority by §5.5:** our carrier capture range is **±6.4 kHz vs gr-dtv's ±31.8 kHz**, so this is not a 1 ms saving, it is a real failure mode we are *more* exposed to than stock GNU Radio | ~1–10 ms, **and removes the capture-range wall** | M (needs an FPLL `set_freq` hook) | Low | replay a capture with an injected ±20 kHz offset; today it should fail to lock, seeded it should not |
| **13** | **Gear-shifted LMS, cold-start window only** (§5.6) — `STVT_EQ_GEAR_LMS` is built and off | 2–5× on cold convergence | S | **Medium** — `mer_dial_universal_algorithm` §(6): the slow-adaptation family passed replay and **collapsed live**. Gate to cold start; never steady-state. | live A/B with an overflow gate, not replay alone |
| **7** | Fix the −2 690 559 Hz pilot constant (§3.3) | 0 s | XS | None | fixture metrics before/after |
| **8** | 2 pilots per tune (§1.2) | phase 1 1.5 s → 0.76 s | M | Medium — thresholds need re-tuning at the new geometry | the §6 wideband capture |
| **9** | **Drop the panel's milestone poll granularity from 1.2 s to 0.1 s** (`tv_tuna_panel.py:1518, 1524`) — every acquisition milestone is currently quantised to 1.2 s, so a tune that finishes in 250 ms is still *reported* up to 2.4 s late | **−1 to −2.4 s per tune, free** | **XS** | None — it is a polling interval, not a timer | `retune_stopwatch.py` `t_done` |
| **10** | **Shorten the post-lock margin read.** The classic path measures MER for 12 s, already shortened to 3 s by the learned hour-curves (`tv_tuna_panel.py:1537-1551`). With a warm cache the equalizer is at final `fs_err_rms` from the **first** telemetry print (~250 ms) — 1 s is plenty | −2 s on warm channels | S | Low — keep the 12 s path for anything without a confident history | median MER from a 1 s vs 12 s window over 20 tunes; they should agree |
| **11** | Trim the 4.26 s import / 4 s release sleep / 2 s kill sleep | −8 s per *process spawn*; largely moot once #1+#2 land | M | None | log timestamps |
| — | Speculative parallel decode | ~0 | L | — | don't |

---

## 4. GPU ANGLE — measured, and the answer is "no"

**Benchmarked on this box (RTX 4090, torch 2.5.1+cu121):**

```
batched FFT:  512 × 16384-pt complex64 in 0.83 ms = 10.1 Gsample/s
```

The RSPdx delivers **8 Msample/s = 0.008 Gsample/s**. **The GPU is ~1 260× faster than the radio
can feed it.** There is no compute bottleneck anywhere in scanning or acquisition to accelerate.
A 2 ms per-channel FFT costs 0.4 ms on *one CPU core*; the entire 35-channel sweep is ~15 ms of
CPU against ~1.5 s of unavoidable radio time.

Ranked by payoff/effort:

1. **Batch replay for the A/B lab — the only real win.** The `tv_dual.py` / noise-sweep rig is
   CPU-bound and seed-starved (`equalizer_research_platform`: "measurement-noise law… any ±2-frame
   `long` claim is inside the noise"). A GPU-resident replay would let us run 100× the seeds and
   actually resolve ±2-frame differences. **Moderate effort, real payoff — but it is a
   *measurement* win, not a speed win.**
2. **Frequency-domain turbo equalization.** Already on the roadmap as the 4090's job
   (`equalizer_research_platform`: turbo-eq "must live on the 4090 as freq-domain 1-field-latency
   block", 2–5 dB). **High ceiling, high effort, and it is a QUALITY lever.**
3. **Parallel brute-force channel scanning.** Pointless — see the 1 260× above.
4. **Speculative multi-hypothesis lock.** Pointless — see §3.6.

Note also that PCIe round-trip latency (tens of µs) would be *added* to any feedback loop moved
to the GPU. The FPLL is a per-sample sequential loop; it must stay on the CPU. This is consistent
with the measured finding that the live throughput gate is the sequential front end and that
OpenMP equalizer fan-out was *monotonically worse* (`x86_cpu_savings_ceiling`).

**Verdict: honour the GPU-optional law. Nothing in the speed plan needs the 4090.**

---

## 5. PRIOR ART

### 5.1 Structural constants — primary source

All from [**ATSC A/53 Part 2:2011, RF/Transmission System Characteristics**](http://www.atsc.org/wp-content/uploads/2021/04/A53-Part-2-2011.pdf),
cross-checked against [**A/54A, Guide to the Use of the ATSC DTV Standard**](https://www.atsc.org/wp-content/uploads/2015/03/a_54a_with_corr_1.pdf).

| Constant | Value | Cite |
|---|---|---|
| Symbol rate | 4.5/286 × 684 = **10.7622377… MHz** | A/53 P2 §5.1 Eq. (1); A/54A §8.5.1 |
| Segment | **832 symbols = 77.3 µs** | A/53 P2 §5.1, §5.3.1, Fig. 5.2 |
| Field | **313 segments = 24.2 ms** | A/53 P2 §5.3.2, Fig. 5.2 |
| Frame | 2 fields = 626 segments = **48.4 ms** | A/53 P2 §5.1, Eq. (3) |
| **Pilot offset** | **309,440.559 Hz** above lower band edge | A/53 P2 §5.4.2 + fn. 4; arithmetic spelled out in A/54A §8.5.1 |
| Pilot power | **−11.3 dB** vs average data power; adds **0.3 dB** to total | A/53 P2 §5.4.2; A/54A Table 8.1, §9.2.14 |
| Segment sync | 4 symbols, **(+5, −5, −5, +5)**, not RS/trellis coded, not interleaved | A/53 P2 §5.3.1 |
| Field sync | 4 + **PN511** + **3× PN63** + 24 VSB-mode + 104 reserved = 832; **middle PN63 inverted on alternate fields** (the field-1/2 discriminator) | A/53 P2 §5.3.2, Fig. 5.9, §5.3.2.1–2 |

**This confirms our −2,690,559 Hz figure in §3.3 and both in-tree constants are wrong.**

### 5.2 ATSC A/74 — a verified *negative*

**A/74 contains no acquisition-time recommendation.** A full-text search of all 88 pages of
[A/74:2010](https://www.atsc.org/wp-content/uploads/2021/04/A74-2010.pdf) returns zero hits for
"acquisition", "acquire", "lock time", or "channel change". It covers sensitivity (−83 to
−5 dBm at TOV, §5.1), overload, phase noise (−80 dBc/Hz @ 20 kHz, §5.3), selectivity, multipath,
and the antenna interface.

Where it *does* touch timing it explicitly declines to specify, and — remarkably — it recommends
exactly what we already do:

> **§5.6, p.27:** *"The dwell time on each combination is not specified since the settling and
> convergence times vary by receiving chip design… **The best performing configuration should be
> stored for each channel.**"*

That is the per-channel profile concept, recommended by the standard body. Our tap cache is the
strongest form of it. §5.6 p.28 also recommends *"'off time' scans automatically"* — which is
what a fast scan makes practical.

### 5.3 Commercial lock times — what is and isn't published

**No public 8-VSB demodulator datasheet publishes an acquisition-time spec.** Checked and empty:
Auvitek AU8522, LG LGDT3306A, MaxLinear MxL69x, Oren OR51132, Philips TDA8960/1, Evertz
9780DM-VSB. (Si2168 and Sony CXD2837ER are DVB-only and not ATSC parts at all.)

What *is* public is the Linux driver acquisition budget each vendor chose — their own implicit
statement of how long lock takes:

| Part | `min_delay_ms` | Implied budget | Source |
|---|---|---|---|
| **LG LGDT3306A** | 100 ms | ~470 ms worst case | [`lgdt3306a.c`](https://raw.githubusercontent.com/torvalds/linux/master/drivers/media/dvb-frontends/lgdt3306a.c) |
| Oren OR51132 | 500 ms | 500 ms | [`or51132.c`](https://raw.githubusercontent.com/torvalds/linux/master/drivers/media/dvb-frontends/or51132.c) |
| Auvitek AU8522 | 1000 ms | 1 s | [`au8522_dig.c`](https://raw.githubusercontent.com/torvalds/linux/master/drivers/media/dvb-frontends/au8522_dig.c) |
| Samsung S5H1409 | 1000 ms | 1 s | [`s5h1409.c`](https://raw.githubusercontent.com/torvalds/linux/master/drivers/media/dvb-frontends/s5h1409.c) |

**Trend: 1000 ms (2006) → 500 ms → 100 ms (LGDT3306A).** Modern hardware locks ~10× faster than
first-generation parts. Two more published figures:
[US 8,233,095](https://patents.google.com/patent/US8233095B2/en) (Broadcom) assumes
*"approximately 0.5 seconds for a channel lock"*;
[EP1025697B1/US6118498](https://patents.google.com/patent/EP1025697B1/en) puts ATSC **video**
acquisition after lock at *"~200 ms average (6 frames), ~400 ms worst case (12 frames)"*.

**Our warm-start figure of ~250 ms to final MER is competitive with an LGDT3306A** and
comfortably better than the 2006-era parts. Our *cold* ~3 s is not.

### 5.4 Published fast-acquisition techniques — and which we already have

The canonical serial order is **A/54A §9.2.13, "Receiver Loop Acquisition Sequencing"**: LO →
non-coherent AGC → carrier (FPLL) → segment sync + clock → coherent AGC → field sync → NTSC
filter decision → equalizer → trellis/RS. Our chain is this exact order.

| Technique | Source | Our status |
|---|---|---|
| **Segment-sync correlation for symbol timing** | [US 5,949,834](https://patents.google.com/patent/US5949834A/en), Zenith (Laud & Mutzabaugh, 1997/1999): *"in only **ten segments** the symbol clock can begin pulling in"*; design target *"symbol frequency lock over a range exceeding ±70 ppm in under **200 milliseconds**"* | ✅ have it — and **faster**: our EMA α=0.40 locks in ~6 segments (0.46 ms) vs their 10 (773 µs) |
| **Warm-start per-channel loop offsets** | [US 7,230,654](https://patents.google.com/patent/US7230654B2/en): store per-channel carrier and symbol-timing offsets in EEPROM — *"Starting the CTL and the STR loops at the previously locked offset values can **substantially reduce the time of acquisition**"* | ⚠ **partial** — we cache equalizer taps but **not** the carrier offset. §3.3's FFT estimate is the missing half. |
| **Per-channel cached demod settings incl. equalizer coefficients** | [US 6,118,498](https://patents.google.com/patent/US6118498A/en), Sarnoff→MediaTek, filed 1997, granted 2000, **expired 2017**: caches *"tuner and demodulator settings (including frequency drift corrections and equalizer coefficients)"* per channel, plus background scanning of 12 likely channels and I-frame caching | ✅ **this is our tap cache, invented in 1997 and now public domain.** Independent convergence on the right answer, and free to use. |
| **Pilot-aided / swept carrier acquisition** | [US 6,665,355](https://patents.google.com/patent/US6665355B1/en), LSI Logic: digital frequency sweep, loop BW 3–6 kHz, sweep *"below a few hundred kilohertz per second"* — i.e. **~1 s to sweep ±100 kHz** | ✅ §3.3 beats this: a 2 ms FFT gives the answer to ±1–5 Hz, no sweep at all |
| **Pilot-less carrier acquisition via PN63 self-correlation** | [US 7,570,717](https://patents.google.com/patent/US7570717B2/en), Samsung: ±85 kHz range from 126 samples | ➖ not needed — we have a pilot |
| **★ Equalizer "data recycling" for fast convergence** | **Oh, Han, Jeon & Rhee, GLOBECOM 2003, pp. 3371–3375, [DOI 10.1109/GLOCOM.2003.1258860](https://doi.org/10.1109/GLOCOM.2003.1258860)** — the field sync arrives only every 260,416 symbols and is *"not long enough to guarantee equalizer operation"*; **reuse the stored field-sync sequence multiple times per field** plus sparse-tap selection | ❌ **we do NOT have this — see new lever §5.6** |
| **Gear-shifted LMS** | The textbook fix; gr-dtv still carries `// FIXME add gear-shifting` unaddressed since 2002 | ⚠ **built but OFF** (`STVT_EQ_GEAR_LMS`, `.cc:481`) — see new lever §5.6 |

Deeper reading list (all IEEE, verified DOIs): Ho, *IEEE Trans. Comm.* 22(11):1866, 1974
[10.1109/TCOM.1974.1092134](https://doi.org/10.1109/TCOM.1974.1092134) (VSB carrier recovery);
Laud/Aitken/Bretl/Kwak, *"Performance of 5th generation 8-VSB receivers"*, IEEE TCE 50(4), 2004
[10.1109/TCE.2004.1362501](https://doi.org/10.1109/TCE.2004.1362501); Lee et al., *"An adaptive
carrier synchronization technique for robust 8-VSB DTV reception"*, IEEE TCE 51(1), 2005
[10.1109/TCE.2005.1405696](https://doi.org/10.1109/TCE.2005.1405696); Chung, *"A DFE Structure
Using Quadrature Components of 8-VSB Signals"*, IEEE Trans. Broadcasting 54(3), 2008
[10.1109/TBC.2008.2001149](https://doi.org/10.1109/TBC.2008.2001149) — the last is directly
adjacent to the widely-linear work in `equalizer_research_platform`.

**Context:** ATSC 3.0's bootstrap ([A/321:2016](https://www.atsc.org/wp-content/uploads/2016/03/A321-2016-System-Discovery-and-Signaling.pdf))
exists *because* 8-VSB acquisition is slow: 3.0 detects and identifies a signal in **~2.5 ms**
versus ~482 ms for a conventional full-frame detect-and-identify.

### 5.5 What the open-source world does — and where we already beat it

**Every open-source 8-VSB receiver in existence is GNU Radio's gr-dtv or a port of it.** Verified
negatives: no Rust ATSC decoder, no pure-Python receiver, **no ATSC demod in SDRangel**
(its `demodatv` is analog ATV; `demoddatv` is DVB-S), SDR++ has only an
[open feature request](https://github.com/AlexandreRouma/SDRPlusPlus/issues/1303), and argilo's
ATSC flowgraphs are all *transmit*. [Xilinx/RFNoC-HLS-ATSC-RX](https://github.com/Xilinx/RFNoC-HLS-ATSC-RX)
ported gr-dtv to FPGA but **skipped sync, fs_checker and the equalizer** — the three hard blocks.

Head-to-head on acquisition, ours vs [gr-dtv](https://github.com/gnuradio/gnuradio/tree/master/gr-dtv/lib/atsc):

| | gr-dtv | **STVT (ours)** | Who wins |
|---|---|---|---|
| FPLL α | 0.01 (10× the old gr-atsc default, 2× its own "max useful" comment) | 0.001 | — |
| **FPLL AFC τ / capture range** | 5 µs → **±31.8 kHz** | 25 µs → **±6.4 kHz** | **gr-dtv, by 5×** ⚠ |
| Coarse frequency search | none — NCO hardcoded to −2.691 MHz | none — NCO hardcoded to −2.691 MHz | tie (both bad; §3.3 fixes ours) |
| Segment-sync lock | 11 segments (851 µs) | ~6 segments (0.46 ms) | ours |
| PN511 error limit | 20/511 | 50/511 | ours (more permissive = faster acquire) |
| **Equalizer taps** | **64** (a 4× regression from old gr-atsc's 256) | **256** | **ours, by 4×** |
| Tap initialisation | **exactly zero** → first whole field outputs 0.0 | delta at tap 51, **or warm cache** | **ours** |
| Decision-directed mode | **none at all** | built (`STVT_EQ_DD_MU`) | ours |
| DFE | dead code (`float kludge() { return 0.0; }`) | built (`STVT_EQ_DFE`, NFB 192) | ours |
| Gear-shifting | `// FIXME` since 2002 | built but **off** | ours, if we turn it on |
| Warm-start cache | none | **yes, per channel+antenna** | **ours — the big one** |
| Implied acquisition | ~200–270 ms clean, equalizer ≈ 70 % of it | ~250 ms warm / ~3 s cold | comparable warm, worse cold |

**Two actionable findings from this comparison:**

1. **Our carrier capture range is 5× narrower than gr-dtv's** (±6.4 kHz vs ±31.8 kHz), because our
   AFC time constant is 25 µs against their 5 µs. That is a deliberate noise-rejection trade, but
   it means any transmitter/LO combination beyond ±6.4 kHz simply never locks for us and *would*
   for stock GNU Radio. **This raises the priority of lever #6 (FFT-seeded NCO)** from "saves 1 ms"
   to "removes a real failure mode we are more exposed to than upstream."
2. gr-dtv's historical comments are the only lock-time documentation GNU Radio ever wrote, and
   they are about *our* α: `gr-atsc/src/lib/atsc_fpll.cc` @ v3.6.5.1 —
   *"alpha = 0.002; // takes about 15k samples to pull in… or about 120k samples on noisy data"*,
   with 0.001 as the shipped default. At our 11.84 MS/s that is **1.3 ms clean, ~10 ms on noisy
   data** — larger than my §3.1 estimate, still small against the equalizer.

### 5.6 ★ Two new levers this literature turned up

**Lever 12 — equalizer data recycling (Oh et al., GLOBECOM 2003).** The equalizer trains on 704
symbols out of every 260,416 — a **0.27 % duty cycle** — which is precisely why cold convergence
takes ~124 fields. The published fix is to *store the field-sync segment and run the LMS over it
N times per field* instead of once. Cost: N× the LMS arithmetic on 704 symbols, once per 24.2 ms
— utterly negligible on a Threadripper. Expected: **cold convergence ~3 s → ~3/N s**, and it
composes with the warm cache (it makes the *first-ever* visit fast, which the cache cannot).
**Effort: S–M, inside `adaptN` only. Risk: it is still supervised LMS on known symbols — the
same gradient, just applied more often; the divergence bail already guards the failure mode.**
This is the best cold-start lever available and it is 23 years old and published.

**Lever 13 — turn on gear-shifted LMS for cold start only.** `STVT_EQ_GEAR_LMS` exists
(`.cc:481-519`) with `BETA_FAST=5e-5`, `BETA_SLOW=1e-6`, and a 100-field debounce. Today it is
off, so we run one μ forever. The textbook use is **fast μ while converging, slow μ once
locked** — exactly the FIXME gr-dtv never fixed. Note `mer_dial_universal_algorithm` §(6) found
`GEAR_LMS` in every top-10 chain-lab config *and* that the whole slow-adaptation family
**failed live validation** — so this must be gated to the cold-start window only and never left
on as a steady-state setting. **Effort: S. Risk: medium — respect the 7/02 live-validation
lesson; A/B live, not just in replay.**

---

## 6. WHAT TO MEASURE BEFORE BUILDING

Everything below is cheap, and three of the six are already written.

| # | Measurement | How | Why it gates a build |
|---|---|---|---|
| **M1** | **Baseline retune latency, ×12** | `adaptive-tv/retune_stopwatch.py RF PROG VIRT NAME ANT --label baseline` — **already written, and `retune_stopwatch.jsonl` is currently empty.** Run 12 transitions (UHF↔UHF, UHF↔VHF, antenna switch) with `STVT_PERSIST_RETUNE` on and off. | This is the number lever #1 claims to move. **Without it we have no before.** |
| **M2** | **RSPdx retune settle floor** | Park on a known-strong channel; retune away and back; capture continuously; find the first sample index at which pilot SNR recovers. Sweep the drain length 5/10/20/40/80 ms. ~90 s of radio time. | Sets the real per-channel scan floor (40 ms is proven, 15 ms is speculation). |
| **M3** | **One wideband capture between two adjacent channels** | LO at (f<sub>N</sub> + f<sub>N+1</sub>)/2 ≈ f<sub>N</sub> + 3 MHz, 8 MS/s, 200 ms, on a pair where **both** channels are live (RF 34/35 or RF 35/36 in DC). Then re-run `scratchpad/dwell_sweep.py`'s detector at ±3 MHz pilot offsets. | The only honest way to settle §1.2. My synthetic test was invalid by construction (overlapping guard bands) and I will not build on it. |
| **M4** | **Cold-EQ convergence, actually measured** | Replay `lab/marginal_iq/rf34_ctrl.cs16` through `tools/tv_replay.py` with `STVT_EQ_TAP_CACHE_FILE` unset vs set, `STVT_EQ_TELEM=1`; plot `fs_err_rms` vs field-sync index. **Offline, no SDR.** | The "~3 s cold" figure is a code comment (`tv_live.py:933`), not a measurement. The theoretical fast-mode floor is 110 ms. If the truth is 500 ms, lever #1's payoff shrinks — better to know first. |
| **M5** | **Warm-start hit rate across a real scan** | Count `[eq-long] WARM START` lines in `scan_rf*.log` for a scan from an empty cache dir, then a second scan. | Quantifies defect D2. On 2026-07-26 it was 7/9 — but that cache had been built up over weeks. |
| **M6** | **Frames, not MER, as the gate** | Any promotion must pass `ffmpeg` null-sink `-map 0:v` frame counts (**not** `ffprobe -count_frames`, which lies on multi-program TS), and **OsO == 0** under full live load. | Two standing laws: `real_quality_metric_ffmpeg_fps`, and the overflow gate from `drizzle_wave_interferer` / the WL live regression. A faster lock that drops frames is not faster. |
| **M7** | **Our actual carrier capture range** | Offline: take `lab/marginal_iq/rf34_ctrl.cs16`, multiply by `exp(2πjΔf t)` for Δf ∈ {0, ±2, ±5, ±10, ±20, ±40} kHz, replay each, record whether it locks. **No SDR.** | §5.5 predicts we fail past ~±6.4 kHz while gr-dtv would survive to ~±32 kHz. If confirmed, lever #6 stops being a speed item and becomes a **bug fix**. |

**Order of operations:** M4 + M7 (both offline — do them today, they need no radio) → M1 (needs
10 min of radio) → **build lever #1** → re-run M1 → M5 → build levers #2–#5 → M2, M3 → decide on
levers #6, #8, #13.

**Do not run M4/M7 while the day-program ladder is decoding.** They are `tv_replay` jobs and will
steal matched-filter cycles from the live chain (`dont_hammer_chain_cpu`).

---

## 7. RECOMMENDED FIRST BUILD

> **Runtime tap-cache rebind, so persistent retune keeps its warm start.**
> ~2 lines of C++ (drop one `static`, add a `save` verb), ~15 lines of Python in
> `TVLive.retune()`, and delete the guard at `tools/tv_live.py:924-937`.

**Why this one:**

- It is the only change in the dossier that moves the dominant term. Everything else trims
  hundreds of milliseconds off a chain that already acquires in 250 ms.
- It uses machinery that is **already built, already installed, and already proven** — the
  command port, the cache format, the quality gates, the divergence bail. Nothing new is
  invented.
- It is measurable with an instrument that is **already written and currently unused**
  (`retune_stopwatch.py`).
- It has an exact revert: restore the guard, and the behaviour is byte-identical to today.

**Build discipline (non-negotiable, per standing law):**

1. Run **M4** and **M1** first. No before, no claim.
2. The C++ change goes through `_rebuild.bat` **and** an explicit install — the build script does
   not install (`gr_atscplus_build_install_gotcha`), and the panel must be **stopped** before
   copying the `.pyd`/`.dll`, in **separate steps**.
3. Regression-gate every step against a `tv_replay` of `rf34_ctrl` — PID count parity, and for
   the default (non-retuning) path, **md5 parity of the output TS**. The default path must stay
   bit-identical.
4. Promote live only after **OsO == 0** across a full-load run and a frames-based A/B.
5. Do this while the user is watching. It touches the live decoder front end.

**Then, in order:** lever #2 (persistent-chain phase 2) → #3/#3b (cache persistence + data
recycling — together they make *first-ever* visits fast, which the cache alone cannot) → #4/#5
(the cheap scan trims) → #9/#10 (the free panel-timer trims).

**Do not do, in order of temptation:** speculative parallel decode (§3.6), GPU scanning (§4),
any whole-band TV channelizer (§1.1), any threshold loosening in `fs_checker` (§3.7 — we are
already 2.5× more permissive than gr-dtv), and buying an RSPduo or RX888 for wideband TV (§1.4).

---

## Appendix — measurement scripts

All read-only, all offline, all preserved in **`Z:\src\magic-tv-decoder\lab\speed\`**. They touch
nothing but `tools/scan_lab/fixtures/*.cf32` and re-run in under a minute each:

- `dwell_sweep.py` — detection vs dwell length across all 35 fixtures (§2.2 table)
- `dwell_sweep2.py` — threshold re-optimisation vs dwell; analysis-band stress (§2.2)
- `wideband_proof.py` — dual-pilot geometry test + channelizer cost (§1.2, §1.3)

**Nothing in this dossier modified DSP source, touched the SDR, or interacted with a running
daemon.** The only writes were `lab/speed_dossier.md` and `lab/speed/*.py`, both untracked; the
`scan_lab/harness.py` / `winning_recipe.json` referenced in §2.2 were read out of git history
(`git show 3426b08:…`), not restored to the tree.
