---
name: multirec-analyze-buffer
description: "ffmpeg needs ~20 MB of buffered live.ts to enumerate every program's codec parameters when fanning out N-way. Smaller = \"unspecified size\" errors on `-c copy`."
metadata: 
  node_type: memory
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

For multirec's per-program demux with `-c copy -map 0:p:N`:

* Default ffmpeg `analyzeduration=0` + `probesize=5M` is enough for 1-program TS files
* For an ATSC mux with 7+ programs, those defaults leave many video streams with "Could not find codec parameters... unspecified size" which kills `-c copy` at the header-write step

Fix used in stvt_multirec.py:
1. Pre-buffer live.ts to >= 20 MB before spawning ffmpeg
2. Pass `-analyzeduration 20000000 -probesize 30000000` to ffmpeg
3. Pipe live.ts → ffmpeg stdin (not direct file read) so ffmpeg sees a continuous stream rather than EOF

Without the pre-buffer wait, ffmpeg starts on a too-small live.ts, can't enumerate programs, and produces stub files. Without the analyzeduration bump, even a large live.ts hits the per-program codec-parameter timeout for the streams ffmpeg gets to last.

Pattern is canonical for any new multi-program demux tool. See stvt_multirec.py `build_ffmpeg_cmd()` + the "buffering live.ts for ffmpeg analyze pass" wait in main().

Related: [[dvr-architecture]] (multirec is the foundation of every record path).
