#!/bin/bash
# stvt_play.sh — direct tail → mpv with --untimed + tolerant demux flags +
# auto-respawn. --untimed makes mpv ignore PTS/sync entirely and play
# frames as they arrive; combined with input rate-limited by the chain
# itself (~1.5MB/s = real-time), playback runs at wall-clock speed.

set -u
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
TITLE="STVT Player"
STALL_SEC=15
MAX_RUN_SEC=600

which mpv >/dev/null || { echo "mpv missing" >&2; exit 1; }

wait_live() {
    while [ ! -f "$LIVE_TS" ] || [ "$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)" -lt 50000000 ]; do
        sleep 2
    done
}

while true; do
    wait_live
    SZ=$(stat -c%s "$LIVE_TS")
    echo "[$(date +%H:%M:%S)] starting mpv at offset $((SZ/1024/1024)) MB"

    (
        exec tail -c +0 --bytes=+"$SZ" -F "$LIVE_TS" \
          | mpv - \
              --title="$TITLE" \
              --msg-level=all=warn \
              --demuxer=lavf \
              --demuxer-lavf-format=mpegts \
              --demuxer-lavf-o=fflags=+discardcorrupt+nobuffer+igndts+genpts,err_detect=ignore_err \
              --untimed \
              --no-cache \
              --no-resume-playback \
              --no-input-default-bindings \
              --no-input-terminal \
              --hr-seek=no \
              --container-fps-override=29.97 \
              --vd-lavc-skiploopfilter=all \
              --vd-lavc-threads=2 \
              --alang=eng,en \
              --keep-open=yes \
              2>/tmp/stvt_mpv.log
    ) &
    PIPELINE_PID=$!

    sleep 5
    FP=$(pgrep -P $PIPELINE_PID -f 'mpv.*STVT Player' | head -1)
    [ -z "$FP" ] && FP=$(pgrep -f 'mpv -.*STVT Player' | head -1)
    if [ -z "$FP" ]; then
        echo "[$(date +%H:%M:%S)] mpv didn't spawn — retry"
        kill $PIPELINE_PID 2>/dev/null
        sleep 3
        continue
    fi
    echo "[$(date +%H:%M:%S)] mpv PID=$FP"

    stall_count=0
    prev_rchar=$(awk '/^rchar/{print $2}' /proc/$FP/io 2>/dev/null || echo 0)
    start_t=$(date +%s)
    while kill -0 $FP 2>/dev/null; do
        sleep 3
        cur_rchar=$(awk '/^rchar/{print $2}' /proc/$FP/io 2>/dev/null || echo 0)
        delta=$((cur_rchar - prev_rchar))
        prev_rchar=$cur_rchar
        if [ "$delta" -lt 10000 ]; then
            stall_count=$((stall_count + 3))
            if [ "$stall_count" -ge "$STALL_SEC" ]; then
                echo "[$(date +%H:%M:%S)] mpv read stalled ${stall_count}s — restart"
                break
            fi
        else
            stall_count=0
        fi
        elapsed=$(( $(date +%s) - start_t ))
        if [ "$elapsed" -ge "$MAX_RUN_SEC" ]; then
            echo "[$(date +%H:%M:%S)] preemptive restart at ${elapsed}s"
            break
        fi
    done

    # kill-ok: player/pipeline consumer (mpv/ffmpeg/tail), not the SDR holder
    kill -9 $FP 2>/dev/null
    # kill-ok: player/pipeline consumer (mpv/ffmpeg/tail), not the SDR holder
    pkill -9 -P $PIPELINE_PID 2>/dev/null
    # kill-ok: player/pipeline consumer (mpv/ffmpeg/tail), not the SDR holder
    kill -9 $PIPELINE_PID 2>/dev/null
    sleep 2
done
