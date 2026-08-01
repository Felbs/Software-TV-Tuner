#!/bin/bash
# iq_hunter.sh — automatic capture + sweep loop. Runs unattended until
# a capture+config combination produces decoded MPEG-2 frames.
#
# Each cycle:
#   1. Pause live chain for 90s
#   2. Capture 90s IQ at the configured gain
#   3. Restart live chain
#   4. Run iq_sweep against the capture in background
#   5. After sweep finishes, check leaderboard
#   6. If any config got total > 0 frames → SUCCESS, alert + exit
#   7. Else delete capture, wait until top-of-next-hour, repeat
#
# Notifies via: stdout + /tmp/iq_hunter.log + (optional) writing
# /tmp/iq_hunter_SUCCESS.env when a working config is found.
#
# Usage:
#   nohup ~/Software-TV-Tuner/tools/iq_hunter.sh > /tmp/iq_hunter.log 2>&1 &
#   disown

set -u
STVT=/home/user/Software-TV-Tuner
LOG=/tmp/iq_hunter.log
CAPTURE_DIR=/home/user/iq_captures
SUCCESS_FLAG=/tmp/iq_hunter_SUCCESS.env

# Tunables
CAPTURE_SECS=90
SWEEP_SECS=30
SLEEP_BETWEEN_CYCLES_SEC=3600   # 1 hour between attempts

mkdir -p "$CAPTURE_DIR"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

cycle=0
log "iq_hunter starting"
log "  capture: ${CAPTURE_SECS}s   sweep: ${SWEEP_SECS}s/config × 32 configs"
log "  cycle interval: ${SLEEP_BETWEEN_CYCLES_SEC}s"

while true; do
    cycle=$((cycle + 1))
    TS=$(date '+%Y%m%d_%H%M%S')
    CAPFILE="$CAPTURE_DIR/cap_${TS}.cf32"

    log ""
    log "=== CYCLE $cycle (capture: $CAPFILE) ==="

    # 1. Stop chain to free SDR
    log "stopping chain"
    for p in $(pgrep -f auto_play_forever) $(pgrep -f auto_acquire) \
             $(pgrep -f run_stvt_winner) $(pgrep -f tv_tuner.py) \
             $(pgrep -f 'tv_live.py --rf') $(pgrep -f 'ffmpeg.*pipe') \
             $(pgrep mpv); do
        # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign); loop also sweeps mpv/ffmpeg players
        kill -9 $p 2>/dev/null
    done
    sleep 5

    # 2. Capture IQ
    log "capturing ${CAPTURE_SECS}s IQ to $CAPFILE"
    python3 "$STVT/tools/record_iq.py" \
        --rf 34 --seconds "$CAPTURE_SECS" \
        --out "$CAPFILE" > /tmp/iq_hunter_capture.log 2>&1
    SZ=$(stat -c%s "$CAPFILE" 2>/dev/null || echo 0)
    if [ "$SZ" -lt 1000000000 ]; then
        log "capture too small ($SZ bytes) — SDR may not have run; sleeping"
        rm -f "$CAPFILE"
        sleep 120
        continue
    fi
    log "captured $((SZ/1024/1024/1024)) GB"

    # 3. Restart chain (live TV resumes)
    log "restarting live chain"
    nohup /home/user/auto_play_forever.sh > /tmp/auto_play_forever.log 2>&1 &
    disown

    # 4. Run sweep against this capture in BACKGROUND
    SWEEP_CSV="/tmp/iq_hunter_${TS}.csv"
    SWEEP_LOG="/tmp/iq_hunter_${TS}.log"
    log "running iq_sweep (~$((SWEEP_SECS * 32 / 60)) min) → $SWEEP_CSV"
    python3 "$STVT/tools/iq_sweep.py" \
        --iq "$CAPFILE" --seconds "$SWEEP_SECS" \
        --out-csv "$SWEEP_CSV" > "$SWEEP_LOG" 2>&1
    log "sweep finished"

    # 5. Check leaderboard
    BEST_LINE=$(sort -t, -k9 -rn "$SWEEP_CSV" | head -2 | tail -1)
    BEST_TOTAL=$(echo "$BEST_LINE" | awk -F, '{print $9}')
    log "best total frames: $BEST_TOTAL   ($BEST_LINE)"

    if [ "${BEST_TOTAL:-0}" -gt 0 ]; then
        log "*** SUCCESS! at least one config decodes frames ***"
        # Parse the best config
        EQ=$(echo "$BEST_LINE" | awk -F, '{print $1}')
        VIT=$(echo "$BEST_LINE" | awk -F, '{print $2}')
        RS=$(echo "$BEST_LINE" | awk -F, '{print $3}')
        NB=$(echo "$BEST_LINE" | awk -F, '{print $4}')
        log "winning config: EQ=$EQ VIT=$VIT RS=$RS NB=$NB"
        # Write env file
        cat > "$SUCCESS_FLAG" <<EOF
# iq_hunter found a decoding config $(date)
export STVT_EQ=$EQ
export STVT_VITERBI=$VIT
export STVT_RS=$RS
export STVT_NB=$NB
# captured IQ: $CAPFILE
# leaderboard CSV: $SWEEP_CSV
EOF
        log "wrote $SUCCESS_FLAG"
        log "EXITING — apply this config and ship"
        exit 0
    fi

    # 6. Failed — keep capture but log
    log "no config decoded — keeping capture for forensics"
    log "sleeping ${SLEEP_BETWEEN_CYCLES_SEC}s before next cycle"
    sleep "$SLEEP_BETWEEN_CYCLES_SEC"
done
