#!/usr/bin/env bash
# stvt_play_hd.sh — supervised HD player for the live STVT stream.
#
# An ATSC broadcast is a MULTIPLEX of several programs (often 1-2 HD 1080 +
# several SD subchannels). If you point mpv at the raw multi-program live.ts it
# picks a track at random and may show "Invalid frame dimensions 0x0" garbage —
# even though the decode is perfect. This script stream-copies ONE program with
# ffmpeg and feeds it to mpv with a buffer cushion, so playback is clean and
# rides through the chain's occasional sample-overflow (OsO) hiccups.
#
# It also self-heals: if the cushion drains or mpv dies, it relaunches the
# player (NEVER the decoder chain) with a hard cap on restarts so it can never
# runaway-respawn.
#
# Usage:  tools/stvt_play_hd.sh [program] [tailMB]
#   program : MPEG-TS program number to play (default 3). Run
#             `ffprobe -show_programs tools/data/tv_live/live.ts` to list them;
#             pick one whose video stream is 1920x1080.
#   tailMB  : how many MB from the live edge to start (default 25 ≈ ~20s cushion).
set -u
PROG="${1:-3}"
BACKMB="${2:-25}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
F="$HERE/data/tv_live/live.ts"
MPVLOG="/tmp/stvt_mpv.log"
SUPLOG="/tmp/stvt_play_hd.sup.log"
MAX_RESTARTS=40
COOLDOWN=3
LOWCACHE_LIMIT=4          # ~60s of sustained low cache before a refresh

# Auto-detect the graphical session (works under a normal desktop login).
export DISPLAY="${DISPLAY:-:0}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

log(){ echo "$(printf '%(%H:%M:%S)T' -1) $*" >> "$SUPLOG"; }

launch(){
  # last-N-bytes form (tail -c N) — NOT absolute offset (tail -c +OFF), which
  # stalls seeking into a multi-GB growing file.
  local bytes=$(( BACKMB*1000000 ))
  : > "$MPVLOG"
  # -f mpegts on the INPUT is essential: tail -c starts mid-packet, so ffmpeg's
  # format auto-probe reads a partial packet and dies ("Invalid data found"),
  # which looks like a rough-patch hang and triggers an endless relaunch storm
  # (observed: 2300+ relaunches, mpv mostly down, while the chain was perfect).
  # Forcing mpegts skips the probe and lets the demuxer resync to the 188 grid.
  #
  # Audio: many ATSC programs carry a Spanish SAP track alongside English. A bare
  # -map 0:p:N lets ffmpeg emit the audio in absolute-index order, which can put
  # Spanish first and varies between relaunches. Mapping by PROGRAM-RELATIVE
  # position (0,1,2,3...) instead forces the broadcaster's PMT order — English is
  # listed first, Spanish second — so mpv reliably shows 1/2=English, 2/2=Spanish.
  # The trailing ? makes the higher slots optional (programs with fewer tracks
  # don't error). --alang is the belt-and-suspenders default; press # to switch
  # live; STVT_ALANG=spa to start on Spanish.
  # Pi 5: the whole player stack runs nice +10 — the chain saturates ~91% of
  # the 4 cores, and at equal priority the player steals just enough to cause
  # SDR overflows (~5/min measured). Niced, the chain wins every contention.
  # mpv flags after --cache-pause-initial are the Pi tune from stvt_dvr watch:
  # software MPEG-2 decode is fine, but mpv's default high-quality VO path
  # can't present 1080 full-rate on the Pi GPU — fast profile + cheap scalers
  # + NO deinterlace (1080i sw-deint is too heavy) = full-rate, in sync.
  # Audio is ALSA-direct (NOT pipewire): the Pi 5 vc4-hdmi PCM only takes
  # IEC958_SUBFRAME_LE, which pipewire's pro-audio path can't negotiate (sink
  # shows up as "Dummy Output"). ALSA's hdmi: device does the IEC958 wrapping.
  # Override the port with STVT_AUDIO_DEV (HDMI1 = alsa/hdmi:CARD=vc4hdmi1).
  # --autofit-larger caps the window below the panel size (1360x768 TV
  # measured; a raw 1080 window overflowed it) while leaving the desktop
  # toolbar reachable. Press f in the window for fullscreen TV mode.
  setsid nice -n "${STVT_PLAYER_NICE:-10}" bash -c "tail -c $bytes -F '$F' | \
    ffmpeg -hide_banner -loglevel warning -fflags nobuffer+flush_packets \
      -flags low_delay -probesize 3M -analyzeduration 3M -err_detect ignore_err \
      -f mpegts -i - -map 0:p:$PROG:0 -map 0:p:$PROG:1? -map 0:p:$PROG:2? -map 0:p:$PROG:3? \
      -c copy -flush_packets 1 -f mpegts - | \
    mpv - --vo=${STVT_MPV_VO:-gpu} --hwdec=no --cache=yes --cache-secs=30 --demuxer-max-bytes=200MiB \
      --demuxer-readahead-secs=20 --cache-pause=no --cache-pause-initial=no \
      --profile=fast --scale=bilinear --cscale=bilinear --dither=no --deinterlace=no \
      --alang=${STVT_ALANG:-eng,en} \
      --ao=alsa --audio-device='${STVT_AUDIO_DEV:-alsa/hdmi:CARD=vc4hdmi0,DEV=0}' \
      --autofit-larger='${STVT_FIT:-85%x85%}' --geometry=50%:50% \
      --title='STVT Live (prog $PROG)' --force-seekable=no \
      --msg-level=all=status" >> "$MPVLOG" 2>&1 < /dev/null &
  log "launched player prog=$PROG tail=${BACKMB}MB"
}

kill_player(){
  for p in $(pgrep -x mpv); do kill "$p" 2>/dev/null; done
  for p in $(pgrep -x ffmpeg); do kill "$p" 2>/dev/null; done
  for p in $(pgrep -f "tail -c .*live.ts"); do kill "$p" 2>/dev/null; done
  sleep 1
  for p in $(pgrep -x mpv) $(pgrep -x ffmpeg); do kill -9 "$p" 2>/dev/null; done
}

# Current playback position (1st AV number) in whole seconds, "" if none yet.
av_pos(){ grep -oE '^AV: [0-9:]+' "$MPVLOG" | tail -1 | grep -oE '[0-9:]+$' \
          | awk -F: '{n=NF; print (n==3?$1*3600+$2*60+$3:$1*60+$2)}'; }

# Returns 0 if mpv started AND its position advances (real playback, not a
# rough-patch hang that stalls at "Reading from stdin..." or freezes on the
# first frame). Waits up to ~20s.
started_ok(){
  local n=0 p0 p1
  while [ "$n" -lt 12 ]; do
    p0=$(av_pos); [ -n "$p0" ] && break
    pgrep -x mpv >/dev/null || return 1
    sleep 1; n=$((n+1))
  done
  [ -n "$p0" ] || return 1
  sleep 4; p1=$(av_pos)
  [ -n "$p1" ] && [ "$p1" -gt "$p0" ] && return 0
  return 1
}

[ -f "$F" ] || { echo "live.ts not found at $F — start the chain first (tools/tv_live.py)"; exit 1; }

# Kill + relaunch + verify start, retrying rough-patch hangs up to the cap.
# Returns non-zero only when the restart cap is exhausted.
relaunch(){
  while :; do
    restarts=$((restarts+1))
    [ "$restarts" -gt "$MAX_RESTARTS" ] && { log "MAX_RESTARTS hit — giving up"; return 1; }
    log "relaunch #$restarts ($1)"
    kill_player; sleep "$COOLDOWN"; launch
    started_ok && { log "playing"; return 0; }
    log "  start hung (rough patch) — retrying"
  done
}

restarts=0
relaunch "initial" || exit 1
last=$(av_pos); stuck=0
while true; do
  sleep 10
  if ! pgrep -f '[t]v_live.py' >/dev/null; then log "chain DOWN — supervisor exiting"; exit 0; fi
  if ! pgrep -x mpv >/dev/null; then relaunch "mpv died" || exit 1; last=$(av_pos); stuck=0; continue; fi
  # True freeze = playback POSITION stops advancing (cache=0 while still
  # advancing at the live edge is fine — not a freeze).
  pos=$(av_pos)
  if [ -n "$pos" ] && [ -n "$last" ] && [ "$pos" -le "$last" ]; then
    stuck=$((stuck+1))
  else
    stuck=0
  fi
  last=$pos
  if [ "$stuck" -ge 3 ]; then   # ~30s with no progress
    relaunch "frozen at ${pos}s" || exit 1; last=$(av_pos); stuck=0
  fi
done
