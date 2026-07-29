# SPEED-1 BUILD WORKLOG — levers 1/2/3 from `lab/speed_dossier.md`

Branch `speed-1` off `main-universal` (5cfe274). Local only, never pushed.
Constraint from the user: **fastest possible with NO sacrifice to quality or
accuracy, must work on ANY user's antenna, DO NOT BREAK THE TELEMETRY.**

| Lever | What | Gate |
|---|---|---|
| 1 | runtime tap-cache rebind (persist-retune keeps its warm start) | channel-change wall time ↓, frames unchanged |
| 2 | fast two-stage pilot scan (2.05 ms stage A → confirm stage B) | zero lost true positives on all 35 fixtures |
| 3 | equalizer data recycling (Oh et al., GLOBECOM 2003) | cold field count ↓, no steady-state quality loss |

---

## 0. Ground rules recorded before touching anything

* `_rebuild.bat` **does not install** — `cmake --install` + the
  `Library\Lib\site-packages` → `Lib\site-packages` xcopy are mandatory
  (`gr_atscplus_build_install_gotcha`).
* Quality = **ffmpeg null-sink `-map 0:v` frame count**, never ffprobe
  (`real_quality_metric_ffmpeg_fps`, and ffprobe lies on multi-program TS).
* `atsc_equalizer_long` is **not bit-reproducible across processes** (volk
  dot-product kernel selection by pointer alignment). Any ±2-frame or md5
  claim needs multi-run evidence (`equalizer_research_platform`, 7/29 law).
* Live promotions gate on **OsO == 0** (`drizzle_wave_interferer`).
* Radio: `day_program_729.py` held the SDR until its last cycle finished
  17:31:59 (`END_HOUR=18`, so no further cycle). Live work only after that,
  under `radio_lock` priority 80, with stray sweep + SDRplay service bounce.

## 0.1 Starting state

* Working tree carried one uncommitted `tools/tv_live.py` change on
  `stvt-2.0-wl` (SDRplay open retry ladder). Stashed as
  `stash@{0}` "WL branch: SDRplay open retry ladder…" — **not lost, not mine.**
* The **installed** gr-atscplus module at session start was the `stvt-2.0-wl`
  build (contains `atsc_equalizer_wl` + `atsc_wl_frontend`). Backed up before
  any install so WL can be restored.

---

## 1. Baselines (offline, `lab/speed_build/replay_bench.py`)

Production chain env, `STVT_EQ=long`, telemetry at every field sync, cold
(no tap cache). Quality = ffmpeg null-sink `-map 0:v` frames.

| capture | frames (3 runs) | md5 | fields to MER 19.69 dB | plateau MER |
|---|---|---|---|---|
| rf34_ctrl (clean) | 403 / 403 / 403 | 2 distinct hashes over 6 runs | **210 (5.9 s)** | 20.52 dB |
| rf7_marg (fading) | 251 / 251 / 251 | 3 distinct hashes | 49 | 16.48 dB |

**The volk law is real and measured here.** `atsc_equalizer_long` produced two
different md5s over six identical rf34 runs and three different md5s over
three identical rf7 runs, with the FRAME count rock steady. So md5 parity is a
statistical statement (post-change hashes must be drawn from the pre-change
set), and frames are the gate — exactly as
`equalizer_research_platform`'s 7/29 measurement-noise law says.

**Cold convergence is 210 field syncs = 5.9 s of stream**, not the ~3 s the
code comment claimed (`tv_live.py` old line 933). The 15.2 dB cliff, though,
is cleared in **6 field syncs (145 ms)** — so cold start is not "no picture
for 6 seconds", it is "6 seconds to reach the MER this channel is capable of".

## 2. Lever 2 — two-stage scan: GATE PASSED (commit f30af73)

See the commit message for the full table. Headline:
`hot` verdict TP=14 FP=0 FN=0 on all 35 fixtures; `weak` and `atsc3` verdicts
identical too; phase-1 radio time 4.90 s -> 3.77 s (1.30x).

**Honest scope note:** phase 1 is ~5 % of a scan. The dossier says so plainly
(§2.2 "Phase 1 is not the problem"), and the measurement agrees: 14 of 35
frequencies are real channels that MUST be confirmed, so with an unchanged
100 ms confirm stage the ceiling in this market is 1.71x on phase 1. Going
faster means shortening the confirm dwell, which I declined to make a default
(see the REFUSED section at the end).

## 3. Lever 3 — data recycling: the measurement that changed the design

`STVT_EQ_RECYCLE=N` reruns the supervised LMS N times over the stored field
sync (Oh, Han, Jeon & Rhee, GLOBECOM 2003). First cut applied it for the whole
run. On the clean capture it was pure profit; on a FADING capture it was a
regression:

| capture | N=1 frames | N=8 frames (unbounded) | plateau MER N=1 -> N=8 |
|---|---|---|---|
| rf34_ctrl clean | 403, 403 | 403, 403 | 20.52 -> 20.81 dB |
| rf35_marg | 396, 396 | 396, 396 | 20.46 -> 20.55 dB |
| rf9_marg | 113, 114 | **350, 349 (+209 %)** | 16.83 -> 16.68 dB |
| rf7_marg fading | 251, 250 | **214, 213 (-15 %)** | 16.48 -> 16.28 dB |

Cold convergence on rf34, one absolute ruler (fs_err_rms <= 0.5179 = MER
19.69 dB, `analyze_curves.py`):

| N | fields to target | seconds | plateau MER |
|---|---|---|---|
| 1 (today) | 210 | 5.90 | 20.52 |
| 2 | 128 | 3.55 | 20.75 |
| 4 | 72 | 1.96 | 20.83 |
| 8 | 24 | 0.62 | 20.81 |
| 16 | 24 | 0.64 | 20.79 |

So recycling is a **convergence** lever, not a steady-state setting — the same
lesson `mer_dial_universal_algorithm` §(6) learned about the slow-adaptation
family. Redesigned: recycling is confined to the first
`STVT_EQ_RECYCLE_FIELDS` field syncs (default 40 ~ 1 s, measured to cover the
whole transient) and steady state is left exactly as today. The `warm` command
resets `d_fs_trained`, so a retune re-arms the window for the new channel.

The unbounded form (`STVT_EQ_RECYCLE_FIELDS=0`) stays available as a RESEARCH
lever with the honest mixed result above (+209 % on rf9, -15 % on rf7) — a
per-channel decision for the perfect-tune table, not a default.

## 4. Lever 1 — the live retune measurement, and the honest negative

`retune_bench.py`, RF36 <-> RF34 <-> RF31 (all UHF, Old Faithful port B),
one persistent tv_live walked around the ladder by retune.cmd.

**The mechanism works, 9/9.** Every transition: `SHERIFF cmd 'save'` ->
`eq cache rebound -> taps_AntennaB_rf<NN>.bin` -> `WARM START ... (|taps|=..)`
-> `SHERIFF cmd 'warm'`. The cached norms are channel-specific and stable
across visits (rf36 1.524-1.545, rf34 1.469-1.476, rf31 1.477-1.488), i.e.
the cache really is a per-channel fingerprint and the rebind really picks the
right one.

**But the channel-change time did not move on this ladder:**

| arm | t_video median | valid samples |
|---|---|---|
| cacheless (today) | 0.358 s | 0.394, 0.321 |
| warm (speed-1) | 0.364 s | 0.317, 0.300, 0.651, 0.371, 0.364 |

**Why — and this corrects the dossier.** §3.1b claims persist-retune is
"forced cold ⇒ ~3 s". It is NOT cold: with the cache disabled the equalizer
simply keeps adapting from the taps it already had, so a retune between three
UHF channels through one antenna starts from a nearly-right answer. Measured
`fs_err_rms` on the first field syncs after a cacheless retune: 0.39-0.50,
i.e. MER 20-22 dB immediately. There was no 3 s cold transient to remove.

So lever 1's payoff is NOT "sub-second channel change on the persist path" —
that path was already sub-second. What lever 1 actually buys is:

1. **Correctness of the retuned chain's taps.** Before, a retuned chain wrote
   nothing and ran on hand-me-down taps from the previous channel. That is
   benign between adjacent UHF channels (measured above) and wrong across a
   big change (VHF<->UHF, antenna switch) — exactly the transitions this rig
   has historically struggled with.
2. **The FRESH-PROCESS tune** — the panel's classic path and the scanner's
   per-candidate spawn — now reliably has a cache to warm-start from, because
   D2/D3 are fixed. Measured offline: 210 field syncs (5.08 s) cold vs
   **6 field syncs (0.12 s) warm = 42x**, frames 403 either way.

Overflow gate: cacheless 29 OsO / 270 s, warm 36 OsO / 400 s — 0.107 vs
0.090 per second, i.e. indistinguishable and ambient (two radiotuna jobs were
holding ~25 % of the box). NOT zero, so this is not a promotion-clean number;
it is a "the change did not make it worse" number.

**One defect found and fixed by this run:** `TVLive.retune()` rebound
`STVT_EQ_TAP_CACHE_FILE` even when the session had the cache disabled, which
re-enabled the equalizer's periodic WRITE while the load stayed off. Now
gated on main() having published the path.

## 5. THE LIVE GATE — `live_bench.py`, RF34, Old Faithful port B, 30 s per
## trial, one FRESH tv_live process per trial (the path the panel and the
## scanner actually take), 3 trials per arm, medians

| arm | fields to MER 19.69 dB | = seconds | frames / 30 s | MER | **OsO** |
|---|---|---|---|---|---|
| `base` today, no cache | 65 | 1.57 | 869* | 21.84 | **0 0 0** |
| `cold` cache configured, empty | 65 | 1.57 | 857* | 21.83 | **0 0 0** |
| **`warm` lever 1** | **7** | **0.17** | 840 | 21.90 | **0 0 0** |
| **`rec8` lever 3, EMPTY cache** | **14** | **0.34** | 839 | 21.88 | **0 0 0** |

* pooled over 6 runs each after the order control below.

* **OsO == 0 in all 18 live runs.** The overflow law is satisfied.
* **lever 1 live: 65 -> 7 field syncs = 9.3x faster to final MER**, and the
  MER it settles at is the same or 0.06 dB better.
* **lever 3 live: 65 -> 14 field syncs = 4.6x, from an EMPTY cache** — this
  is the one thing the tap cache fundamentally cannot do: make a
  FIRST-EVER visit fast.
* **Order control.** The first pass showed `base` 870 frames vs `cold` 839 and
  I did not accept it: rerunning with the arms REVERSED gave `cold` 873 vs
  `base` 868. Whichever arm runs first wins, in both directions, so the 3 %
  was airtime drift over the 9-minute sequence, not the cache. Pooled
  medians 869 vs 857 with fully overlapping ranges (826-889 both).

## 6. Lever 2 live: phase-1 sweep A/B over the real 35-frequency table

| sweep | radio time | wall |
|---|---|---|
| two-stage (shipped) | 7.24 s / 7.20 s | 9.07 s / 8.97 s |
| single-stage (STVT_SCAN_FAST=0) | 8.85 s / 8.83 s | 10.76 s / 10.60 s |

**1.22x live** (vs 1.30x predicted from fixtures). The gap is honest: the
real per-tune overhead is ~140 ms (SoapySDR setFrequency + the 40 ms drain
loop + python), not the 42 ms the arithmetic assumes, so the 2.05 ms saving
per frequency is diluted.

**Channel-set parity on live air is not a usable gate, and the control proves
it:** the two SINGLE-STAGE sweeps disagreed with EACH OTHER (run 1 found
9 channels, run 2 found 11 — RF26/545 MHz and RF27/551 MHz flipped in). Those
are exactly the fixtures sitting within 1 dB of the strict gate. That is why
the parity claim is made on the deterministic fixture rail (TP 14 / FP 0 /
FN 0 over 35 captures, all three verdicts identical) and not on air.

## 7. Telemetry: re-ran the real consumers, not a re-implementation

`lab/speed_build/telemetry_check.py` runs the OWNERS' verbatim regexes over
post-change chain logs (replay, live fresh-process, live persistent-retune):

* panel `RE_FS` (the MER dial) — OK, 1201 hits on a live log
* panel `RE_FPLL` (in_rms / max|x|) — OK
* panel `RE_RS5` (loss %) — OK
* `quality_tuner` `RELOCKS_RE` + `ALIGNED_RE` — OK on replay and live
* `tv_dual` `_RE_LONG` (the paired MER series) — OK
* `day_program_729`'s `in_rms` split and `frame=` regex — OK
* `tools/stvt_docs_guard.py` — CLEAN (all four contract tags still emitted)

One apparent break needs stating: `sync_soft FINAL` is missing from BOTH
retune-bench logs — the `cacheless` (today's behaviour) one and the `warm`
one, same binary, same shutdown path — and present in every replay log and
every live_bench log. It is that harness's kill path, not a regression.

## 8. WHAT I REFUSED TO DO

1. **Shorten the scan's confirm dwell from 100 ms.** A 16.4 ms confirm would
   take phase 1 to ~2.7x, and the dossier's own table says 16.4 ms detects
   the same 8 channels. But the confirm stage is what protects the WEAKEST
   channels, the fixtures are one market on one antenna on one evening, and
   the requirement is "works on ANY user's antenna". Left as
   `--dwell-sec`/`STVT_SCAN_FAST_DWELL`, not a default.
2. **Shorten the 40 ms retune drain in `sdr_sweep`.** 40 ms is the only
   settle proven in-tree (it produced the fixtures). The dossier's 15 ms is
   explicitly speculation pending measurement M2. Untouched.
3. **Touch the scan's 2 s USB pre-flight / 2 s and 3 s inter-phase sleeps**
   (7 s, more than phase 1 itself). They exist for SDRplay release, and the
   7/29 three-layer contention lesson is exactly what happens when that
   window is too small. Untouched.
4. **Flip `STVT_EQ_RECYCLE` on by default.** It ties or wins on all four
   replay captures and passed the live gate with OsO == 0 — but on ONE live
   channel. A DSP default that every user inherits deserves more than one
   channel and one evening.
5. **Change the FPLL's NCO seed constant** (`atsc_fpll_tight_impl.cc:43`,
   -2,691,000 Hz vs the spec's -2,690,559.441). It is inside the sample loop's
   initial condition, so changing it changes the output TS bit-for-bit on the
   DEFAULT path — the one thing that must not move. The measured LO error is
   +0.7 ppm and the FPLL has +/-6.4 kHz of pull-in, so the 441 Hz is worth
   ~0 s. Left alone; the detector-side constant (which does not touch the
   decoder) IS fixed.
6. **Recycling without a cold-start window.** The published algorithm run
   unbounded regressed the fading rf7 capture by 15 % of its video frames.
   Available as `STVT_EQ_RECYCLE_FIELDS=0` for research, never as a default.
