#!/bin/bash
# stvt_play.sh — simple stable playback.

set -u
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
BUFFER_SCRIPT=/home/user/pipe_buffer.py
TITLE="STVT Player"
PROGRAM="${STVT_PROGRAM:-3}"

[ -f "$BUFFER_SCRIPT" ] || { echo "missing $BUFFER_SCRIPT" >&2; exit 1; }
which ffmpeg mpv >/dev/null || { echo "ffmpeg/mpv missing" >&2; exit 1; }

while [ ! -f "$LIVE_TS" ] || [ "$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)" -lt 30000000 ]; do
    sleep 2
done
SZ=$(stat -c%s "$LIVE_TS")
echo "[stvt_play] live.ts=$((SZ/1024/1024)) MB"

# Throttled minimal prebuffer (15MB ~ 10s of stream, paced to 1.8MB/s
# so ffmpeg can't process it faster than real-time → no PTS jump → no skip).
PROBE_BYTES=15000000
PROBE_START=$(( SZ > PROBE_BYTES ? SZ - PROBE_BYTES : 0 ))

(
    dd if="$LIVE_TS" bs=1M skip=$((PROBE_START / 1048576)) status=none
    exec tail -c +0 --bytes=+"$SZ" -F "$LIVE_TS"
) | python3 /home/user/pipe_throttle.py 1800 2>/tmp/stvt_throttle.log \
  | python3 "$BUFFER_SCRIPT" --buffer-mb 64 2>/tmp/stvt_buf.log \
  | ffmpeg \
      -hide_banner -loglevel warning \
      -fflags +genpts+igndts \
      -err_detect ignore_err -flags +output_corrupt \
      -ec favor_inter+deblock+guess_mvs \
      -analyzeduration 20000000 -probesize 30000000 \
      -f mpegts -i pipe:0 \
      -map "0:p:$PROGRAM:v" -map "0:p:$PROGRAM:a?" \
      -map_metadata 0 \
      -c:v libx264 -preset ultrafast -tune zerolatency -crf 28 -g 30 \
      -c:a aac -b:a 128k -ar 48000 -ac 2 \
      -f mpegts pipe:1 2>/tmp/stvt_ffmpeg.log \
  | mpv \
      --no-terminal --keep-open=yes \
      --title="$TITLE" \
      --demuxer=lavf --demuxer-lavf-format=mpegts \
      --cache=yes --cache-secs=10 \
      --demuxer-max-bytes=50MiB \
      --cache-pause=no \
      --alang=eng,en \
      --input-ipc-server=/tmp/mpv_stvt.sock \
      - 2>/tmp/stvt_mpv.log
