---
name: wsl-handoff-briefing
description: "Starting brief for the WSL Claude session — what's on `windows` branch waiting to be ported to `wsl-port-stvt-v2`, what's safe to port, what needs adaptation."
metadata: 
  node_type: memory
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

User switched from a Windows Claude session to a WSL Claude session on 2026-06-05 morning to port the new DVR work from Windows to the WSL branch. This memory exists so the WSL Claude can pick up cleanly.

## What's on the `windows` branch (origin, head = 1fb8a45) that needs porting

A full DVR stack built and validated over one long Windows session:
* Scanner upgrades — `env_with_sdrplay()` now injects CHAIN_DEFAULTS so scans use same chain settings as play path (recovered 2+ DC channels)
* `stvt_capacity.py` — DVR capacity report (what can record simultaneously)
* `stvt_epg.py` — EPG grid with mux-grouped sections
* `stvt_multirec.py` — record every program in a mux simultaneously, `--keep-mux` for whole-mux passthrough file (raw-tee, preserves multi-program PAT)
* `stvt_schedule.py` — DVR scheduler with `tv` interactive controller (rrf, watch, recs); daemon with cascade-clamp on late fires + defer-on-cross-mux for back-to-back recordings
* `stvt_recordings.py` — interactive browser/manager (list/play/del/clean/dups)
* `stvt_signal.py` — real-time signal-strength meter for antenna aiming (with SoapySDR_setLogLevel(FATAL) to suppress driver-load noise)
* `stvt_livewatch.py` — one-command tv_live + VLC
* `stvt_inventory.py` — read-only catalogue of recordings/
* `stvt_dedup.py` — find duplicate .ts files via size + sample-hash
* `tools/tests/` — 165 unit tests covering all the above
* `.gitignore` — runtime log + live.ts patterns

## What needs adaptation for WSL

* `stvt_hotkey_listener.py` — uses Win32 ctypes for global hotkeys. WSL/Linux equivalent would need X11 or evdev approach. Probably skip the port unless user wants Linux hotkeys.
* `os.add_dll_directory` + `C:\Program Files\SDRplay\API\x64` PATH injection in `stvt_signal.py`, `tv_live.py` — Linux has its own SDRplay API install (`/usr/local/lib`), the Win-specific code is already inside `sys.platform == "win32"` guards.
* Hardcoded `C:\Program Files\VideoLAN\VLC\vlc.exe` paths in `stvt_dvr_play.py`, `stvt_play_hd.py`, `stvt_recordings.py`, `stvt_livewatch.py`, `stvt_schedule.py` (watch feature) — Linux uses `/usr/bin/vlc`. Use `shutil.which("vlc")` for portability.
* `subprocess.creationflags=0x08000000` (CREATE_NO_WINDOW) — Win-only, ignored on Linux but cosmetic; leave the guards in place.
* `wmic`/`taskkill`/`Get-CimInstance` calls in `stvt_channel_step.py` and elsewhere — already have `sys.platform == "win32"` branches that fall back to `pkill`/`pgrep` on Linux.
* `subprocess.send_signal(CTRL_BREAK_EVENT)` — Windows-only; Linux uses SIGINT/SIGTERM.

## User's explicit constraints

* **DO NOT push to `wsl-port-stvt-v2` or `linux-port-stvt-v3` without per-branch live test first** — per [[no-push-linux-without-test]]. User wants to test in the target environment before publishing.
* The Ubuntu rig's branch is `linux-port-stvt-v3`; pushes there are subject to [[windows-relay-trick]] (no creds on Ubuntu).
* Be honest about what got walked back vs shipped. Don't claim things work without verification.

## Local worktree state at handoff

* `Z:/src/magic-tv-decoder` = primary worktree on `windows` branch (head 1fb8a45)
* `Z:/src/magic-tv-decoder-linux` = secondary worktree on stale local `linux-port-stvt-v2` (origin renamed to `wsl-port-stvt-v2`). First step in WSL: cd to whatever path WSL sees this as (likely `/mnt/z/src/magic-tv-decoder-linux`), then `git branch -m linux-port-stvt-v2 wsl-port-stvt-v2 && git fetch && git branch --set-upstream-to=origin/wsl-port-stvt-v2`.
* Local-only branches `overnight-cleanup` and `overnight-cleanup-2` exist but their content is already merged into `windows`. Safe to delete (`git branch -D`) once user confirms.
* Scheduler daemon PID 8484 may still be running on Windows. Not relevant to WSL; leave it.

Related: [[dvr-architecture]], [[scanner-chain-env-gotcha]], [[sdr-single-tuner-rule]], [[branch-layout]], [[no-push-linux-without-test]], [[ubuntu-rig-workflow]], [[windows-relay-trick]].
