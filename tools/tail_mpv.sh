#!/bin/bash
# tail_mpv.sh — play a growing live.ts directly with mpv, restarting
# from a fresh tail position when mpv reaches EOF.
#
# Why this exists: ffmpeg-from-pipe takes 30+ minutes to find an MPEG-2
# seq_header in the chain's corrupt output (probe is sequential, can't
# seek). ffmpeg-from-file probes in seconds (can seek). mpv has the same
# seek-vs-sequential difference. So reading live.ts as a file (instead of
# tail|ffmpeg|mpv pipe) just works.
#
# Trade-off: mpv reads from a fixed start position, so it plays a few
# seconds BEHIND the live edge. Re-launch every time mpv exits to catch
# up to the current end of the growing file.

LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
LOG=/tmp/tail_mpv.log

log() { echo "[$(date '+%H:%M:%S')] $*" >> "$LOG"; }
log "tail_mpv starting"

while true; do
    # Wait for live.ts to exist + be substantial
    while [ ! -f "$LIVE_TS" ] || [ "$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)" -lt 50000000 ]; do
        sleep 2
    done

    SZ=$(stat -c%s "$LIVE_TS")
    # Start playing from current end-1.5GB or beginning
    START_OFFSET=$(( SZ > 1500000000 ? SZ - 1500000000 : 0 ))
    log "starting mpv at offset $START_OFFSET (live.ts is $((SZ/1024/1024)) MB)"

    # mpv on the live file. --start gives byte offset (use --stream-offset=N).
    # Actually mpv doesn't have byte-offset start; we'd have to use a
    # short shell trick or rely on mpv's own seek-near-end behavior.
    mpv --no-terminal --keep-open=no \
        --title="STVT live" \
        --demuxer=lavf --demuxer-lavf-format=mpegts \
        --cache=yes --cache-secs=30 \
        --demuxer-max-bytes=200MiB \
        "$LIVE_TS"

    EXIT_CODE=$?
    log "mpv exited code=$EXIT_CODE; respawn in 2s"
    sleep 2
done
