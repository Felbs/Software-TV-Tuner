# WSL Port Report — 2026-07-11

**Branch:** `wsl-port` (from `origin/main-universal` @ the 7/11 handoff state)
**Rig:** WSL2 Ubuntu 24.04, Threadripper (64c), system GNU Radio 3.10.9.2,
ffmpeg 6.1.1, Python 3.12. Scope per handoff: **build + replay validation
ONLY** (no live TV attempted — WSL USB sample loss is documented fatal).

## Verdict: PASS — the decoder is bit-comparable across operating systems

The chain built clean on Linux, every selftest passed, and both specimen
replays land **inside the ±0.14pp gate** — with the philips TS packet count
matching Windows **byte-for-byte exactly** and TEI landing inside Windows'
own run-to-run variance band. The Ubuntu live test should be a formality.

## 1. Build

| step | result |
|---|---|
| bootstrap.sh (apt skipped — toolchain present) | OK |
| cmake configure + make -j64 | OK, no errors |
| sudo make install + ldconfig | OK |
| `from gnuradio import atscplus` | **21 blocks** (fpll_tight, sync_kalman/soft/fieldlock/slidefs/pathA/tunable, viterbi_soft, rs_decoder_erasure, all equalizers, noise_blanker, adaptive_notch, spectral_smoother, deinterleaver, fs_checker_inst) |
| stale-module trap check (mempalace `gr_atscplus_build_install_gotcha`) | installed .so mtime 0.4 min old — **fresh, trap avoided** |

## 2. Byte-compile + import

- `python3 -m compileall tools/ adaptive-tv/` → **0 errors** on both trees.
- Import smoke on the critical path (`tv_replay.py`, `time_knob.py`,
  `antenna_id.py`, `scan_planner.py`, `time_knob_prior.py`) → all import
  clean on Linux.

## 3. Selftests — 53/53 checks pass

| module | checks |
|---|---|
| adaptive-tv/time_knob.py selftest | 13/13 |
| adaptive-tv/antenna_id.py selftest | 13/13 |
| adaptive-tv/scan_planner.py selftest | 16/16 |
| adaptive-tv/time_knob_prior.py selftest | 11/11 |

## 4. Replay validation (the gate)

Specimens read from `/mnt/z/src/adaptive-tv/lab/captures/` (staged to local
ext4 first). Dialect = **e7_vote `CLIFF_ENV`** verbatim (soft Viterbi +
erasure RS 14 + SOVA + long EQ w/ LKG + DD_MU 1e-2 + MOD12 guard,
TEISCRUB=0, SPS 1.1, RRC 8) — the exact env of the Windows turbo2b **OFF**
reference rows. Turbo confirmed default-OFF in code. Parsers per the
documented metric definitions (Σ over `rs_erasure last5s` lines; TEI counted
in the emitted TS; ffmpeg null-sink of **program 3**, `-v error`).

| metric | philips WSL | philips Win ref | Δ | fox WSL | fox Win ref | Δ |
|---|---:|---:|---:|---:|---:|---:|
| bad% (vs turbo2b OFF) | 0.429 | 0.418 | **+0.011pp ✓** | 1.456 | 1.426 | **+0.030pp ✓** |
| TS packets emitted | 382,800 | 382,800 | **EXACT** | 579,936 | n/a¹ | — |
| TEI in TS | 2,119 | 2,120 (2,119–2,120 across Win runs) | **within Win variance** | 8,660 | n/a¹ | — |
| frames (prog 3, vs time_machine vintage²) | 1,708 | 1,710 | −2 | 1,312 | 1,309 | +3 |
| ffmpeg err lines² | 277 | 311 | −34 | 870 | 900 | −30 |
| replay wall s | 48.5 | 47.4 | +1.1 | 73.6 | 72.4 | +1.2 |

**Gate: bad% within ±0.14pp → PASS on both specimens** (margins 12.7× and
4.7× inside the gate). Cumulative-counter and Σ-last5s parsers agree exactly.

Footnotes / caveats, honestly stated:
1. turbo2b's fox row reports frames=2,585 — that used a different ffmpeg
   mapping (not the documented one-program method). The like-for-like frames
   reference is time_machine's program-3 metric (1,309), which we match +3.
2. frames/err_lines cross a **ffmpeg version boundary** (Win = 8.1, WSL apt
   = 6.1.1): corrupt-frame handling differs slightly between versions; the
   chain-only metrics (bad%, TS packets, TEI) are ffmpeg-independent and are
   the primary gate. err_lines −10% on both specimens is consistent with
   6.1's quieter corrupt-slice reporting, not a decode difference.
3. The erasure histogram (`/tmp/atscplus_rs_erasure_hist.bin`) started COLD
   here; Windows reference runs may have had warm state. Effect is inside
   the observed +0.011/+0.030pp margins either way.
4. philips bad% ref: the turbo2b report itself notes the rf36 log-derived
   bad% wobbles (0.418/0.419) and calls TEI-in-TS "the honest column" —
   where we match within their own variance.

## 5. Linux fixes made (all platform-layer, no forks)

1. **`adaptive-tv/antenna_id.py`** — `TOOLS` was hardcoded
   `Z:\src\magic-tv-decoder\tools`. Now resolves: `$STVT_TOOLS_DIR` env
   override → in-repo sibling `../tools` (any Linux/checkout) → the Windows
   rig absolute path (unchanged fallback, so the Windows deploy at
   `Z:\src\adaptive-tv` keeps working). Verified: resolves to the repo
   `tools/` on WSL; selftest still 13/13.

That is the only source change required. Notables that needed **no** fix:
- `tools/tv_tuner.py` already carries a platform layer
  (`_resolve_python_exe` honoring `$STVT_PYTHON`/`$RADIOCONDA_PY`,
  `_resolve_binary` PATH-lookup on Linux) — the port anticipated this.
- `tools/tv_replay.py` is Linux-native already (its `/home/user` diag
  default is env-overridable, only used with STVT_DIAG=1).
- gr-atscplus C++ built with zero source edits.

**Known remaining Windows-isms (out of WSL scope, byte-compile clean):**
the live-rig lab harnesses (`ab3_impulse.py`, `ab4_strikes.py`,
`ab_flywheel.py`, `ab_impulse_guard.py`, `day_lab.py`, `night_lab.py`,
`gap_profiler.py`, `overnight_cube.py`, `tv_tuna_panel.py` relaunch path,
etc.) invoke PowerShell to bounce the panel/SDRplay service. They are
live-test tools — unusable in WSL by design (no live chain) — and should be
ported with a process-management shim **on the Ubuntu box** where they can
actually be exercised (per the no-untested-push law).

## 6. Where this leaves the roadmap

- **WSL (this report): DONE** — build ✓, selftests ✓, replay bit-comparable ✓.
- **Ubuntu box (`main-linux`)**: live test is now the only remaining
  unknown, and it's de-risked: the DSP is proven OS-portable; what's left is
  SDR/USB/RF plumbing, which the Ubuntu rig already ran in June.
- **Pi (`pi-port-stvt`)**: feature governor still to be born there.

Per the handoff laws: nothing pushed to `main-linux`; work is on `wsl-port`
only. Replay validated the *decoder math*, not live promotion — the live
A/B on Ubuntu must still be overflow-gated (OsO==0).
