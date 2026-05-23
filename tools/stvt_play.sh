#!/bin/bash
# stvt_play.sh — STVT playback pipeline.
#
# Pipeline:
#   tail-F live.ts → pipe_throttle (rate-limit to chain rate) →
#   pipe_buffer (1 GB headroom) → ffmpeg (transcode mpeg2→h264) →
#   mpv (2-min cache)
#
# Why each layer:
#   - tail -F starts at current EOF so we never read historical bytes
#     (prevents catch-up forward-skip).
#   - pipe_throttle (1.8 MB/s) caps the prebuffer read rate at chain
#     rate so even if tail emits a burst, ffmpeg gets it slowly enough
#     that source PTS tracks wall-clock.
#   - pipe_buffer accumulates ~1 GB so ffmpeg's probe can scan widely
#     without blocking on slow input.
#   - ffmpeg libx264 transcode: the chain produces mpeg2 video with
#     intermittent bit errors; ffmpeg decodes what it can, re-encodes
#     clean h264 with proper SPS/PPS for mpv to probe instantly.
#   - mpv with cache-secs=120 absorbs the brief output dips when
#     ffmpeg's mpeg2 decoder cycles through error concealment.

set -u
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
BUFFER_SCRIPT=/home/user/pipe_buffer.py
THROTTLE_SCRIPT=/home/user/pipe_throttle.py
TITLE="STVT Player"
PROGRAM="${STVT_PROGRAM:-3}"

[ -f "$BUFFER_SCRIPT" ]   || { echo "missing $BUFFER_SCRIPT"   >&2; exit 1; }
[ -f "$THROTTLE_SCRIPT" ] || { echo "missing $THROTTLE_SCRIPT" >&2; exit 1; }
which ffmpeg mpv >/dev/null || { echo "ffmpeg/mpv missing" >&2; exit 1; }

# Wait for chain to start producing
while [ ! -f "$LIVE_TS" ] || [ "$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)" -lt 50000000 ]; do
    sleep 2
done
SZ=$(stat -c%s "$LIVE_TS")
echo "[stvt_play] live.ts=$((SZ/1024/1024)) MB"

tail -c +0 --bytes=+"$SZ" -F "$LIVE_TS" \
  | python3 "$THROTTLE_SCRIPT" 1800 2>/tmp/stvt_throttle.log \
  | python3 "$BUFFER_SCRIPT" --buffer-mb 256 2>/tmp/stvt_buf.log \
  | ffmpeg \
      -hide_banner -loglevel warning \
      -fflags +genpts+igndts \
      -err_detect ignore_err -flags +output_corrupt \
      -ec favor_inter+deblock+guess_mvs \
      -analyzeduration 30000000 -probesize 50000000 \
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
      --cache=yes --cache-secs=120 \
      --demuxer-max-bytes=500MiB --demuxer-max-back-bytes=100MiB \
      --cache-pause=no \
      --alang=eng,en \
      --input-ipc-server=/tmp/mpv_stvt.sock \
      - 2>/tmp/stvt_mpv.log
