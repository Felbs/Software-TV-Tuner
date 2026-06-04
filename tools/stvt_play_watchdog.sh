#!/bin/bash
# stvt_play_watchdog.sh — run stvt_play.sh with auto-restart when ffmpeg
# goes idle (the freeze pattern observed 2026-05-22: pipeline plays for
# ~30 min then ffmpeg encoder stops emitting frames because mpeg2 decoder
# loses its seq_header context).
#
# Watchdog: every 30s sample ffmpeg's wchar. If write rate stays at 0
# for 3 consecutive samples (90s), kill+restart the pipeline. fresh
# ffmpeg re-probes the existing live.ts (now 7+ GB) and finds new headers.

set -u
LOG=/tmp/stvt_play_watchdog.log
SAMPLE_SEC=30
ZERO_THRESHOLD=6   # 6 × 30s = 180s of zero output triggers restart
                   # (ffmpeg with -re needs longer probe headroom)

log() { echo "[$(date '+%H:%M:%S')] $*" >> "$LOG"; }
log "watchdog starting"

while true; do
    # Start the pipeline
    log "launching stvt_play.sh"
    /home/user/stvt_play.sh &
    PLAY_PID=$!
    log "stvt_play.sh PID=$PLAY_PID"

    # Wait for ffmpeg child to appear
    FF=""
    for try in 1 2 3 4 5 6 7 8 9 10; do
        sleep 3
        FF=$(pgrep -f 'ffmpeg.*libx264.*pipe:1$' | head -1)
        [ -n "$FF" ] && break
    done
    if [ -z "$FF" ]; then
        log "ffmpeg didn't start within 30s; killing wrapper"
        kill -9 $PLAY_PID 2>/dev/null
        pkill -9 -f 'STVT Player' 2>/dev/null
        sleep 5
        continue
    fi
    log "ffmpeg PID=$FF"

    # Monitor loop
    zero_count=0
    prev_wchar=$(awk '/^wchar/{print $2}' /proc/$FF/io 2>/dev/null || echo 0)
    while kill -0 $FF 2>/dev/null; do
        sleep $SAMPLE_SEC
        cur_wchar=$(awk '/^wchar/{print $2}' /proc/$FF/io 2>/dev/null || echo 0)
        delta=$((cur_wchar - prev_wchar))
        prev_wchar=$cur_wchar
        if [ "$delta" -lt 50000 ]; then  # less than 50KB in 30s = stalled
            zero_count=$((zero_count + 1))
            log "ffmpeg stalled sample $zero_count/$ZERO_THRESHOLD (delta=${delta} bytes in ${SAMPLE_SEC}s)"
            if [ "$zero_count" -ge "$ZERO_THRESHOLD" ]; then
                log "ffmpeg stalled for $((ZERO_THRESHOLD * SAMPLE_SEC))s, restarting pipeline"
                break
            fi
        else
            if [ "$zero_count" -gt 0 ]; then
                log "ffmpeg recovered ($((delta / 1024)) KB in ${SAMPLE_SEC}s)"
            fi
            zero_count=0
        fi
    done

    # Kill the pipeline (everything in its process tree)
    log "tearing down pipeline"
    pkill -9 -f 'STVT Player' 2>/dev/null
    pkill -9 -f 'ffmpeg.*libx264.*pipe:1$' 2>/dev/null
    pkill -9 -f 'python3 /home/user/pipe_buffer.py' 2>/dev/null
    pkill -9 -f 'tail -c \+0.*live.ts' 2>/dev/null
    kill -9 $PLAY_PID 2>/dev/null
    sleep 5
    log "restarting in 2s"
    sleep 2
done
