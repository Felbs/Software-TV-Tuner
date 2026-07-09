# Overnight Cleanup 2 — 2026-06-05

## Summary

6 commits on `overnight-cleanup-2` (branched off `overnight-cleanup`).
All three assigned tasks complete plus the optional same-mux skip
regression test. Full test suite: **161 passing** (was ~76 before;
+85 new tests across this session).

## Task 1 — Late-fire cascade fix

- `4d0cfd1 scheduler: clamp late-fire duration to remaining window`
- Bug: `fire_recording()` passed the full original `length_sec`
  unconditionally. When the daemon fired late (after a same-mux skip
  releases the SDR mid-window, which IS what happened tonight),
  multirec recorded the full original duration from NOW and ate the
  next slot.
- Fix: extracted `compute_fire_duration_min(entry, now_unix)` ->
  `(duration_min, was_clamped)`. Clamp = `min(original, end_unix - now)`,
  60s floor, 1-min pad. Same formula the spec called for.
- Daemon log on a clamp now shows:

  ```
  [scheduler] late-fire clamp: recording for 7 min instead of original 90 min
              (entry 20260605_0000_rf31_p4__mux__america_reframed end window passes in 360s)
  ```

- Regression test: `tools/tests/test_schedule_fire.py`, **7 tests** —
  on-time fire, lead-time fire (30s early), exact America-ReFramed
  numbers (12:00 start, 90 min, fire at 1:24 -> clamped to 7 min),
  extremely-late floor at 60s, post-end-window defensive case, zero-
  length entry. **All pass.**

## Task 2 — Recording browser

- `f8b1b95 add stvt_recordings.py: interactive browser/manager for recordings/`
- New file: `tools/stvt_recordings.py` (~420 lines)
- Reuses `stvt_inventory`'s filename parser and STUB/SHORT/EMPTY
  classifier so the labels here match the inventory tool one-for-one.
  No drift possible.
- Sessions are clustered by multirec's `_YYYYMMDD_HHMMSS.ts` suffix.
  Each multirec spawn writes its FULL mux file plus per-program demuxes
  with identical timestamps — natural session boundary.
- Commands implemented: `list / s <#> / p <#> / pmux <#> / del <#> /
  dels <#> / clean / dups / q`. The `dups` command shells out to
  `stvt_dedup.py` (offers `--delete-extras` on confirm).
- Wired into the `tv` controller: yes. Added a `recs` command to the
  prompt help and dispatcher in `cmd_tv`. Spawns the browser as a
  subprocess so its `input()` loop owns stdin cleanly.
- Unit tests: `tools/tests/test_recordings.py`, **24 tests**.
  Covers session-stamp regex (FULL files, program files, dashed
  callsigns like `WTTG-DT`, unrecognized names), clustering by stamp,
  Session helper properties, junk detection, and a live tmp-dir
  scan-and-cluster round trip. **All pass.**
- Sanity-checked against the real recordings dir: cluster correctly
  groups 6 files of the in-progress WETA recording into one session
  with RF=31 and `FULL=yes`, and identifies 18 prior sessions.

## Task 3 — Signal strength meter

- `5c0e7e4 add stvt_signal.py: real-time signal-strength meter for antenna aiming`
- New file: `tools/stvt_signal.py` (~330 lines)
- **HAS NOT BEEN LIVE-TESTED.** The SDR is held by the scheduler
  daemon (PID 29160) and its multirec children all night. Bounds
  cannot be checked against actual carriers until ~8:30 AM.
- **Morning smoke test:** once recordings are finished,
  ```
  python tools/stvt_signal.py --rf 36 --bars
  python tools/stvt_signal.py --scan-band
  ```
- Reuses `sdr_sweep.sweep()` + `sdr_sweep._analyze()` so the metrics
  here match what `tv_tuner --scan` would see on the same channel —
  one source of truth for "is there a real ATSC carrier here?".
- Thresholds for STRONG/GOOD/WEAK/NONE labels come from
  `tv_tuner.run_scan`'s strict + weak gates (pilot SNR 30/15,
  sharpness 26.25/8, VSB asym 2.4/-14), so any RF the scanner
  would lock reads STRONG here. All four are flag-tunable.
- Modes:
  - `--rf <N>` — monitor one channel forever, in-place refresh via
    ANSI cursor-up + clear-line if stdout is a TTY (falls back to
    plain repeated prints otherwise). Ctrl+C clean shutdown closes
    the SDR.
  - `--scan-band` — one-shot sniff of every NA ATSC RF (2..36) with
    a one-line pilot-SNR bar per channel. Quick "antenna pointed at
    anything?" check.
- `--bars` toggles the ASCII bar charts (default numeric).
- Static verification:
  - `ast.parse` clean
  - `python tools/stvt_signal.py --help` renders
  - Pure-helper smoke under radioconda Python: label thresholds,
    bar rendering on [0..1] and out-of-range inputs, `_render_block`
    on mock metrics including `-inf` (dropped-buffer case).
- Unit tests: `tools/tests/test_signal.py`, **21 tests**. Pins the
  STRICT/WEAK threshold boundaries (so a future re-tune of the
  scanner numbers must update both places consciously), bar clamp
  behavior, and the 6-line `_render_block` invariant (which the
  TTY in-place refresh depends on — if the line count changes, the
  cursor-up math breaks). **All pass.**

## Bonus — same-mux skip regression test

- `91b9006 scheduler: extract compute_skip_reason and pin same-mux skip rule with tests`
- Extracted the 25-line inline skip-decision in `cmd_run` to
  `compute_skip_reason(entry, active_ids, queue) -> str | None`.
  Behavior unchanged — same walk order, same two message strings.
- Added 5 tests: no actives = None, same-RF active = same-mux message
  with the correct RF, different-RF active = different-mux message
  with "only one SDR" wording, missing-from-queue defensive case,
  first-active-wins for the message.

## Other commits on the branch

- `fadb46b lint: tv_tuner drop bogus f-prefixes on literal strings` —
  uncommitted WIP from the prior session (only 10 inert f-prefix
  removals, no behavior change). Committed early on this branch to
  avoid carrying a dirty tree forward.
- `859c26f tests: add tools/tests/test_psip.py (28 unit tests for atsc_psip)`
  — **THIS WAS NOT CREATED BY THIS AGENT.** It appeared on the branch
  with timestamp 02:03:13 between my Task 1 and Task 2 commits, with
  the same git author identity as the repo owner. Most
  likely a parallel agent or auto-process. The test file is well-
  scoped (covers atsc_psip's pure helpers), the tests pass, and it
  doesn't conflict with anything I added. Worth eyeballing in the
  morning to confirm it's a friendly contribution.

## Tonight's cascade actually fired

Confirming the bug is real and active: at the time of writing, the
queue shows

```
20260605_0000_rf31_p4__mux__america_reframed   status=recording
20260605_0200_rf31_p1__mux__overnight_weta_mux  status=skipped:
    same-mux RF31 record already active ("[MUX] America ReFramed");
    use rmux to cover both
```

The 12:00 AM America-ReFramed entry fired late, ran (without the
clamp) until ~2:55 AM, and starved the 2:00 AM WETA slot exactly as
the task description predicted. The fix in Task 1 will prevent this
in future runs, but it's already too late for THIS night's WETA
mux — the running daemon (PID 29160) predates the patch. The fix
takes effect on the next daemon restart.

## Issues / concerns

- **Live SDR test for `stvt_signal.py` is the open item.** Cannot
  validate the metrics-loop loop or the `--scan-band` sweep without
  the device. Risk surface is small (the SDR plumbing is verbatim
  reused from `sdr_sweep`), but the cursor-up TTY refresh and the
  Ctrl+C SDR-release path have never executed.
- The mystery `859c26f` commit raises a process question — if there's
  a parallel auto-agent writing to this branch, future overnight runs
  should coordinate via the branch name or a lock file. Worth a
  policy decision.
- Stash `stash@{0}` ("prior-agent wip: analyze_diag + tv_player
  comment cleanup") is unrelated dirty work I quarantined to keep my
  commits clean. Review with `git stash show -p stash@{0}` — looks
  like in-progress comment cleanup that the prior agent didn't ship.
- The recordings folder is huge (~10 GB live WETA recording plus
  ~5 GB of historic stubs). Once you've reviewed, run
  `python tools/stvt_recordings.py` and then `clean` + `dups` to
  reclaim space.

## To review

```
git log overnight-cleanup-2 ^overnight-cleanup --oneline
git diff overnight-cleanup..overnight-cleanup-2
python -m unittest discover tools/tests       # 161 tests pass
python tools/stvt_recordings.py --list        # eyeball the session list
python tools/stvt_signal.py --rf 36 --bars    # morning smoke test (live SDR)
```
