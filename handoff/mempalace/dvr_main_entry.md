---
name: dvr-main-entry
description: "The headline DVR UI is `python tools/stvt_schedule.py tv` — interactive controller with mux-grouped EPG. Almost everything user-facing flows through here."
metadata: 
  node_type: memory
  type: reference
  originSessionId: 5e2ec3a5-5dce-46ba-9541-4f3e423c8039
---

Single command to start using the DVR:

```powershell
& "$env:USERPROFILE\radioconda\python.exe" "Z:\src\magic-tv-decoder\tools\stvt_schedule.py" tv
```

(Linux/Ubuntu uses `python3 tools/stvt_schedule.py tv` from the repo root.)

The `tv` subcommand renders:
- The full EPG GROUPED BY RF mux (so 4.x and 44.x are visibly together because they're on the same NBC mux)
- Per-mux header with "N channels share this broadcast — record all with: rrf <RF>"
- Pending recordings inline
- A command menu

Commands available at the prompt:
- `r <#>` — record ON-NOW of channel #
- `rn <#>` — record NEXT show on channel #
- `rmux <#>` — record entire mux for ON-NOW's duration (uses channel #'s mux)
- `rrf <RF>` — record the whole RF mux
- `watch <#>` — VLC spy on channel # using active recording's live.ts (ffmpeg→VLC pipe, true live-edge)
- `unwatch` — close watcher
- `show <#>` — next 6 events for channel #
- `rm <id-prefix>` — cancel a pending recording
- `q` — quit

For unattended recording, you need the daemon running too:
```powershell
& "$env:USERPROFILE\radioconda\python.exe" "Z:\src\magic-tv-decoder\tools\stvt_schedule.py" run
```

The controller will warn you "scheduler daemon is NOT running" if your queued entries won't actually fire.

For the live-watch-without-recording case, use `tools/stvt_livewatch.py --rf <RF>` (created by the overnight cleanup agent — wraps tv_live + VLC + Ctrl+C cleanup into one command).
