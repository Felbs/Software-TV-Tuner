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
