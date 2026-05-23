#!/bin/bash
# auto_test_beta.sh — sweep STVT_EQ_BETA values to find optimal LMS step
# size for the long equalizer. Uses quality_judge for scoring.
#
# BETA = LMS adaptation step. Default 5e-5.
# - Smaller (1e-5, 2e-5): slower adapt, more stable but less responsive
# - Larger (1e-4, 2e-4): faster adapt, more reactive but may diverge
#
# Holds the rest of the chain at the best-known config:
#   STVT_SDR_AGC=1 STVT_RS=erasure STVT_RS_ERASURES=18
#   STVT_EQ=long STVT_VITERBI=soft (uses tagged deinterleaver)

set -u
LOG=/tmp/auto_test_beta.log
RESULT=/tmp/auto_test_beta.csv
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
STABILIZE_SEC=120  # allow chain to converge + accumulate buffer
SAMPLES=4

# BETAs to test (default in code is 5e-5)
BETAS=(
    "1e-5"
    "2e-5"
    "5e-5"
    "1e-4"
    "2e-4"
)

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

kill_chain() {
    pkill -9 -f 'tv_live\.py.*--rf' 2>/dev/null
    pkill -9 -f 'stvt_play\.sh\|pipe_buffer\|ffmpeg.*libx264.*pipe:1\|mpv.*STVT Player\|tail.*live.ts' 2>/dev/null
    sleep 5
}

# Pause watchdog
WD=$(pgrep -f stvt_play_watchdog | head -1)
[ -n "$WD" ] && { log "pausing watchdog PID=$WD"; kill -STOP $WD; }
trap 'kill -CONT $WD 2>/dev/null; exit' EXIT INT TERM

> "$RESULT"
echo "beta,fps_avg,vid_err_avg,aud_err_avg,score_avg,bad_pkt_pct_avg" >> "$RESULT"

log "=== beta sweep starting (${#BETAS[@]} configs × ${STABILIZE_SEC}s) ==="

best_score=0
best_beta="(none)"

for beta in "${BETAS[@]}"; do
    log ""
    log "=== testing STVT_EQ_BETA=$beta ==="

    kill_chain
    rm -f "$LIVE_TS"

    cd /home/user/Software-TV-Tuner
    nohup bash -c "
        source /tmp/auto_acquire_winner.env
        export STVT_SDR_AGC=1
        export STVT_RS=erasure
        export STVT_RS_ERASURES=18
        export STVT_EQ=long
        export STVT_VITERBI=soft
        export STVT_EQ_BETA=$beta
        python3 tools/tv_live.py --rf 34
    " > /tmp/auto_test_beta_chain.log 2>&1 &
    disown

    deadline=$(($(date +%s) + STABILIZE_SEC))
    while [ ! -f "$LIVE_TS" ] || [ "$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)" -lt 80000000 ]; do
        sleep 5
        if [ $(date +%s) -gt $deadline ]; then
            log "  chain didn't produce live.ts in ${STABILIZE_SEC}s, skipping"
            echo "$beta,0,0,0,0,100" >> "$RESULT"
            kill_chain
            continue 2
        fi
    done
    log "  live.ts grew, sampling quality"

    sum_fps=0; sum_v=0; sum_a=0; sum_score=0
    for i in $(seq 1 $SAMPLES); do
        /home/user/quality_judge.sh >/dev/null 2>&1
        s=$(awk -F'[: ,]' '/"score":/{for(j=1;j<=NF;j++)if($j~/^[0-9]+$/){print $j; exit}}' /tmp/quality_state.json)
        fps=$(awk -F'[:,]' '/"fps":/{gsub(/[" ]/,"",$2); print $2}' /tmp/quality_state.json)
        v=$(awk -F'[:,]' '/"video_errors_per_sec":/{gsub(/[" ]/,"",$2); print $2}' /tmp/quality_state.json)
        a=$(awk -F'[:,]' '/"audio_errors_per_sec":/{gsub(/[" ]/,"",$2); print $2}' /tmp/quality_state.json)
        log "    sample $i: score=$s fps=$fps v_err=$v a_err=$a"
        sum_score=$((sum_score + s))
        sum_fps=$(awk "BEGIN{print $sum_fps + $fps}")
        sum_v=$(awk "BEGIN{print $sum_v + $v}")
        sum_a=$(awk "BEGIN{print $sum_a + $a}")
    done

    avg_score=$((sum_score / SAMPLES))
    avg_fps=$(awk "BEGIN{printf \"%.2f\", $sum_fps / $SAMPLES}")
    avg_v=$(awk "BEGIN{printf \"%.2f\", $sum_v / $SAMPLES}")
    avg_a=$(awk "BEGIN{printf \"%.2f\", $sum_a / $SAMPLES}")

    # Also extract avg bad-packet percentage from rs_erasure log
    bad_pct=$(grep '^\[rs_erasure' /tmp/auto_test_beta_chain.log 2>/dev/null | tail -10 | \
        awk -F'last5s: ' 'NF>1 {
            n=split($2,a," ")
            bad=0; pkts=0
            for(i=1;i<=n;i++) {
                if(a[i] ~ /^bad=/) bad=substr(a[i],5)
                if(a[i] ~ /^pkts=/) pkts=substr(a[i],6)
            }
            if(pkts > 0) {total_bad+=bad; total_pkts+=pkts; cnt++}
        } END {if(cnt > 0) printf "%.1f", 100*total_bad/total_pkts; else print "0"}')

    log "  AVG: score=$avg_score fps=$avg_fps v_err=$avg_v a_err=$avg_a bad_pkt=$bad_pct%"
    echo "$beta,$avg_fps,$avg_v,$avg_a,$avg_score,$bad_pct" >> "$RESULT"

    if [ "$avg_score" -gt "$best_score" ]; then
        best_score=$avg_score
        best_beta=$beta
    fi
done

kill_chain

log ""
log "=== RANKING ==="
{ head -1 "$RESULT"; tail -n +2 "$RESULT" | sort -t, -k5 -rn; } | column -t -s,
log ""
log "BEST: STVT_EQ_BETA=$best_beta with score=$best_score"

# Resume watchdog
kill -CONT $WD 2>/dev/null
log "watchdog resumed"
