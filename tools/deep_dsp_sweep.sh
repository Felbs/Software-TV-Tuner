#!/bin/bash
# deep_dsp_sweep.sh — vary the DSP "math" parameters and measure
# MPEG-2 marker recovery. The chain occasionally produces decodable
# video; goal is to find params that keep it decoding sustainedly.
#
# Strategy: hold EQ=long + RS=erasure as the baseline (best so far),
# then vary one math knob at a time so we can attribute wins.
#
# Each config: 25s dwell. Measure last 20MB of TS for:
#   seq_header (0x000001B3)  — required for mpv to start decode
#   GOP        (0x000001B8)  — required for keyframe sync
#   picture    (0x00000100)  — partial frame recovery indicator
# Winner: highest seq_header. Tiebreak: highest GOP, then picture.
#
# Writes /tmp/deep_dsp_sweep.csv with one row per config + winner pick.

set -u
RESULT=/tmp/deep_dsp_sweep.csv
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
DWELL=25
PP=/home/user/pp.sh

> "$RESULT"
echo "label,fpll_alpha,fpll_tau,agc_rate,sync_lock,sync_unlock,dcr_taps,conv_sec,nb_thresh,seq_h,gop,pic,ts_mb" >> "$RESULT"

# Each config is: LABEL FPLL_ALPHA FPLL_TAU AGC_RATE SYNC_LOCK SYNC_UNLOCK DCR_TAPS CONV_SEC NB_THRESH
# Defaults are: 0.003 20 1e-6 3.5 2.0 32 20 3.0
# (AGC_RATE not currently exposed via env in tv_live; included in CSV for analysis but ignored at runtime)
CONFIGS=(
    "baseline           0.003 20  1e-6 3.5 2.0 32 20 3.0"
    "fpll_wider         0.01  20  1e-6 3.5 2.0 32 20 3.0"
    "fpll_widest        0.03  20  1e-6 3.5 2.0 32 20 3.0"
    "fpll_tight         0.001 20  1e-6 3.5 2.0 32 20 3.0"
    "fpll_fast_afc      0.003 5   1e-6 3.5 2.0 32 20 3.0"
    "fpll_slow_afc      0.003 60  1e-6 3.5 2.0 32 20 3.0"
    "sync_aggressive    0.003 20  1e-6 2.5 1.5 32 20 3.0"
    "sync_conservative  0.003 20  1e-6 5.0 3.0 32 20 3.0"
    "dcr_off            0.003 20  1e-6 3.5 2.0 0  20 3.0"
    "dcr_long           0.003 20  1e-6 3.5 2.0 128 20 3.0"
    "conv_60            0.003 20  1e-6 3.5 2.0 32 60 3.0"
    "nb_aggressive      0.003 20  1e-6 3.5 2.0 32 20 1.5"
    "nb_off             0.003 20  1e-6 3.5 2.0 32 20 99"
    "fpll_wide_dcr_off  0.01  20  1e-6 3.5 2.0 0  20 3.0"
    "all_aggressive     0.01  10  1e-6 2.5 1.5 0  40 1.5"
)

kill_chain() {
    for p in $($PP chain) $($PP mpv) $($PP ffmpeg); do
        kill -9 $p 2>/dev/null
    done
    sleep 3
}

run_config() {
    local label="$1" alpha="$2" tau="$3" agc="$4" slock="$5" sunlock="$6" \
          dcr="$7" conv="$8" nb_t="$9"

    kill_chain
    rm -f "$LIVE_TS"

    cd /home/user
    nohup bash -c "
        source /tmp/auto_acquire_winner.env
        export STVT_EQ=long
        export STVT_RS=erasure
        export STVT_RS_ERASURES=14
        export STVT_FPLL_ALPHA=$alpha
        export STVT_FPLL_AFC_TAU=$tau
        export ATSC_SYNC_SOFT_LOCK=$slock
        export ATSC_SYNC_SOFT_UNLOCK=$sunlock
        export STVT_DCR_TAPS=$dcr
        export STVT_CONVERGENCE_SEC=$conv
        export STVT_NB=1
        export STVT_NB_THRESHOLD=$nb_t
        ~/run_stvt_winner.sh long 34
    " > /tmp/deep_sweep_chain.log 2>&1 &
    disown

    sleep "$DWELL"

    local sz=$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)
    local seq_h=0 gop=0 pic=0
    if [ "$sz" -gt 21000000 ]; then
        local TAIL_SZ=$((20*1024*1024))
        seq_h=$(tail -c $TAIL_SZ "$LIVE_TS" 2>/dev/null | grep -aoP '\x00\x00\x01\xb3' | wc -l)
        gop=$(tail -c $TAIL_SZ "$LIVE_TS" 2>/dev/null | grep -aoP '\x00\x00\x01\xb8' | wc -l)
        pic=$(tail -c $TAIL_SZ "$LIVE_TS" 2>/dev/null | grep -aoP '\x00\x00\x01\x00' | wc -l)
    fi
    local ts_mb=$((sz/1024/1024))
    echo "  $label  seq=$seq_h gop=$gop pic=$pic ts=${ts_mb}MB"
    echo "$label,$alpha,$tau,$agc,$slock,$sunlock,$dcr,$conv,$nb_t,$seq_h,$gop,$pic,$ts_mb" >> "$RESULT"
}

echo "=== deep DSP sweep starting ($(date '+%H:%M:%S')) ==="
echo "configs: ${#CONFIGS[@]} × ${DWELL}s = ~$(( ${#CONFIGS[@]} * (DWELL + 4) / 60 )) min"
echo ""

for cfg in "${CONFIGS[@]}"; do
    run_config $cfg
done

kill_chain

echo ""
echo "=== sorted by seq_header DESC ==="
{ head -1 "$RESULT"; tail -n +2 "$RESULT" | sort -t, -k10 -rn; } | column -t -s,

BEST=$(tail -n +2 "$RESULT" | sort -t, -k10 -rn -k11 -rn -k12 -rn | head -1)
BEST_SEQ=$(echo "$BEST" | awk -F, '{print $10}')
echo ""
if [ "${BEST_SEQ:-0}" -gt 0 ]; then
    echo "*** WINNER: $BEST"
    echo "*** seq_header count > 0 — video should DECODE with this config"
    echo ""
    # Write a winner env file for future use
    echo "$BEST" > /tmp/deep_sweep_winner.csv
    LABEL=$(echo "$BEST" | awk -F, '{print $1}')
    ALPHA=$(echo "$BEST" | awk -F, '{print $2}')
    TAU=$(echo "$BEST" | awk -F, '{print $3}')
    SLOCK=$(echo "$BEST" | awk -F, '{print $5}')
    SUNLOCK=$(echo "$BEST" | awk -F, '{print $6}')
    DCR=$(echo "$BEST" | awk -F, '{print $7}')
    CONV=$(echo "$BEST" | awk -F, '{print $8}')
    NB_T=$(echo "$BEST" | awk -F, '{print $9}')
    cat > /tmp/deep_sweep_winner.env <<EOF
# deep_dsp_sweep winner — $LABEL — $(date)
source /tmp/auto_acquire_winner.env
export STVT_EQ=long
export STVT_RS=erasure
export STVT_RS_ERASURES=14
export STVT_FPLL_ALPHA=$ALPHA
export STVT_FPLL_AFC_TAU=$TAU
export ATSC_SYNC_SOFT_LOCK=$SLOCK
export ATSC_SYNC_SOFT_UNLOCK=$SUNLOCK
export STVT_DCR_TAPS=$DCR
export STVT_CONVERGENCE_SEC=$CONV
export STVT_NB=1
export STVT_NB_THRESHOLD=$NB_T
EOF
    echo "winner env written to /tmp/deep_sweep_winner.env"
else
    echo "*** NO CONFIG produced sequence_header_code"
    echo "*** Best partial recovery: $BEST"
    echo "*** Will iterate further — adjust strategy based on which configs"
    echo "*** got highest picture/GOP counts."
fi
