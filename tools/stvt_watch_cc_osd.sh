#!/usr/bin/env bash
# stvt_watch_cc_osd.sh — smooth live TV in mpv WITH CEA-608 captions
# overlaid on the picture (via mpv's on-screen text + IPC).
#
# Why this and not VLC: VLC's live audio is unusable on WSLg (stutters
# ~50%, proven by stvt_audio_autotune.py); mpv plays flawlessly but can't
# render embedded 608 itself. So we decode the captions with atsc_cc.py and
# push them into mpv's OSD via stvt_cc_osd.py.
#
# Caption timing: mpv plays a few seconds behind the live edge, so captions
# are delayed to match. Tune with STVT_CC_DELAY (seconds) if they lead/lag.
#
# Usage:  tools/stvt_watch_cc_osd.sh [program] [cc_channel] [backMB]
#   program    : MPEG-TS program (default 3 = RF34 1080 HD)
#   cc_channel : 1 = CC1 English (default), 2 = CC2 Spanish
#   backMB     : start this far behind the live edge (default 20)
# Env: STVT_CC_DELAY (default 5.0)
# Close: press 'q' in mpv (everything else cleans up automatically).
set -u
PROG="${1:-3}"
CCCH="${2:-1}"
BACKMB="${3:-20}"
DELAY="${STVT_CC_DELAY:-5.0}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
F="$HERE/data/tv_live/live.ts"
SOCK="/tmp/mpv-cc.sock"
CCFEED="/tmp/stvt_cc_feed.ts"
[ -f "$F" ] || { echo "live.ts not found at $F — start the chain first."; exit 1; }

export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PULSE_SERVER="${PULSE_SERVER:-unix:/mnt/wslg/PulseServer}"

# WSLg's PulseAudio suspends the sink whenever it goes briefly idle (a pause
# in the show), and mpv's audio output never recovers -> sound dies after a
# while. Unload that module so the sink stays alive for the whole session.
pactl unload-module module-suspend-on-idle 2>/dev/null || true

rm -f "$SOCK" "$CCFEED"

# 1) single-program feed for the 608 decoder (so it captions the right channel)
setsid bash -c "tail -s 0.1 -c $((BACKMB * 1000000)) -F '$F' \
  | ffmpeg -hide_banner -loglevel error -i pipe:0 -map 0:p:$PROG -c copy \
      -f mpegts '$CCFEED'" >/dev/null 2>&1 < /dev/null &
FEED=$!

# 2) mpv: smooth program playback (wlshm for WSLg) + IPC socket for captions
setsid bash -c "tail -c $((BACKMB * 1000000)) -F '$F' \
  | ffmpeg -hide_banner -loglevel warning -err_detect ignore_err -i - \
      -map 0:p:$PROG -c copy -flush_packets 1 -f mpegts - \
  | mpv - --input-ipc-server='$SOCK' --vo=wlshm --hwdec=no \
      --cache=yes --cache-secs=30 --demuxer-readahead-secs=20 \
      --cache-pause=no --cache-pause-initial=no --force-seekable=no \
      --osd-align-x=center --osd-align-y=bottom --osd-font-size=42 \
      --osd-border-size=2 --title='STVT Live + CC (mpv OSD)' \
      --msg-level=all=status" >/tmp/stvt_mpv_cc.log 2>&1 < /dev/null &
MPV=$!

cleanup() { kill -- -"$FEED" -"$MPV" 2>/dev/null; rm -f "$SOCK" "$CCFEED"; }
trap cleanup EXIT INT TERM

# wait for the IPC socket + a bit of caption feed
for i in $(seq 1 50); do
  [ -S "$SOCK" ] && [ "$(stat -c%s "$CCFEED" 2>/dev/null || echo 0)" -gt 2000000 ] && break
  sleep 0.5
done
[ -S "$SOCK" ] || { echo "mpv IPC socket never appeared (check /tmp/stvt_mpv_cc.log)"; exit 1; }

# 3) caption bridge in the foreground; it exits when mpv closes
python3 "$HERE/stvt_cc_osd.py" --feed "$CCFEED" --channel "$CCCH" \
  --sock "$SOCK" --delay "$DELAY"
