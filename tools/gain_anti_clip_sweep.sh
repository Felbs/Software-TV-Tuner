#!/bin/bash
# gain_anti_clip_sweep.sh — Hypothesis: chain crashes ~3 min in because
# SDR samples are clipping (max|x| > 1.0 in fpll log). Try lower-gain
# combos to keep samples in linear range.
#
# IFGR is "IF gain reduction" — HIGHER value = LESS gain = MORE headroom.
# RFGAIN_SEL is the RF preamp band; values 0..7, lower = less preamp gain.
#
# Each config runs for 180s to see if it survives past the 3-min mark.
# Measures seq_header count in the last 50MB (i.e. the period that
# matters — after the chain SHOULD have stabilized).

set -u
RESULT=/tmp/gain_anti_clip.csv
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
TVLIVE_LOG=/home/user/Software-TV-Tuner/tools/data/tv_live/tv_tuner.tv_live.log
DWELL=180
PP=/home/user/pp.sh

> "$RESULT"
echo "label,ifgr,rfgain,seq_h_50mb,gop_50mb,pic_50mb,oso_count,clip_max,ts_mb" >> "$RESULT"

CONFIGS=(
    "baseline       59 5"
    "more_reduce    63 5"
    "max_reduce     63 1"
    "less_rfgain    59 1"
    "least_gain     63 0"
    "mod_low        55 3"
    "way_lower      50 1"
)

kill_chain() {
    for p in $($PP chain) $($PP mpv) $($PP ffmpeg); do
        kill -9 $p 2>/dev/null
    done
    sleep 3
}

run_config() {
    local label="$1" ifgr="$2" rfg="$3"

    kill_chain
    rm -f "$LIVE_TS"
    rm -f "$TVLIVE_LOG"

    cd /home/user
    nohup bash -c "
        source /tmp/auto_acquire_winner.env
        export STVT_IFGR=$ifgr
        export STVT_RFGAIN_SEL=$rfg
        export STVT_EQ=long
        export STVT_RS=erasure
        export STVT_RS_ERASURES=14
        ~/run_stvt_winner.sh long 34
    " > /tmp/gain_sweep_chain.log 2>&1 &
    disown

    echo "  starting $label (IFGR=$ifgr RFGAIN_SEL=$rfg) — ${DWELL}s dwell"
    sleep "$DWELL"

    local sz=$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)
    local seq_h=0 gop=0 pic=0
    if [ "$sz" -gt 52428800 ]; then
        seq_h=$(tail -c 52428800 "$LIVE_TS" 2>/dev/null | grep -aoP '\x00\x00\x01\xb3' | wc -l)
        gop=$(tail -c 52428800 "$LIVE_TS" 2>/dev/null | grep -aoP '\x00\x00\x01\xb8' | wc -l)
        pic=$(tail -c 52428800 "$LIVE_TS" 2>/dev/null | grep -aoP '\x00\x00\x01\x00' | wc -l)
    fi
    local ts_mb=$((sz/1024/1024))
    # Count OsO events + max clip value
    local oso_count=$(grep -c '^OsO' "$TVLIVE_LOG" 2>/dev/null || echo 0)
    local clip_max=$(grep -oP 'max\|x\|=\K[0-9.]+' "$TVLIVE_LOG" 2>/dev/null | sort -rn | head -1)
    echo "    → seq=$seq_h gop=$gop pic=$pic OsO=$oso_count clip_max=$clip_max ts=${ts_mb}MB"
    echo "$label,$ifgr,$rfg,$seq_h,$gop,$pic,$oso_count,$clip_max,$ts_mb" >> "$RESULT"
}

echo "=== gain anti-clip sweep ($(date '+%H:%M:%S')) ==="
echo "configs: ${#CONFIGS[@]} × ${DWELL}s = ~$(( ${#CONFIGS[@]} * (DWELL + 4) / 60 )) min"
echo ""

for cfg in "${CONFIGS[@]}"; do
    run_config $cfg
done

kill_chain

echo ""
echo "=== RESULTS sorted by seq_header DESC ==="
{ head -1 "$RESULT"; tail -n +2 "$RESULT" | sort -t, -k4 -rn -k5 -rn -k6 -rn; } | column -t -s,

BEST=$(tail -n +2 "$RESULT" | sort -t, -k4 -rn -k5 -rn -k6 -rn | head -1)
BEST_SEQ=$(echo "$BEST" | awk -F, '{print $4}')
if [ "${BEST_SEQ:-0}" -gt 0 ]; then
    echo ""
    echo "*** SEQ_HEADER > 0!!! winner: $BEST"
    BEST_IFGR=$(echo "$BEST" | awk -F, '{print $2}')
    BEST_RFG=$(echo "$BEST" | awk -F, '{print $3}')
    cat > /tmp/gain_anti_clip_winner.env <<EOF
# gain_anti_clip_sweep winner — $(date)
source /tmp/auto_acquire_winner.env
export STVT_IFGR=$BEST_IFGR
export STVT_RFGAIN_SEL=$BEST_RFG
export STVT_EQ=long
export STVT_RS=erasure
export STVT_RS_ERASURES=14
EOF
    echo "winner env written to /tmp/gain_anti_clip_winner.env"
fi
