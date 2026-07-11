---
name: vlc-program-filter-unreliable
description: "VLC's `--programs=N` filter does NOT work reliably on a growing TS file. Use ffmpeg-side demux instead."
metadata: 
  node_type: memory
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

When you want VLC to play ONE program out of a multi-program MPEG-TS, the obvious approach is `vlc --programs=N file.ts`. This is BROKEN for growing TS files (like a live.ts being written by tv_live in parallel):

* VLC opens the file at byte 0
* Ignores the `--programs` filter
* Plays whatever is FIRST in the PAT regardless of N
* On reopen for a different program, replays from byte 0 again

Symptoms when this fails: the user sees "the same video over and over from the beginning" instead of different subchannels at the live edge.

**Working pattern** (used in stvt_schedule.py `watch <#>`):
1. Spawn `ffmpeg -f mpegts -i pipe:0 -map 0:p:N -c copy -f mpegts pipe:1` (stdin/stdout pipes)
2. Spawn `vlc -` reading from ffmpeg's stdout
3. Python tail thread reads live.ts from CURRENT END (so playback is live, not from start) and pipes bytes into ffmpeg's stdin
4. ffmpeg does the program isolation correctly; VLC sees a clean single-program stream and just plays it

This same architecture works for any tool that needs to watch a specific program from a growing/multi-program TS source. The tail-start-at-end gives live-edge playback automatically.

**Why:** It took a full debug cycle to find this. The obvious `vlc --programs=N` works for FINITE recorded TS files (after the file stops growing) but fails for live capture. ffmpeg's program filter is the only reliable one.

Related: [[dvr-architecture]] (the `watch` command in the `tv` controller uses this).
