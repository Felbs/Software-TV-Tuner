#!/bin/bash
# auto_test_stability.sh — find config that minimizes bad-rate spikes.
#
# Holds BETA=2e-5 + soft viterbi + tagged deinterleaver (current best).
# Varies NB on/off and LEAK to find what damps spikes.
#
# Stability metric: STDEV of bad-rate samples. Lower = more stable.
# Also reports the worst spike (max bad%) — what user actually sees.

set -u
LOG=/tmp/auto_test_stability.log
RESULT=/tmp/auto_test_stability.csv
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
STABILIZE_SEC=180  # 3 min to ensure equalizer settled
SAMPLES_DURATION_SEC=120  # measure spikes over 2 min

# label  NB  LEAK
CONFIGS=(
    "baseline  0  5e-4"
    "nb_on     1  5e-4"
    "leak_lo   0  1e-4"
    "leak_hi   0  1e-3"
    "nb_leak_hi 1 1e-3"
)

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

kill_chain() {
    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
    pkill -9 -f 'tv_live\.py.*--rf' 2>/dev/null
    # kill-ok: player/pipeline consumer (mpv/ffmpeg/tail), not the SDR holder
    pkill -9 -f 'stvt_play\.sh\|pipe_buffer\|ffmpeg.*libx264.*pipe:1\|mpv.*STVT Player\|tail.*live.ts' 2>/dev/null
    sleep 5
}

# Pause watchdog
WD=$(pgrep -f stvt_play_watchdog | head -1)
[ -n "$WD" ] && { log "pausing watchdog PID=$WD"; kill -STOP $WD; }
trap 'kill -CONT $WD 2>/dev/null; exit' EXIT INT TERM

> "$RESULT"
echo "label,nb,leak,bad_pct_mean,bad_pct_stdev,bad_pct_max,n_samples" >> "$RESULT"

for entry in "${CONFIGS[@]}"; do
    label=$(echo $entry | awk '{print $1}')
    nb=$(echo $entry | awk '{print $2}')
    leak=$(echo $entry | awk '{print $3}')

    log ""
    log "=== testing $label (NB=$nb LEAK=$leak) ==="

    kill_chain
    rm -f "$LIVE_TS"
    > /tmp/stab_chain.log

    cd /home/user/Software-TV-Tuner
    nohup bash -c "
        source /tmp/auto_acquire_winner.env
        export STVT_SDR_AGC=1
        export STVT_RS=erasure
        export STVT_RS_ERASURES=18
        export STVT_EQ=long
        export STVT_VITERBI=soft
        export STVT_EQ_BETA=2e-5
        export STVT_EQ_LEAK=$leak
        export STVT_NB=$nb
        python3 tools/tv_live.py --rf 34
    " > /tmp/stab_chain.log 2>&1 &
    disown

    # Wait for live.ts to grow + chain to stabilize
    log "  warming up ${STABILIZE_SEC}s..."
    deadline=$(($(date +%s) + STABILIZE_SEC))
    while [ $(date +%s) -lt $deadline ]; do
        sleep 10
    done

    # Sample rs_erasure log lines over SAMPLES_DURATION_SEC
    log "  measuring spikes for ${SAMPLES_DURATION_SEC}s..."
    sleep $SAMPLES_DURATION_SEC

    # Compute stats from the last N rs_erasure log lines
    # Each line is 5 seconds, so we look at last (SAMPLES_DURATION_SEC/5) lines
    n_lines=$((SAMPLES_DURATION_SEC / 5))
    stats=$(grep '^\[rs_erasure' /tmp/stab_chain.log 2>/dev/null | tail -$n_lines | \
        awk -F'last5s: ' 'NF>1 {
            n=split($2,a," ")
            for(i=1;i<=n;i++) {
                if(a[i] ~ /^bad=/) bad=substr(a[i],5)
                if(a[i] ~ /^pkts=/) pkts=substr(a[i],6)
            }
            if(pkts > 0) {
                pct = 100*bad/pkts
                sum += pct
                sumsq += pct*pct
                if(pct > max) max=pct
                cnt++
            }
        } END {
            if(cnt > 0) {
                mean = sum/cnt
                variance = sumsq/cnt - mean*mean
                if(variance < 0) variance = 0
                stdev = sqrt(variance)
                printf "%.2f,%.2f,%.2f,%d", mean, stdev, max, cnt
            } else {
                printf "0,0,0,0"
            }
        }')
    log "  mean,stdev,max,n: $stats"
    echo "$label,$nb,$leak,$stats" >> "$RESULT"
done

kill_chain

log ""
log "=== RANKING (sort by stdev — lower is more stable) ==="
{ head -1 "$RESULT"; tail -n +2 "$RESULT" | sort -t, -k5 -n; } | column -t -s,

log ""
log "=== also sort by max-spike — lower means fewer visible glitches ==="
{ head -1 "$RESULT"; tail -n +2 "$RESULT" | sort -t, -k6 -n; } | column -t -s,

# Resume watchdog
kill -CONT $WD 2>/dev/null
log "watchdog resumed"
