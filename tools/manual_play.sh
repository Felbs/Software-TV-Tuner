#!/bin/bash
# manual_play.sh — test if libx264 re-encode → mpv works with buffered input
# Strategy: cat current TS + tail -F → ffmpeg → mpv. The cat upfront gives
# ffmpeg enough data to probe quickly; tail -F keeps the stream flowing.

LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts

# Wait for live.ts to be substantial
while [ ! -f "$LIVE_TS" ] || [ "$(stat -c%s "$LIVE_TS")" -lt 100000000 ]; do
    sleep 2
done

echo "$(date '+%H:%M:%S') starting pipeline (live.ts is $(stat -c%s $LIVE_TS) bytes)"

(
    # Dump current file contents (so ffmpeg has lots to probe from)
    cat "$LIVE_TS"
    # Then follow growth
    exec tail -c +0 --bytes=+$(stat -c%s "$LIVE_TS") -F "$LIVE_TS"
) | ffmpeg \
    -hide_banner -loglevel warning \
    -fflags +genpts+igndts \
    -err_detect ignore_err -flags +output_corrupt \
    -analyzeduration 60000000 -probesize 200000000 \
    -f mpegts -i pipe:0 \
    -map "0:p:3:v" -map "0:p:3:a?" \
    -c:v libx264 -preset ultrafast -tune zerolatency -crf 28 -g 30 \
    -c:a aac -b:a 128k \
    -f mpegts pipe:1 2>/tmp/manual_ffmpeg2.log \
| mpv --no-terminal --keep-open=yes --title="STVT manual v2" \
      --demuxer=lavf --demuxer-lavf-format=mpegts \
      --demuxer-lavf-analyzeduration=10000000 \
      --demuxer-lavf-probesize=50000000 \
      --cache=yes --cache-secs=30 \
      - 2>/tmp/manual_mpv2.log
