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

