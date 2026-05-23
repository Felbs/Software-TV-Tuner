#!/bin/bash
# stvt_play.sh — robust STVT playback using the pipe_buffer breakthrough.
#
# Replaces the chain's tail|ffmpeg|mpv stdin pipeline (which suffered the
# probe-from-pipe issue → ffmpeg took 30+ min to find seq_header) with:
#
#   cat live.ts + tail -F → pipe_buffer (1GB headroom) → ffmpeg libx264 → mpv
#
# The 1GB in-memory buffer lets ffmpeg's probe drain enough TS quickly to
# find a seq_header (the chain's corrupt mpeg2video produces them sparsely).
#
# Usage:
#   ~/stvt_play.sh                  # plays from live.ts at default path
#
# Requires:
#   - tv_live.py running and producing /home/user/.../live.ts at ~1.5 MB/s
#   - python3 /home/user/pipe_buffer.py (will check)
#   - ffmpeg, mpv

set -u
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
BUFFER_SCRIPT=/home/user/pipe_buffer.py
TITLE="STVT Player"
PROGRAM="${STVT_PROGRAM:-3}"

# Sanity checks
[ -f "$BUFFER_SCRIPT" ] || { echo "missing $BUFFER_SCRIPT" >&2; exit 1; }
which ffmpeg mpv >/dev/null || { echo "ffmpeg/mpv missing" >&2; exit 1; }

# Wait for live.ts to have enough data
while [ ! -f "$LIVE_TS" ] || [ "$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)" -lt 100000000 ]; do
    sleep 2
done

SZ=$(stat -c%s "$LIVE_TS")
echo "[stvt_play] starting (live.ts=$((SZ/1024/1024)) MB, prog=$PROGRAM)"

# The pipeline:
#   1. cat current TS (gives ffmpeg lots to probe from immediately)
#   2. tail -F starting at the position cat ended (follows new growth)
#   3. pipe_buffer.py with 1 GB headroom (drains slow ffmpeg gracefully)
#   4. ffmpeg re-encodes mpeg2→h264 (mpv probes h264 SPS/PPS instantly)
#   5. mpv plays from ffmpeg's stdout

(
    cat "$LIVE_TS"
    exec tail -c +0 --bytes=+"$SZ" -F "$LIVE_TS"
) | python3 "$BUFFER_SCRIPT" --buffer-mb 1024 2>/tmp/stvt_buf.log \
  | ffmpeg \
      -hide_banner -loglevel warning \
      -fflags +genpts+igndts \
      -err_detect ignore_err -flags +output_corrupt \
      -analyzeduration 60000000 -probesize 200000000 \
      -f mpegts -i pipe:0 \
      -map "0:p:$PROGRAM:v" -map "0:p:$PROGRAM:a?" \
      -map_metadata 0 \
      -c:v libx264 -preset ultrafast -tune zerolatency -crf 28 -g 30 \
      -fps_mode cfr -r 30 \
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
      --interpolation=yes --tscale=mitchell \
      --video-sync=display-resample \
      --framedrop=vo \
      - 2>/tmp/stvt_mpv.log
