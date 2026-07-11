---
name: dvr-architecture
description: "How the DVR stack flows from scan to recording — which tool does what, in what order. Lets you orient quickly in stvt_*.py"
metadata: 
  node_type: memory
  type: project
  originSessionId: 5e2ec3a5-5dce-46ba-9541-4f3e423c8039
---

The Software-TV-Tuner DVR is a chain of small CLIs glued together by shared file state:

```
tv_tuner.py --scan       writes  ~/.tv_tuner/scan.json (channels, PSIP, EIT)
                                          |
stvt_capacity.py         reads  scan.json -> "what can we record" report
stvt_epg.py              reads  scan.json -> EPG grid (now+next, --watch)
                                          |
stvt_schedule.py tv      reads  scan.json + queue.json -> INTERACTIVE controller
                         (commands: r/rn/rmux/rrf/watch/show/rm/refresh/q)
                                          |
stvt_schedule.py run     polls  queue.json -> spawns stvt_multirec.py at fire time
                                          |
stvt_multirec.py         starts tv_live.py -> live.ts -> ffmpeg -map per-program
                                           -> N .ts files + (if --keep-mux) one
                                              byte-accurate mux_FULL_*.ts
                                          |
stvt_dvr_play.py         plays  live.ts or any .ts file in VLC
stvt_livewatch.py        starts tv_live + VLC in one shot (live-only, no record)
```

Queue persistence: `~/.tv_tuner/schedule.json`.
Recordings land: `tools/recordings/`.
Daemon log when overnight: `tools/recordings/overnight_daemon.log`.

**Why:** None of this is in any single file's docstring; the architecture lives across multiple tools. Useful when picking up after a break: read this, then dive into the specific tool.

**How to apply:** When asked "how do I X with the DVR", trace the flow above to find the right tool. The `tv` controller is the headline UI for almost everything user-facing. See [[dvr-main-entry]] for the single-command path.

Related: [[scanner-chain-env-gotcha]], [[sdr-single-tuner-rule]], [[dvr-main-entry]].
