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

## 9. Post-fix smoke, with a genuinely clean cache (`RB_DIR=retune2/3`)

`--arm cacheless` after the disable fix: **zero cache files, zero
`SHERIFF cmd` / `cache rebound` / `persisted on stop` lines** — the disabled
path is now truly inert (before the fix it was silently writing). OsO 0.

`--arm warm`, empty cache, RF36 <-> RF34, 4 transitions:

```
[1] -> RF34  COLD START (no cache for this channel) - taps reset to delta   t_video 0.264 s
[2] -> RF36  WARM START ... (|taps|=1.422)                                  t_video 0.647 s
[3] -> RF34  WARM START ... (|taps|=1.385)                                  t_video 0.538 s
[4] -> RF36  WARM START ... (|taps|=1.449)                                  t_video 0.427 s
4 x SHERIFF cmd 'save', 2 cache files written, OsO 0
```

So the full state machine is exercised on air: first-ever visit -> delta (NOT
the previous channel's multipath), later visits -> that channel's own taps,
and the delta case does not delay video either (0.264 s).

## 10. Reverts

| to undo | do this |
|---|---|
| lever 1 | `STVT_PERSIST_RETUNE_CACHE=0` (retuned chains run cache-less again) |
| lever 1's cadence | `STVT_EQ_CACHE_EVERY=1024` (the old 24.8 s interval) |
| lever 2 | `STVT_SCAN_FAST=0` (single-stage full-dwell sweep) |
| lever 2's pilot constant | `STVT_PILOT_OFFSET_HZ=-2690000` |
| lever 3 | already off; it needs `STVT_EQ_RECYCLE=N` to do anything |
| everything | `git checkout main-universal` + rebuild/install gr-atscplus |

The installed gr-atscplus module is now the **speed-1** build, which does NOT
contain `atsc_equalizer_wl` / `atsc_wl_frontend` (those live on
`stvt-2.0-wl`). The panel and tv_live default to `STVT_EQ=long`, so normal
viewing is unaffected, but `STVT_EQ=wl` and `tv_dual.py` need the WL module
back. Two ways:
* restore the pre-session binaries from the backup taken before any install:
  `<scratchpad>/atscplus_wl_installed_backup/` (module dir + the DLL), or
* cherry-pick `7427aea` onto `stvt-2.0-wl` and rebuild — the C++ diff is
  three self-contained hunks in `atsc_equalizer_long_impl.{cc,h}` and applies
  cleanly (that branch's only change to those files is the TELEM_EVERY knob,
  which this branch now also carries, identically).

---

# 11. THE UNIFIED BUILD (2026-07-29 evening) — branch `stvt-2.0-wl-speed`

The problem §10 left behind is now closed: there is ONE installed
gr-atscplus that carries **both** research tracks, so the panel can drive
`STVT_EQ=wl` / `tv_dual.py` AND the speed levers without reinstalling
between experiments. CPU/build only — **no SDR was touched** (a balloon-hunt
window holder owned the radio 18:38-20:25; every number below is offline
replay).

## 11.1 Merge strategy

`git checkout -b stvt-2.0-wl-speed stvt-2.0-wl` then `git merge speed-1`.
A merge, not a cherry-pick: the two branches share `main-universal`
(5cfe274) as their merge base, so a merge brings the whole speed-1 set —
including `.gitignore`, `lab/speed*`, `tools/sdr_sweep.py` and
`tools/tv_tuner.py` — with correct history and no replayed hunks.

Two commits on the branch:

| commit | what |
|---|---|
| `b01bdce` | the merge (WL 2.0 x speed-1 levers 1/2/3) |
| `39939ad` | `tools/tv_live.py` SDRplay open-retry hardening, recovered from `stash@{0}` |

## 11.2 Conflicts, and how they were resolved

**1. `gr-atscplus/lib/atsc_equalizer_long_impl.cc` — the ONLY textual
conflict, and it was comment-only.** Both branches had independently added
the *identical* `STVT_EQ_TELEM_EVERY` cadence knob (WL needed it for
tv_dual's paired MER series; speed-1 needed it for field-resolution
convergence curves) with different explanatory comments, and speed-1 also
adds `#include <cstdio>`. Kept `<cstdio>`, kept speed-1's code verbatim,
wrote one comment naming both callers. **Verified:** the merged file is
byte-identical to `speed-1:atsc_equalizer_long_impl.cc` except that comment
block (diff with `--strip-trailing-cr` shows exactly one hunk, all `//`
lines).

**2. `tools/tv_live.py` — NO conflict, git auto-merged, and that is
legitimate rather than lucky:** the three contributors edit disjoint regions
of the file.

| region | owner | what lands there |
|---|---|---|
| ~205-240 | `stash@{0}` | SDR open retry ladder + service restart |
| ~460-720 | stvt-2.0-wl | `STVT_EQ=wl`, fused `atsc_wl_frontend`, WL edge wiring, `STVT_EQ_DUMP` |
| ~770-1010 | speed-1 | `_eq_cmd` / `_eq_cache_path`, retune save-rebind-warm, `STVT_PERSIST_RETUNE_CACHE` default flip, `STVT_EQ_LKG` + `STVT_EQ_CMD_FILE` defaults |

All three verified present in the merged file by grep. **Nothing was
dropped from either side, so no safety-vs-feature tie-break was needed.**
`stash@{0}` was applied with `git stash apply` (not `pop`) — it is still
there as a safety net.

**Caveat carried forward from the stash, stated not silenced:** the
64 s-then-`Restart-Service SDRplayAPIService` fallback is a *global* side
effect. If another process legitimately owns the RSP (a window holder, the
observatory, the storm watch), it yanks it after 64 s of waiting rather than
backing off. It only fires on the sdrplay driver and only after the full
ladder, but a `radio_lock`-aware backoff is the better long-term answer.

## 11.3 Install verification — BOTH feature sets in the loaded module

`_rebuild.bat` then `_install.bat` (install is mandatory, §0).
Installed artefacts: `gnuradio-atscplus.dll` md5 `54359c27a67014c2...`,
`atscplus_python.cp312-win_amd64.pyd` md5 `2e80d2da48a754ac...`.

WL side — constructed from the installed module, banners are the proof:

```
[eq-wl] build 2026-07-29 v3 (adaptive conjugate shrinkage, ntaps=128, shrink=off)
[wl_front] FUSED v1 (2026-07-27) rate=... interp=129x8 ... fs_validate=ON
atsc_equalizer_wl PRESENT   atsc_wl_frontend PRESENT
```

Speed side — the levers are env / command-port driven, so the DLL's own
strings and the live banners are the proof: `STVT_EQ_RECYCLE`,
`STVT_EQ_RECYCLE_FIELDS`, `DATA RECYCLING`, `WARM START`, `COLD START`,
`SHERIFF cmd`, `cache persisted on stop`, `STVT_EQ_CACHE_EVERY`,
`STVT_EQ_CMD_FILE`, `STVT_EQ_TELEM_EVERY` — all present; plus the WL-only
`STVT_WL_SHRINK*` set. Observed live in the gate logs below:
`[eq-long] DATA RECYCLING x8 for the first 40 field syncs`,
`[eq-long] WARM START`, `cache persisted on stop`.

## 11.4 GATES — all passed

**G1 default path (STVT_EQ unset/long) unchanged.** Multi-run per the volk
law (§1): frames are the gate, md5 must be drawn from the known set.

| capture | frames | md5s seen | fields | converge_field |
|---|---|---|---|---|
| rf34_ctrl x3 | **403 / 403 / 403** | `f1f867c5...` x2, `3d8c11ee...` | 620 | 210 / 210 / 210 |
| rf7_marg x2 | **251 / 250** | `ac5ff168...`, `2b3d7075...` | 620 | 49 / 49 |

Both rf34 hashes are in the documented pre-change set (`F1F867C5...`,
`AA0DB81B...`, `3D8C11EE...`). rf7 frames sit in the documented 250-251
band, and its md5 was never reproducible. Convergence 210 (rf34) and 49
(rf7) are the §1 baselines *exactly*.

**G2 `STVT_EQ=wl` still decodes, at the WL v3 numbers.** `lab/wl_v3/sweep.py`
(sample-aligned tv_dual), arm `v2`:

| capture | long | WL | doc'd WL | imag_frac | benefit | kappa | ctl |
|---|---:|---:|---:|---:|---:|---:|---|
| rf34 clean | 403 | **403** | 403 | 0.206 | 0.932 | 0.000 | OK |
| rf34+AWGN 2147 (knee) | 130 | **226** | 226-230 | 0.120 | 0.597 | 0.000 | OK |
| rf7 marginal | 251 | **257** | 257 | 0.146 | 0.589 | 0.000 | OK |
| rf9 marginal | 112 | **349** | 348-350 | 0.119 | 0.634 | 0.003 | OK |

Every WL number is on or inside its recorded band; `imag_frac`, benefit and
kappa reproduce to three decimals. The `long` leg is the built-in control
and was identical within each capture.

**G3 `tools/tv_dual.py` reproduces its documented md5 pair** — rf34_ctrl:

| leg | md5 | expected |
|---|---|---|
| long | `F1F867C5567B33721684F4FBF7C423BB` | == production `tv_replay STVT_EQ=long` |
| wl | `AF9769A6F60C2BEBF6C6A50CF7CD8440` | == the 7/27 fused hash |

Both exact. The harness is still byte-for-byte the production decode on one
leg and the recorded fused decode on the other.

**G4 the levers still behave.** One replay each, scored on the ONE absolute
ruler (`analyze_curves.py --target 0.5179` = MER 19.69 dB) so the arms are
comparable:

| arm | frames | fields_to_target | t_s | plateau | MER dB | banner |
|---|---:|---:|---:|---:|---:|---|
| default | 403 | 210 | 5.13 | 0.4708 | 20.52 | — |
| `STVT_EQ_RECYCLE=8` | 403 | **24** | 0.55 | 0.4625 | 20.68 | `DATA RECYCLING x8 ... first 40 field syncs` |
| tap cache, cold (empty) | 403 | 210 | 5.13 | 0.4709 | 20.52 | wrote `taps_TEST_rf34.bin` |
| tap cache, warm | 403 | **6** | 0.12 | 0.4588 | 20.75 | `WARM START`, `cache persisted on stop` |

§3's recycling table said 24 fields at N=8 — reproduced exactly. §4's
fresh-process warm-start claim was 6 fields / 0.12 s / 403 frames —
reproduced exactly. Frames never moved (403 in all four arms).

**G4b lever 2 on the deterministic fixture rail** (`scan_gate_study.py`, 35
OTA fixtures): `HOT TP=14 FP=0 FN=0 TN=21`, `VERDICT MISMATCHES: NONE`
(hot / weak / atsc3 all match), phase-1 radio time 4.90 s -> 3.77 s =
**1.30x**. Identical to §2.

**G5 telemetry + docs.** `lab/speed_build/telemetry_check.py` over three
post-merge chain logs (default / recycle / warm-cache): **TELEMETRY
INTACT** — panel `RE_FS` 620 hits, `RE_FPLL` 115, `RE_RS5` 3,
`quality_tuner` RELOCKS + ALIGNED, `tv_dual _RE_LONG` 620,
`day_program_729` in_rms split, `OsO/overflow = 0` in all three.
`tools/stvt_docs_guard.py` -> **CLEAN** (exit 0; 0 errors, warnings only,
all four contract tags still emitted).

## 11.5 Two findings that are NOT merge regressions, but must be recorded

**(a) `atsc_wl_frontend` has an intermittent hard lock failure (~5%).** In a
minority of standalone `tv_replay STVT_EQ=wl` runs the fused front end never
achieves timing lock at all — `relocks=0`, `segs_aligned=0 (0.00%)`,
`fs accepted=0` — and it free-runs, emitting `segs_emitted=291044` instead
of `194030` (exactly the 1.5x SPS ratio, i.e. one output per input *sample*
instead of per *symbol*). The TS comes out **0 bytes**.

Measured, same deterministic 480 MB fixture, same env:

| build | runs | lock failures |
|---|---:|---:|
| unified (`stvt-2.0-wl-speed`) | 39 | **2** (~5%) |
| pre-merge WL backup (47d419f state) | 47 | 0 |

Fisher exact p ~ 0.22 — **not significant**, and the arms are not perfectly
matched (the backup DLL predates `e439374`, so it lacks the `[eq-wl]`
banner). The stronger argument is structural: the only code this merge
changed is `atsc_equalizer_long_impl.{cc,h}`, and on the `STVT_EQ=wl` replay
path `atsc_equalizer_long` **is never even instantiated** — nothing merged
can reach the front end's timing loop. The signature (deterministic input,
nondeterministic all-or-nothing outcome, free-run at exactly the sample
rate) points at work-call-boundary state in
`atsc_wl_frontend_impl::general_work`, which has been there since
2026-07-27. `tv_dual.py`, which drives the same block, has never shown it
(0/14 here, plus every historical `lab/wl_v3` run).
**Consequence: `STVT_EQ=wl` needs a lock watchdog before it goes anywhere
user-facing.** Filed here, not fixed — fixing it is DSP surgery, not a merge.

**(b) The volk non-reproducibility law extends to the WL path too.** §1 and
`lab/wl_v3/WORKLOG.md` recorded it for `atsc_equalizer_long` only, and wl_v3
explicitly said "WL is not affected (its taps/window happen to land
stably)". Not so: 15 identical unified WL runs gave three distinct TS md5s
(`af9769a6...` x12, `d8b4f370...` x2, `55eb2faa...`), and the pre-merge
build gave three as well (`af9769a6...`, `55eb2faa...`, `bf5ffb10...`). The
`AF9769A6...` gate hash is the *modal* value, not an invariant. Frames
remain the gate for both equalizers.

## 11.6 A new build gotcha, learned the hard way

`_install.bat` invoked as `cmd /c "_install.bat"` **from Git Bash** does
nothing at all — bash hands cmd an argument it drops, cmd opens an
interactive prompt, returns 0, and the *stale* module stays installed. This
silently produced 15 "unified" measurements that were actually running the
pre-merge DLL. Invoke it from PowerShell with an absolute path:

```powershell
& cmd /c "Z:\src\magic-tv-decoder\gr-atscplus\_install.bat"
```

and **always confirm the installed md5 afterwards** — this is the
`gr_atscplus_build_install_gotcha` wearing a new hat.

## 11.7 REVERT PATH — how to get back to either parent

| want | do |
|---|---|
| **the WL parent build** (`stvt-2.0-wl`) | `git checkout stvt-2.0-wl` then `_rebuild.bat` + `_install.bat` (from PowerShell, verify md5) |
| ...without a rebuild | restore `<scratchpad>/atscplus_wl_installed_backup/` (DLL -> `%RADIOCONDA%\Library\bin\`, module dir -> `%RADIOCONDA%\Lib\site-packages\gnuradio\atscplus\`). NOTE: that backup is the **47d419f** build — WL v3 complete, but no `[eq-wl]` build banner. DLL md5 `b839d3721996c98d...` |
| **the speed-1 parent build** | `git checkout speed-1` then `_rebuild.bat` + `_install.bat` |
| ...without a rebuild | restore `<scratchpad>/atscplus_speed1_installed_backup/` (taken this session, the exact speed-1 binaries). DLL md5 `5d6b5d8199b5acf0...` |
| **the unified build again** | `git checkout stvt-2.0-wl-speed` + rebuild/install. DLL md5 `54359c27a67014c2...`, pyd md5 `2e80d2da48a754ac...` |
| **the uncommitted tv_live hardening alone** | still preserved as `stash@{0}` (applied, never popped) |
| **plain main-universal** | `git checkout main-universal` + rebuild/install |

The runtime revert knobs of §10 all still apply unchanged
(`STVT_PERSIST_RETUNE_CACHE=0`, `STVT_EQ_CACHE_EVERY=1024`,
`STVT_SCAN_FAST=0`, `STVT_PILOT_OFFSET_HZ=-2690000`, recycling off by
default, `STVT_WL_SHRINK` off by default, `STVT_WL_FUSED=0` for the legacy
WL companion path). **Nothing in this merge changes a default** — the
default viewing path is still `STVT_EQ=long` with recycling and shrinkage
off.

## 11.8 State left behind

* Installed gr-atscplus = the **unified** build (md5s above).
* Branch `stvt-2.0-wl-speed` committed locally, **not pushed**. Parents
  `stvt-2.0-wl` and `speed-1` untouched.
* Panel health: the radio panel on **:8643** was up throughout and still
  serves — `/api/band` and `/api/state` both answer with full payloads. It
  does not link gr-atscplus, so the install could not disturb it, and it was
  not restarted. The **TV panel on :8642 was NOT running** at any point in
  this session (nothing listening on the port), so there was nothing to
  restart; start it the usual detached way when you want to drive the
  unified build.
* No SDR access of any kind. `radio_lock` never taken.

---

# 12. THE TWO ROBUSTNESS JOBS (2026-07-29 late) — branch `stvt-2.0-wl-speed`

Both items §11.5 filed as "recorded, not fixed" are now closed. CPU/build only:
a balloon hunt (`wxTuna sonde_rx.py hunt --mhz 405.5`, alive all session) owned
the SDR, so **no radio was touched and `radio_lock` was never taken.** Every
number below is offline replay.

## 12.1 §11.5(a) — the WL lock failure is a MEMORY BUG, and it is fixed

Full log: **`lab/wl_watchdog/WORKLOG.md`**. Short version:

* It would not reproduce on an idle box (0/40 sequential). Under real CPU
  contention (8 parallel harnesses) it did: **1/64**. The trigger is what else
  the machine is doing — because the trigger is heap CONTENTS, not the signal.
* The failing run's own telemetry solved it: `peak=-nan rms=-nan` from the FIRST
  debug print, while the FPLL's `out_rms=4.8` stayed finite for the whole 15 s.
  So the input was clean and the NaN was manufactured inside the block — in the
  one thing it computes before touching a sample: its interpolator taps table.
* **ROOT CAUSE:** GR 3.10.12's `kernel::fir_filter_fff::filter()` rounds the
  input pointer DOWN to volk's alignment and dot-products `ntaps + al` floats
  from there, relying on the leading `al` values being multiplied by ZERO taps.
  `0 * NaN = NaN`. The fused block probed it with a bare `std::vector<float>(8)`
  off the recycled C++ heap (MSVC 16 B aligned, so `al == 4` about half the
  time), so when the 16 bytes before that allocation happened to hold a NaN/Inf
  pattern, **every** entry of the table came out NaN. GR's own callers always
  pass a pointer into a zero-filled GR buffer, which is why nothing upstream
  ever saw it — and why `atsc_sync_soft` (which passes `&in[d_si]`) is immune.
* Then `d_mu` goes NaN, `(int)std::floor(NaN)` is INT_MIN, the `d_incr < 1`
  clamp pins `d_incr` at **1 forever**, and the block advances one input SAMPLE
  per output symbol: 242 150 000 / 832 = **291 044** — the exact number
  §11.5(a) recorded, and the reason it is exactly the SPS ratio.
* **PROVED, not inferred:** `STVT_WL_PROBE_DIAG=1` puts a NaN in the floats
  before a deliberately misaligned probe pointer and reports
  `poison_makes_nan=1` on this build of GR; and `STVT_WL_INJECT_NAN=99`
  reproduces the field signature to within 2 segments (`291042` / `0.00 %` /
  `relocks=0` / `fs=0` / 0-byte TS).
* **FIX:** probe through the middle of a zero-filled buffer with 64 floats of
  padding on both sides. Taps VALUES are unchanged whenever the old code
  happened to work (`0*finite == 0*0 == 0`), and the post-fix WL decode is
  telemetry- and hash-identical to the pre-fix one.

### The watchdog (added anyway — defence in depth)

One integer comparison per SEGMENT, gated on `d_fs_accepted == 0` so a healthy
stream never enters it (first field sync lands at **seg 135**, reproducibly, in
all 104 healthy runs; the window is 1252 segments = 4 fields = 2x that, with
margin). Bounded at `STVT_WL_WD_MAX` (default 4) resets, each logged, then ONE
loud `GIVING UP`; explicit success condition logged as
`RECOVERED ... standing down`. Each reset also RE-PROBES the taps table, so the
historical failure is now self-healing: the injected-fault run decoded
**36 159 168 bytes** instead of 0, losing 1491 segments (~0.14 s).

`[wl_front FINAL]` keeps its exact historical format; the watchdog adds a
separate `[wl_front WD FINAL]` line (plus the new `first_align_seg` /
`first_fs_seg` acquisition-latency numbers nobody had before). Knobs:
`STVT_WL_WD`, `STVT_WL_WD_SEGS`, `STVT_WL_WD_MAX`, and the two test-only ones.

### FAILURE RATE, before vs after

| build | batch | runs | failures |
|---|---|---:|---:|
| pre-fix DLL `54359c27` | sequential, idle | 40 | 0 |
| pre-fix | 8-way parallel, contended | 64 | **1** |
| pre-fix (7/29 earlier session) | mixed load | 39 | **2** |
| **post-fix DLL `cac54ce0`** | 8-way parallel, contended | **64** | **0** |
| **post-fix** | sequential | **40** | **0** |

Pre-fix pooled **3/143 (2.1 %)**, post-fix **0/104**. Fisher p ~ 0.09 — the
statistics alone are suggestive, not conclusive; the load-bearing evidence is the
proved mechanism plus the injected-fault reproduction.

## 12.2 §11.5(b) — the measurement methodology is now honest

**`lab/gate_lib.py`** is the new single home of the law and the valid gate.

| call | does |
|---|---|
| `replay_multi(iq, tag, env, runs=5, ...)` | N identical `tv_replay.py` (or `tv_dual.py`) runs -> `[RunRow]` (md5, ffmpeg-null-sink frames, wall, bytes, parsed extras) |
| `hash_stats(rows)` / `frame_stats(rows)` | modal hash + full counted set + reproducibility flag; frame median / range / spread |
| `gate(rows, expect_md5=<set>, expect_frames=N, frame_tol=2)` | PASSES on **modal-hash-in-the-known-set OR frame-median-within-tolerance**; always renders the whole hash set |
| `control_ok(rows_a, rows_b, frame_tol)` | two run-sets that must be the same decode: modal agreement, else hash-set overlap, else frame-median agreement |
| `render(res)` | the printable evidence block |
| `SingleRunGateError` | **raised for fewer than 3 runs** unless `allow_single_run="<why>"` — this is what makes the invalid test hard to use by accident |
| `KNOWN`, `EXPECT_FRAMES` | growing registry of hashes/frames this tree has legitimately produced (8-char prefixes supported, because that is all some worklogs recorded) |
| `DETERMINISTIC_ENV` / `DETERMINISTIC_REF` | the test-only `VOLK_GENERIC=1` knob (below) |

Wired in:

* **`lab/wl_v3/sweep.py`** — its `control_ok = (lm == base_long_md5)` WAS the
  invalid test, and it was producing false alarms. Now every arm's `long` leg is
  collected (the WL knobs cannot reach it, so the arms ARE repeated runs of one
  control) and judged as a set. Proof it mattered: 3 identical rf34cliff runs
  gave **3 distinct hashes** and 3 rf34clean runs gave 2 — the old test would
  have marked those rows VOID.
* **`lab/speed_build/replay_bench.py`** — new `--gate --runs N --expect-frames
  --expect-md5 (repeatable) --frame-tol`; exits non-zero on failure and REFUSES
  to judge fewer than 3 runs.
* **`lab/speed_build/telemetry_check.py`** — docstring now points at gate_lib and
  states plainly that a single-run md5 is not a gate; it also counts the new
  watchdog lines.

### The hash set is OPEN-ENDED — measured again today

| path / capture | hashes seen | frames |
|---|---|---|
| `long` rf34_ctrl | `F1F867C5` x3, `92D014CD`, `3D8C11EE` in **5** runs — `92D014CD` is NEW today, a 4th value | 403 x5 |
| `wl` rf34_ctrl | `AF9769A6` x39, `BF5FFB10` x1 in **40** runs | 403 |
| `long` rf34+AWGN knee | **3 distinct in 3 runs** | 133 / 132 / 131 |
| `long` rf7_marg | **3 distinct in 3 runs** | 251 / 250 / 251 |
| `long` rf9_marg | **3 distinct in 3 runs** | 111 / 114 / 112 |

**New sub-law: the FRAME COUNT is not reproducible to +-2 on marginal captures
either.** Measured spread on the `long` control leg: clean 0, rf7 1, knee 2,
**rf9 3** (and §11.4-era 5 over 5 runs at the knee). The first run of the new
pooled control used a tolerance of 2 and manufactured a VOID on rf9 — corrected
to a measured default of 4 (`--ctl-frame-tol`).

### Deterministic volk for TESTS: it works

volk 3.2.0 honours `VOLK_GENERIC=1` (and `VOLK_CONFIGPATH`; both appear in the
DLL's strings). Forcing the plain-C kernels removes the alignment-dependent
summation order entirely:

| arm | 3 runs | wall/run vs SIMD |
|---|---|---|
| `STVT_EQ=long` + `VOLK_GENERIC=1` | **3/3 identical** -> `F1F867C5567B33721684F4FBF7C423BB`, the documented MODAL hash | 27.8 s vs 15.5 s (1.8x) |
| `STVT_EQ=wl` + `VOLK_GENERIC=1` | **3/3 identical** -> `D8B4F370...`, a member of the recorded WL set | 25.3 s vs 15.7 s (1.6x) |

So a bit-exact comparison IS available at ~1.7x cost, as an env var with no
global side effect (no `~/.volk/volk_config` write). Recorded as
`gate_lib.DETERMINISTIC_ENV`, **test-only, never a default** — it costs speed,
and a change that only altered the SIMD summation order would slip past a
generic-only test. It is a bisecting tool, not the gate.

## 12.3 Gates re-run after the change

| gate | result |
|---|---|
| **G1 default path** (`STVT_EQ=long`, 5 runs, gate_lib) | **PASS** — frames 403 x5 (median 403, spread 0); modal `F1F867C5` in the known set; converge_field 210 in all 5 = the §1 baseline exactly |
| **G2 WL numbers** (`tv_dual` via wl_v3/sweep, 3 repeats x 4 captures) | rf34 clean **403/403**; knee long 131-133 / **WL 227-229**; rf7 long 250-251 / **WL 255-256**; rf9 long 111-114 / **WL 350-351** — every WL value on or within 1 frame of its recorded band (403 / 226-230 / 257 / 348-350). `imag_frac`, benefit and kappa reproduce to 3 decimals. |
| **G3 front-end telemetry unchanged** (104 post-fix WL runs) | `segs_emitted=194030`, `aligned 99.99 %`, `relocks=33`, `fs accepted=620`, `first_fs_seg=135`, TS 36 335 136 B — **identical in all 104** |

The rf9 WL band widens to **348-351** and rf7's WL sits at 255-256 against a
recorded 257: both inside the frame-noise floor measured above, and both
recorded here rather than quietly rounded.

## 12.4 State left behind

* Installed gr-atscplus = DLL md5 **`CAC54CE0CBE637B742B9ADA915C487D5`**, pyd
  `2E80D2DA48A754ACA276844FEB6AA0F9` (bindings unchanged — the entire change is
  C++ inside the DLL). Previous unified build was `54359C27...`.
* **No default changed.** The watchdog lives only in `atsc_wl_frontend`, which
  the default `STVT_EQ=long` viewing path never instantiates; it is enabled but
  strictly inert unless a stream has failed to frame at all.
* Still owed before any WL user-facing promotion: the **live OsO==0 gate**
  (`drizzle_wave_interferer`). This session had no radio access.
* `lab/wl_watchdog/front_in_{r,i}.f32` (185 MB each) are gitignored — regenerate
  with `dump_front_in.py`.
* The `0 * NaN` hazard is in GNU Radio itself; every GR block that probes
  `fir_filter::filter()` with a heap array is exposed. That is an upstream bug
  report, not a change to make inside this tree.
