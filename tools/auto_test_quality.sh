#!/bin/bash
# auto_test_quality.sh — autonomous quality search.
#
# For each config in CONFIGS:
#   1. Restart tv_live with that config
#   2. Wait 90s for chain to stabilize and live.ts to grow
#   3. Run quality_judge 3 times (90s of measurement)
#   4. Record average score
# Then ranks all configs by quality score, picks winner.
#
# Goal: find config that scores >= 90 (cable_quality) or report the
# best achievable. Logs to /tmp/auto_test_quality.csv + .log.
#
# Stops EARLY if any config scores >= 90. Otherwise tries them all.
#
# CRITICAL: this script EXPECTS no other tv_live to be running. It
# manages tv_live itself.

set -u
LOG=/tmp/auto_test_quality.log
RESULT=/tmp/auto_test_quality.csv
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
CABLE_TARGET=90
STABILIZE_SEC=90
SAMPLES=3

# Configs to test. Format: "label STVT_VAR=value STVT_VAR=value ..."
CONFIGS=(
    "baseline STVT_SDR_AGC=1 STVT_RS=erasure STVT_RS_ERASURES=14 STVT_EQ=long"
    "no_agc STVT_SDR_AGC=0 STVT_RS=erasure STVT_RS_ERASURES=14 STVT_EQ=long"
    "stock_rs STVT_SDR_AGC=1 STVT_RS=stock STVT_EQ=long"
    "erasure_low STVT_SDR_AGC=1 STVT_RS=erasure STVT_RS_ERASURES=8 STVT_EQ=long"
    "erasure_max STVT_SDR_AGC=1 STVT_RS=erasure STVT_RS_ERASURES=18 STVT_EQ=long"
    "eq_pilot STVT_SDR_AGC=1 STVT_RS=erasure STVT_RS_ERASURES=14 STVT_EQ=pilot"
    "eq_pilot_dd_soft STVT_SDR_AGC=1 STVT_RS=erasure STVT_RS_ERASURES=14 STVT_EQ=pilot_dd_soft"
    "eq_cma STVT_SDR_AGC=1 STVT_RS=erasure STVT_RS_ERASURES=14 STVT_EQ=cma"
    "nb_on STVT_SDR_AGC=1 STVT_RS=erasure STVT_RS_ERASURES=14 STVT_EQ=long STVT_NB=1"
)

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

kill_chain() {
    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
    pkill -9 -f 'tv_live\.py.*--rf' 2>/dev/null
    # kill-ok: player/pipeline consumer (mpv/ffmpeg/tail), not the SDR holder
    pkill -9 -f 'stvt_play\.sh\|pipe_buffer\|ffmpeg.*libx264\|mpv.*STVT Player\|tail.*live.ts' 2>/dev/null
    sleep 5
}

# Pause watchdog (we don't want it interfering)
WD=$(pgrep -f stvt_play_watchdog | head -1)
if [ -n "$WD" ]; then
    log "pausing watchdog PID=$WD"
    kill -STOP $WD
fi
trap 'kill -CONT $WD 2>/dev/null; exit' EXIT INT TERM

# Init CSV
> "$RESULT"
echo "label,config,fps_avg,vid_err_avg,aud_err_avg,score_avg,best_sample" >> "$RESULT"

log "=== auto_test_quality starting (target score >= $CABLE_TARGET) ==="

best_score=0
best_label="(none)"
best_config="(none)"

for entry in "${CONFIGS[@]}"; do
    label=$(echo "$entry" | awk '{print $1}')
    config=$(echo "$entry" | cut -d' ' -f2-)

    log ""
    log "=== testing $label ==="
    log "  config: $config"

    # Kill any existing chain
    kill_chain
    rm -f "$LIVE_TS"

    # Start tv_live with this config
    cd /home/user/Software-TV-Tuner
    nohup bash -c "
        source /tmp/auto_acquire_winner.env
        $(echo "$config" | tr ' ' '\n' | sed 's/^/export /')
        python3 tools/tv_live.py --rf 34
    " > /tmp/auto_test_chain.log 2>&1 &
    disown

    # Wait for live.ts to grow
    deadline=$(($(date +%s) + 120))
    while [ ! -f "$LIVE_TS" ] || [ "$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)" -lt 50000000 ]; do
        sleep 5
        if [ $(date +%s) -gt $deadline ]; then
            log "  chain didn't produce live.ts in 120s, skipping"
            echo "$label,\"$config\",0,0,0,0,0" >> "$RESULT"
            kill_chain
            continue 2
        fi
    done

    log "  live.ts grew to $(($(stat -c%s "$LIVE_TS")/1024/1024)) MB, sampling quality"

    # Sample quality SAMPLES times
    sum_fps=0; sum_v=0; sum_a=0; sum_score=0; best_in_run=0
    for i in $(seq 1 $SAMPLES); do
        /home/user/quality_judge.sh >/dev/null 2>&1
        s=$(awk -F'[: ,]' '/"score":/{for(j=1;j<=NF;j++)if($j~/^[0-9]+$/){print $j; exit}}' /tmp/quality_state.json)
        fps=$(awk -F'[:,]' '/"fps":/{gsub(/[" ]/,"",$2); print $2}' /tmp/quality_state.json)
        v=$(awk -F'[:,]' '/"video_errors_per_sec":/{gsub(/[" ]/,"",$2); print $2}' /tmp/quality_state.json)
        a=$(awk -F'[:,]' '/"audio_errors_per_sec":/{gsub(/[" ]/,"",$2); print $2}' /tmp/quality_state.json)
        log "    sample $i/$SAMPLES: score=$s fps=$fps v_err=$v a_err=$a"
        sum_score=$((sum_score + s))
        sum_fps=$(awk "BEGIN{print $sum_fps + $fps}")
        sum_v=$(awk "BEGIN{print $sum_v + $v}")
        sum_a=$(awk "BEGIN{print $sum_a + $a}")
        [ "$s" -gt "$best_in_run" ] && best_in_run=$s
    done

    avg_score=$((sum_score / SAMPLES))
    avg_fps=$(awk "BEGIN{printf \"%.2f\", $sum_fps / $SAMPLES}")
    avg_v=$(awk "BEGIN{printf \"%.2f\", $sum_v / $SAMPLES}")
    avg_a=$(awk "BEGIN{printf \"%.2f\", $sum_a / $SAMPLES}")

    log "  AVERAGE: score=$avg_score fps=$avg_fps v_err=$avg_v a_err=$avg_a (best sample=$best_in_run)"
    echo "$label,\"$config\",$avg_fps,$avg_v,$avg_a,$avg_score,$best_in_run" >> "$RESULT"

    if [ "$avg_score" -gt "$best_score" ]; then
        best_score=$avg_score
        best_label=$label
        best_config=$config
    fi

    # Early-exit if cable quality reached
    if [ "$avg_score" -ge "$CABLE_TARGET" ]; then
        log "*** CABLE QUALITY REACHED: $label scored $avg_score ***"
        break
    fi
done

kill_chain

log ""
log "=== FINAL RANKING ==="
sort -t, -k6 -rn "$RESULT" | head -10 | tee -a "$LOG"

log ""
log "BEST: $best_label score=$best_score"
log "config: $best_config"

if [ "$best_score" -ge "$CABLE_TARGET" ]; then
    log "STATUS: CABLE QUALITY ACHIEVABLE"
else
    log "STATUS: cable quality NOT achievable. Best is $best_score (need $CABLE_TARGET)."
fi

# Resume watchdog
kill -CONT $WD 2>/dev/null
log "watchdog resumed"
