#!/usr/bin/env bash
# stvt_play_vlc_cc.sh — live TV in VLC with native CEA-608 closed-caption
# overlay.
#
# WSLg WARNING: VLC's live AUDIO is broken on WSLg — it stutters ~50%
# regardless of transport/codec/cache (proven by stvt_audio_autotune.py:
# every VLC config dropped out, mpv was flawless). VLC's video + native CC
# overlay work, but the audio does not. On WSL use stvt_watch_cc_osd.sh
# instead (mpv + caption OSD). This script is kept for native-Linux, where
# VLC plays fine and gives the nicest native CC overlay.
#
# Why the ffmpeg stage: VLC's AC-3 S/PDIF passthrough fails on WSLg's
# PulseAudio (RDP sink) and --no-spdif doesn't override it. So we
# pre-transcode the audio to AAC stereo while keeping the video as -c copy
# — that preserves the embedded CEA-608 user_data, so VLC's built-in
# closed-caption decoder still finds it and overlays CC1-CC4 natively.
#
# WSLg display: software GL vout (the GPU vout stalls), windowed (VLC's
# default fullscreen grabs the screen with no easy escape).
#
# Captions: VLC auto-selects CC1 via --sub-track. To switch CC1/CC2
# (English/Spanish) use the VLC menu: Subtitle -> Closed Captions.
#
# Usage:  tools/stvt_play_vlc_cc.sh [program] [backMB]
#   program : MPEG-TS program (default 3 = RF34 1080 HD)
#   backMB  : how far behind the live edge to start (default 20 MB ≈ ~8s)
# Close:   press 'q' in the VLC window, or:  pkill -x vlc
set -u
PROG="${1:-3}"
BACKMB="${2:-20}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
F="$HERE/data/tv_live/live.ts"
[ -f "$F" ] || { echo "live.ts not found at $F — start the chain first."; exit 1; }

export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export LIBGL_ALWAYS_SOFTWARE=1     # software GL (WSLg GPU vout stalls)
unset WAYLAND_DISPLAY              # force the Xwayland/X11 path

# tail the live edge -> transcode audio to AAC stereo (video+CC copied) ->
# VLC reads the pipe. Closing VLC breaks the pipe and cleans up the rest.
# Audio -> MP2 stereo: AAC-in-mpegts over a pipe mis-frames into static in
# VLC; MP2 is the robust broadcast audio for MPEG-TS. Map only the primary
# audio (a:0) and downmix to clean stereo. Bigger VLC cache rides out pipe
# jitter.
tail -c "$((BACKMB * 1000000))" -F "$F" \
  | ffmpeg -hide_banner -loglevel error \
      -i pipe:0 -map 0:p:"$PROG":v -map 0:p:"$PROG":a:0 \
      -c:v copy -c:a mp2 -ac 2 -b:a 256k -ar 48000 \
      -f mpegts pipe:1 \
  | vlc fd://0 \
      --no-fullscreen --vout=gl --avcodec-hw=none --no-video-on-top \
      --sub-track=1 --network-caching=8000 --clock-jitter=0 --clock-synchro=0 \
      --video-title="STVT Live + CC  (Subtitle menu = Closed Captions)"
