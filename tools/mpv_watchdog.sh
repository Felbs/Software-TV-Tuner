#!/bin/bash
# mpv_watchdog.sh — kill mpv when video playback stalls.
#
# Detects the "chain is alive but mpv stopped reading" pattern by
# comparing mpv's rchar to ffmpeg's wchar over a 10-second window.
# If ffmpeg is writing but mpv isn't reading, mpv is stuck — kill it
# so tv_tuner.py respawns a fresh one with a clean probe.
#
# Runs forever. Lightweight (one sample per 10s).
#
# Usage:
#   nohup ~/mpv_watchdog.sh > /tmp/mpv_watchdog.log 2>&1 &
#   disown
#
# Log: /tmp/mpv_watchdog.log

set -u
LOG=/tmp/mpv_watchdog.log
PROBE_INTERVAL=10           # seconds between checks
STALL_THRESHOLD_KB=20       # mpv read < this much per interval = stalled
STALL_STREAKS_BEFORE_KILL=2 # need N consecutive stalls before kill (avoid false positives)

streak=0
last_kill=0

log() {
    # Write directly to the log file (bypass stdout buffering).
    echo "[$(date '+%H:%M:%S')] $*" >> "$LOG"
}

TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts

while true; do
    MPV=$(ps -eo pid,cmd | grep -E '^ *[0-9]+ +mpv ' | awk '{print $1}' | head -1)

    # Check live.ts growth — this is the CHAIN producing data, independent
    # of any downstream blockage. If live.ts is growing but mpv isn't
    # reading, the pipeline is deadlocked.
    if [ ! -f "$TS" ]; then
        log "waiting for live.ts"
        streak=0
        sleep $PROBE_INTERVAL
        continue
    fi
    S1=$(stat -c%s "$TS" 2>/dev/null)
    R1=$(awk '/^rchar/{print $2}' /proc/$MPV/io 2>/dev/null || echo 0)
    sleep $PROBE_INTERVAL
    S2=$(stat -c%s "$TS" 2>/dev/null)
    R2=$(awk '/^rchar/{print $2}' /proc/$MPV/io 2>/dev/null || echo 0)

    TS_KB=$(((S2-S1) / 1024))
    MPV_KB=$(((R2-R1) / 1024))

    # If live.ts isn't growing, chain itself is dead — not our problem to fix
    if [ $TS_KB -lt 500 ]; then
        log "tick chain_KB=${TS_KB} mpv_KB=${MPV_KB} mpv=$MPV (chain low-rate, not a mpv stall)"
        streak=0
        continue
    fi

    if [ -z "$MPV" ]; then
        log "tick chain_KB=${TS_KB} mpv_KB=- (no mpv yet)"
        streak=0
        continue
    fi

    # Healthy: chain produces, mpv reads
    if [ $MPV_KB -ge $STALL_THRESHOLD_KB ]; then
        log "OK chain_KB=${TS_KB} mpv_KB=${MPV_KB}"
        streak=0
    else
        # Chain producing > 500 KB/s but mpv consuming < 20 KB/s = STALL
        streak=$((streak + 1))
        log "STALL streak=$streak/$STALL_STREAKS_BEFORE_KILL  chain_KB=${TS_KB} mpv_KB=${MPV_KB} pid=$MPV"
        if [ $streak -ge $STALL_STREAKS_BEFORE_KILL ]; then
            NOW=$(date +%s)
            if [ $((NOW - last_kill)) -lt 60 ]; then
                log "  too soon since last kill, waiting"
            else
                log "  KILLING mpv $MPV (tv_tuner will respawn)"
                # kill-ok: player/pipeline consumer (mpv/ffmpeg/tail), not the SDR holder
                kill -9 $MPV
                last_kill=$NOW
                streak=0
            fi
        fi
    fi
done
