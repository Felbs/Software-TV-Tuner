#!/bin/bash
# auto_test_dsp_knobs.sh — sweep DSP knobs NOT yet tested.
#
# Builds on current best (BETA=2e-5 + LEAK=1e-3 + soft viterbi + tagged
# deinterleaver) by varying ONE knob at a time:
#   - ATSC_SYNC_SOFT_ALPHA (timing loop bandwidth)
#   - STVT_FPLL_ALPHA      (PLL bandwidth)
#   - STVT_AGC_ALPHA       (AGC time constant)
#   - STVT_AGC_REFERENCE   (AGC setpoint)
#   - STVT_DCR_TAPS        (DC blocker tap count)
#   - STVT_RS_ERASURES     (RS erasure max)

set -u
LOG=/tmp/auto_test_dsp_knobs.log
RESULT=/tmp/auto_test_dsp_knobs.csv
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
STABILIZE_SEC=150
SAMPLES=4

CONFIGS=(
    "baseline ATSC_SYNC_SOFT_ALPHA=0.4 STVT_FPLL_ALPHA=0.001 STVT_AGC_REFERENCE=4.0 STVT_DCR_TAPS=32 STVT_RS_ERASURES=18"
    "sync_slow ATSC_SYNC_SOFT_ALPHA=0.2 STVT_FPLL_ALPHA=0.001 STVT_AGC_REFERENCE=4.0 STVT_DCR_TAPS=32 STVT_RS_ERASURES=18"
    "sync_fast ATSC_SYNC_SOFT_ALPHA=0.7 STVT_FPLL_ALPHA=0.001 STVT_AGC_REFERENCE=4.0 STVT_DCR_TAPS=32 STVT_RS_ERASURES=18"
    "fpll_tight ATSC_SYNC_SOFT_ALPHA=0.4 STVT_FPLL_ALPHA=0.0005 STVT_AGC_REFERENCE=4.0 STVT_DCR_TAPS=32 STVT_RS_ERASURES=18"
    "fpll_loose ATSC_SYNC_SOFT_ALPHA=0.4 STVT_FPLL_ALPHA=0.003 STVT_AGC_REFERENCE=4.0 STVT_DCR_TAPS=32 STVT_RS_ERASURES=18"
    "agc_lower ATSC_SYNC_SOFT_ALPHA=0.4 STVT_FPLL_ALPHA=0.001 STVT_AGC_REFERENCE=2.0 STVT_DCR_TAPS=32 STVT_RS_ERASURES=18"
    "agc_higher ATSC_SYNC_SOFT_ALPHA=0.4 STVT_FPLL_ALPHA=0.001 STVT_AGC_REFERENCE=6.0 STVT_DCR_TAPS=32 STVT_RS_ERASURES=18"
    "dcr_long ATSC_SYNC_SOFT_ALPHA=0.4 STVT_FPLL_ALPHA=0.001 STVT_AGC_REFERENCE=4.0 STVT_DCR_TAPS=128 STVT_RS_ERASURES=18"
    "dcr_short ATSC_SYNC_SOFT_ALPHA=0.4 STVT_FPLL_ALPHA=0.001 STVT_AGC_REFERENCE=4.0 STVT_DCR_TAPS=16 STVT_RS_ERASURES=18"
    "ras_max ATSC_SYNC_SOFT_ALPHA=0.4 STVT_FPLL_ALPHA=0.001 STVT_AGC_REFERENCE=4.0 STVT_DCR_TAPS=32 STVT_RS_ERASURES=20"
    "ras_low ATSC_SYNC_SOFT_ALPHA=0.4 STVT_FPLL_ALPHA=0.001 STVT_AGC_REFERENCE=4.0 STVT_DCR_TAPS=32 STVT_RS_ERASURES=12"
)

# Common baseline (the best we have so far)
BASE_ENV="STVT_SDR_AGC=1 STVT_RS=erasure STVT_EQ=long STVT_VITERBI=soft STVT_EQ_BETA=2e-5 STVT_EQ_LEAK=1e-3"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

kill_chain() {
    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
    pkill -9 -f 'tv_live\.py.*--rf' 2>/dev/null
    # kill-ok: player/pipeline consumer (mpv/ffmpeg/tail), not the SDR holder
    pkill -9 -f 'stvt_play\.sh\|pipe_buffer\|ffmpeg.*libx264.*pipe:1\|mpv.*STVT Player\|tail.*live.ts' 2>/dev/null
    sleep 5
}

# Pause the quality controller AND the watchdog so they don't interfere
CTRL=$(pgrep -f quality_controller.sh | head -1)
[ -n "$CTRL" ] && { log "pausing controller PID=$CTRL"; kill -STOP $CTRL; }
WD=$(pgrep -f stvt_play_watchdog | head -1)
[ -n "$WD" ] && { log "pausing watchdog PID=$WD"; kill -STOP $WD; }
trap '[ -n "$CTRL" ] && kill -CONT $CTRL 2>/dev/null; [ -n "$WD" ] && kill -CONT $WD 2>/dev/null; exit' EXIT INT TERM

> "$RESULT"
echo "label,score_avg,fps_avg,bad_pct_avg,bad_pct_max" >> "$RESULT"

best_score=0
best_label="(none)"

for entry in "${CONFIGS[@]}"; do
    label=$(echo "$entry" | awk '{print $1}')
    extra=$(echo "$entry" | cut -d' ' -f2-)

    log ""
    log "=== testing $label ==="
    log "  extra: $extra"

    kill_chain
    rm -f "$LIVE_TS"
    > /tmp/dsp_chain.log

    cd /home/user/Software-TV-Tuner
    nohup bash -c "
        source /tmp/auto_acquire_winner.env
        $(echo "$BASE_ENV $extra" | tr ' ' '\n' | sed 's/^/export /')
        python3 tools/tv_live.py --rf 34
    " > /tmp/dsp_chain.log 2>&1 &
    disown

    deadline=$(($(date +%s) + STABILIZE_SEC))
    while [ ! -f "$LIVE_TS" ] || [ "$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)" -lt 100000000 ]; do
        sleep 5
        if [ $(date +%s) -gt $deadline ]; then
            log "  chain didn't acquire in ${STABILIZE_SEC}s — skip"
            echo "$label,0,0,100,100" >> "$RESULT"
            kill_chain
            continue 2
        fi
    done
    log "  stabilized, sampling quality"

    sum_score=0; sum_fps=0
    for i in $(seq 1 $SAMPLES); do
        /home/user/quality_judge.sh >/dev/null 2>&1
        s=$(awk -F'[: ,]' '/"score":/{for(j=1;j<=NF;j++)if($j~/^[0-9]+$/){print $j; exit}}' /tmp/quality_state.json)
        fps=$(awk -F'[:,]' '/"fps":/{gsub(/[" ]/,"",$2); print $2}' /tmp/quality_state.json)
        log "    sample $i: score=$s fps=$fps"
        sum_score=$((sum_score + s))
        sum_fps=$(awk "BEGIN{print $sum_fps + $fps}")
    done
    avg_score=$((sum_score / SAMPLES))
    avg_fps=$(awk "BEGIN{printf \"%.2f\", $sum_fps / $SAMPLES}")

    # bad pct from rs_erasure
    stats=$(grep '^\[rs_erasure' /tmp/dsp_chain.log 2>/dev/null | tail -20 | \
        awk -F'last5s: ' 'NF>1 {
            n=split($2,a," ")
            for(i=1;i<=n;i++) {
                if(a[i] ~ /^bad=/) bad=substr(a[i],5)
                if(a[i] ~ /^pkts=/) pkts=substr(a[i],6)
            }
            if(pkts > 0) {
                pct = 100*bad/pkts
                sum += pct
                if(pct > max) max=pct
                cnt++
            }
        } END {
            if(cnt > 0) printf "%.2f,%.2f", sum/cnt, max
            else printf "0,0"
        }')

    log "  AVG: score=$avg_score fps=$avg_fps  rs_bad: $stats"
    echo "$label,$avg_score,$avg_fps,$stats" >> "$RESULT"

    if [ "$avg_score" -gt "$best_score" ]; then
        best_score=$avg_score
        best_label=$label
        log "  *** new best: $label score=$best_score"
    fi
done

kill_chain
log ""
log "=== RANKING ==="
{ head -1 "$RESULT"; tail -n +2 "$RESULT" | sort -t, -k2 -rn; } | column -t -s,
log "BEST: $best_label score=$best_score"

# Resume controller + watchdog
[ -n "$WD" ] && kill -CONT $WD 2>/dev/null
[ -n "$CTRL" ] && kill -CONT $CTRL 2>/dev/null
log "controller + watchdog resumed"
