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

# Pi/ARM player trade (June Pi lineage): half-res mpeg2 decode (lavc lowres=1)
# halves mpv decode CPU and memory bandwidth — on a 4-core Pi the player was
# measurably corrupting the DECODER CHAIN via bandwidth contention (cc-errors
# in live.ts that vanish with the player off). Default ON for aarch64;
# export STVT_PLAY_FULLRES=1 for full resolution. x86 unchanged.
MPV_EXTRA="${STVT_MPV_EXTRA:-}"
if [ "$(uname -m)" = "aarch64" ] && [ -z "${STVT_PLAY_FULLRES:-}" ]; then
  MPV_EXTRA="$MPV_EXTRA --vd-lavc-o=lowres=1"
fi

launch(){
  # last-N-bytes form (tail -c N) — NOT absolute offset (tail -c +OFF), which
  # stalls seeking into a multi-GB growing file.
  local bytes=$(( BACKMB*1000000 ))
  : > "$MPVLOG"
  # player stack at nice +10 (PI_ARCHITECTURE law): the decoder chain owns the CPU
  setsid nice -n 10 bash -c "tail -c $bytes -F '$F' | \
    ffmpeg -hide_banner -loglevel warning -fflags nobuffer+flush_packets \
      -flags low_delay -probesize 3M -analyzeduration 3M -err_detect ignore_err \
      -i - -map 0:p:$PROG -c copy -flush_packets 1 -f mpegts - | \
    mpv - --hwdec=no $MPV_EXTRA --cache=yes --cache-secs=30 --demuxer-max-bytes=200MiB \
      --demuxer-readahead-secs=20 --cache-pause=no --cache-pause-initial=no \
      --title='STVT Live (prog $PROG)' --force-seekable=no \
      --input-ipc-server=/tmp/stvt-mpv.sock \
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

# ---- resolve PROG ------------------------------------------------------
# The word "program" means two different things in this repo, and they met
# here. tv_tuner.py --program is a 1-based SUB-CHANNEL index (1 = the main
# channel). This script historically took the raw PMT program_id, defaulting
# to 3 -- which only ever worked because the station we developed against
# happens to number its programs from 3. Point it at a station whose ids
# start anywhere else and ffmpeg says "No program with ID 1 exists" and mpv
# gets an empty stream: a black window on a PERFECT decode.
# Accept either spelling, and say which one we understood.
resolve_prog(){
  local ids n real
  ids=$(timeout 60 ffprobe -hide_banner -v error -show_entries program=program_id         -of csv=p=0 -i <(tail -c 20000000 "$F") 2>/dev/null         | tr -d " ," | grep -E "^[0-9]+$" | sort -un)
  if [ -z "$ids" ]; then
    log "could not read the program list from live.ts -- using $PROG as given"
    return 0
  fi
  n=$(echo "$ids" | wc -l)
  if echo "$ids" | grep -qx "$PROG"; then
    log "program $PROG is a PMT program_id present in this multiplex"
    return 0
  fi
  if [ "$PROG" -ge 1 ] 2>/dev/null && [ "$PROG" -le "$n" ]; then
    real=$(echo "$ids" | sed -n "${PROG}p")
    log "program $PROG read as a sub-channel index -> PMT program_id $real"
    PROG="$real"
    return 0
  fi
  real=$(echo "$ids" | head -1)
  log "no program $PROG here (available: $(echo $ids | tr "
" " ")) -- using $real"
  PROG="$real"
}
resolve_prog

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

# ---- one supervisor at a time -----------------------------------------
# kill_player() pkills mpv/ffmpeg by NAME, with no notion of whose player it
# is. Two supervisors therefore murder each other's player on sight: every
# start looks like a "rough patch hang", both burn through MAX_RESTARTS, and
# the screen stays black while the decode underneath is perfect. Observed on
# the Pi 8/12 -- 40 relaunches in 90 seconds, none of them the real fault.
#
# Enforced with a LOCK, not by matching process names: one logical instance
# shows up as several matching command lines (the setsid/nohup wrapper AND the
# script), so a pgrep-based guard refuses its own first launch. The lock is
# held by an fd for the life of the process and released automatically on exit.
LOCK="/tmp/stvt_play_hd.lock"
exec 9>"$LOCK" || { echo "cannot open $LOCK"; exit 1; }
if ! flock -n 9; then
  echo "another stvt_play_hd.sh already holds $LOCK."
  echo "Stop it first -- two supervisors kill each other's player and neither wins."
  exit 1
fi

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
