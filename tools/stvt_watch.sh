#!/usr/bin/env bash
# Watch the live STVT stream in mpv (WSLg). Starts ~25s behind the live write
# head so mpv keeps a buffer cushion and rides through chain hiccups.
#
# Usage: tools/stvt_watch.sh [vid]
#   vid selects the mpv video track (default 2). NOTE mpv re-numbers --vid each
#   start on a multi-program TS, so the HD 1080 track is not always the same N —
#   just press `_` in the mpv window to cycle video tracks to the 1080 one.
#
# WSLg gotchas baked in below (learned the hard way):
#   --vo=wlshm     WSLg's GPU VO (zink/vdpau/EGL) stalls; software wlshm is smooth
#   --hwdec=no     software decode is reliable under WSLg
#   --cache-pause=no  never freeze on a brief underrun — plow through (live TV)
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
F="$HERE/data/tv_live/live.ts"
VID="${1:-2}"
SZ=$(stat -c%s "$F"); OFF=$((SZ-50000000)); [ "$OFF" -lt 1 ] && OFF=1
exec tail -c +"$OFF" -f "$F" | mpv --vid="$VID" \
  --vo=wlshm --hwdec=no \
  --title="STVT Live RF34 (HD)" --force-seekable=no \
  --cache=yes --cache-secs=60 --demuxer-max-bytes=300MiB \
  --demuxer-readahead-secs=40 \
  --cache-pause=no --cache-pause-initial=no \
  --msg-level=all=status -
