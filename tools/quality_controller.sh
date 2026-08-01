#!/bin/bash
# quality_controller.sh — autonomous quality watchdog + config switcher.
#
# Continuously samples /tmp/quality_state.json (via quality_judge.sh). When
# the moving-average score drops below MIN_OK for STALL_SAMPLES consecutive
# samples, switches the chain to the next config in CONFIGS[]. When score
# is high enough (TARGET_OK) for TARGET_SAMPLES samples, declares success
# and stops cycling.
#
# Designed to run unattended for hours, exploring config space until it
# finds a sustained good moment.

set -u
LOG=/tmp/quality_controller.log
STATE_FILE=/tmp/quality_state.json
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts

# Tier thresholds (matching quality_judge.sh)
MIN_OK=30      # below = "broken" tier — switch config
TARGET_OK=60   # above = "watchable" tier — declare progress
CABLE_OK=90    # above = "cable" tier — ultimate goal

STALL_SAMPLES=10  # 10 × 30s = 5 min below MIN_OK before switching
TARGET_SAMPLES=20 # 20 × 30s = 10 min above TARGET_OK = sustained good
SAMPLE_INTERVAL=30

# Each config: "label STVT_VAR=val ..."
CONFIGS=(
    "current_best STVT_SDR_AGC=1 STVT_RS=erasure STVT_RS_ERASURES=18 STVT_EQ=long STVT_VITERBI=soft STVT_EQ_BETA=2e-5"
    "leak_hi STVT_SDR_AGC=1 STVT_RS=erasure STVT_RS_ERASURES=18 STVT_EQ=long STVT_VITERBI=soft STVT_EQ_BETA=2e-5 STVT_EQ_LEAK=1e-3"
    "nb_on STVT_SDR_AGC=1 STVT_RS=erasure STVT_RS_ERASURES=18 STVT_EQ=long STVT_VITERBI=soft STVT_EQ_BETA=2e-5 STVT_NB=1"
    "no_agc_softeq STVT_SDR_AGC=0 STVT_RS=erasure STVT_RS_ERASURES=18 STVT_EQ=long STVT_VITERBI=soft STVT_EQ_BETA=2e-5"
    "hard_v_only STVT_SDR_AGC=1 STVT_RS=erasure STVT_RS_ERASURES=18 STVT_EQ=long STVT_VITERBI=hard"
    "low_rfgain STVT_SDR_AGC=1 STVT_RFGAIN_SEL=1 STVT_RS=erasure STVT_RS_ERASURES=18 STVT_EQ=long STVT_VITERBI=soft STVT_EQ_BETA=2e-5"
)

log() {
    echo "[$(date '+%H:%M:%S')] $*" >> "$LOG"
    echo "[$(date '+%H:%M:%S')] $*"
}

apply_config() {
    local label="$1"; shift
    local config="$*"
    log ">>> APPLYING $label: $config"

    # Pause watchdog (we manage chain ourselves during transition)
    local wd=$(pgrep -f stvt_play_watchdog | head -1)
    [ -n "$wd" ] && kill -STOP $wd

    # Kill chain
    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
    pkill -9 -f 'tv_live\.py.*--rf' 2>/dev/null
    # kill-ok: player/pipeline consumer (mpv/ffmpeg/tail), not the SDR holder
    pkill -9 -f 'stvt_play\.sh\|pipe_buffer\|ffmpeg.*libx264.*pipe:1\|mpv.*STVT Player\|tail.*live.ts' 2>/dev/null
    sleep 5
    rm -f "$LIVE_TS"

    # Spawn tv_live with config
    cd /home/user/Software-TV-Tuner
    nohup bash -c "
        source /tmp/auto_acquire_winner.env
        $(echo "$config" | tr ' ' '\n' | sed 's/^/export /')
        python3 tools/tv_live.py --rf 34
    " > /tmp/quality_controller_chain.log 2>&1 &
    disown

    # Wait for live.ts to be substantial
    local deadline=$(($(date +%s) + 120))
    while [ ! -f "$LIVE_TS" ] || [ "$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)" -lt 80000000 ]; do
        sleep 5
        if [ $(date +%s) -gt $deadline ]; then
            log "  chain didn't acquire within 120s, marking config as failed"
            break
        fi
    done

    # Resume watchdog so pipeline gets spawned
    [ -n "$wd" ] && kill -CONT $wd

    # Give chain another 60s to fully stabilize
    log "  applied. waiting 60s for stabilization."
    sleep 60
}

# Initialize: apply default best config
log "=== quality controller starting ==="
log "MIN_OK=$MIN_OK TARGET_OK=$TARGET_OK CABLE_OK=$CABLE_OK"

cur_idx=0
cur_label=$(echo "${CONFIGS[$cur_idx]}" | awk '{print $1}')
cur_cfg=$(echo "${CONFIGS[$cur_idx]}" | cut -d' ' -f2-)
apply_config "$cur_label" $cur_cfg

# Track moving average
scores=()
ma_window=5

below_count=0
above_count=0
total_samples=0
best_score=0
best_label="$cur_label"

while true; do
    sleep $SAMPLE_INTERVAL
    total_samples=$((total_samples + 1))

    # Run quality_judge
    /home/user/quality_judge.sh > /dev/null 2>&1
    score=$(awk -F'[: ,]' '/"score":/{for(j=1;j<=NF;j++)if($j~/^[0-9]+$/){print $j; exit}}' "$STATE_FILE")
    fps=$(awk -F'[:,]' '/"fps":/{gsub(/[" ]/,"",$2); print $2}' "$STATE_FILE")

    scores+=($score)
    if [ ${#scores[@]} -gt $ma_window ]; then
        scores=("${scores[@]:1}")
    fi

    # Compute moving average
    ma=$(echo "${scores[@]}" | awk '{sum=0; for(i=1;i<=NF;i++) sum+=$i; printf "%.0f", sum/NF}')

    log "sample $total_samples: cur=$cur_label score=$score fps=$fps ma=$ma  (below=$below_count above=$above_count)"

    if [ "$ma" -gt "$best_score" ]; then
        best_score=$ma
        best_label=$cur_label
        log "  new best: $best_label score=$best_score"
    fi

    if [ "$ma" -ge "$CABLE_OK" ]; then
        log "*** CABLE QUALITY REACHED ($ma >= $CABLE_OK) — done"
        break
    fi

    if [ "$ma" -ge "$TARGET_OK" ]; then
        above_count=$((above_count + 1))
        below_count=0
        if [ "$above_count" -ge "$TARGET_SAMPLES" ]; then
            log "*** SUSTAINED WATCHABLE QUALITY ($ma >= $TARGET_OK for $above_count samples) — stopping cycle"
            break
        fi
    elif [ "$ma" -lt "$MIN_OK" ]; then
        below_count=$((below_count + 1))
        above_count=0
        if [ "$below_count" -ge "$STALL_SAMPLES" ]; then
            log "  $below_count consecutive samples below $MIN_OK — switching config"
            cur_idx=$(( (cur_idx + 1) % ${#CONFIGS[@]} ))
            cur_label=$(echo "${CONFIGS[$cur_idx]}" | awk '{print $1}')
            cur_cfg=$(echo "${CONFIGS[$cur_idx]}" | cut -d' ' -f2-)
            apply_config "$cur_label" $cur_cfg
            scores=()
            below_count=0
            above_count=0
        fi
    else
        # Middling score (glitchy tier) — accumulate slowly
        if [ "$below_count" -gt 0 ]; then below_count=$((below_count - 1)); fi
        if [ "$above_count" -gt 0 ]; then above_count=$((above_count - 1)); fi
    fi
done

log "=== quality controller finished ==="
log "BEST: $best_label with MA score $best_score"
log "leaving chain in current state"
