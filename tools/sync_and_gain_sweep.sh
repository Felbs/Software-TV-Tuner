#!/bin/bash
# sync_and_gain_sweep.sh — iteration 2 of DSP sweep, based on findings
# from deep_dsp_sweep.sh. If baseline produces pic>0 but seq_header=0,
# the bottleneck is likely sync/equalizer transient behavior, not FPLL.
# This sweep varies:
#   - SDR gain (IFGR, RFGAIN_SEL) — different operating points
#   - sync_soft thresholds (STICKY, ADAPTIVE, separate ACQUIRE vs STEADY)
#   - sync_soft timing scale
#   - sync_soft alpha (loop bandwidth)
#
# Holds the rest at deep_sweep baseline.

set -u
RESULT=/tmp/sync_gain_sweep.csv
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
DWELL=25
PP=/home/user/pp.sh

> "$RESULT"
echo "label,ifgr,rfgain,sync_alpha,sync_sticky,sync_timing_scale,sync_adaptive,seq_h,gop,pic,ts_mb" >> "$RESULT"

# Each: LABEL IFGR RFGAIN SYNC_ALPHA SYNC_STICKY TIMING_SCALE SYNC_ADAPTIVE
CONFIGS=(
    "baseline           59 5 0.01  1 1.0 1"
    "ifgr_low           45 5 0.01  1 1.0 1"
    "ifgr_lower         35 5 0.01  1 1.0 1"
    "ifgr_high          63 5 0.01  1 1.0 1"
    "rfgain_lo          59 1 0.01  1 1.0 1"
    "rfgain_med         59 3 0.01  1 1.0 1"
    "rfgain_hi          59 7 0.01  1 1.0 1"
    "sync_loose_alpha   59 5 0.05  1 1.0 1"
    "sync_tight_alpha   59 5 0.002 1 1.0 1"
    "sync_no_sticky     59 5 0.01  0 1.0 1"
    "sync_timing_slow   59 5 0.01  1 0.5 1"
    "sync_timing_fast   59 5 0.01  1 2.0 1"
    "sync_no_adaptive   59 5 0.01  1 1.0 0"
    "lo_gain_loose_sync 45 3 0.05  1 1.0 1"
    "hi_gain_tight_sync 63 7 0.002 1 1.0 1"
)

kill_chain() {
    for p in $($PP chain) $($PP mpv) $($PP ffmpeg); do
        # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign); loop also sweeps mpv/ffmpeg players
        kill -9 $p 2>/dev/null
    done
    sleep 3
}

run_config() {
    local label="$1" ifgr="$2" rfg="$3" sync_a="$4" sticky="$5" \
          tscale="$6" adapt="$7"

    kill_chain
    rm -f "$LIVE_TS"

    cd /home/user
    nohup bash -c "
        source /tmp/auto_acquire_winner.env
        export STVT_IFGR=$ifgr
        export STVT_RFGAIN_SEL=$rfg
        export STVT_EQ=long
        export STVT_RS=erasure
        export STVT_RS_ERASURES=14
        export ATSC_SYNC_SOFT_ALPHA=$sync_a
        export ATSC_SYNC_SOFT_STICKY=$sticky
        export ATSC_SYNC_SOFT_TIMING_SCALE=$tscale
        export ATSC_SYNC_SOFT_ADAPTIVE=$adapt
        ~/run_stvt_winner.sh long 34
    " > /tmp/sync_gain_sweep_chain.log 2>&1 &
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
    echo "$label,$ifgr,$rfg,$sync_a,$sticky,$tscale,$adapt,$seq_h,$gop,$pic,$ts_mb" >> "$RESULT"
}

echo "=== sync_and_gain sweep ($(date '+%H:%M:%S')) ==="
echo "configs: ${#CONFIGS[@]} × ${DWELL}s = ~$(( ${#CONFIGS[@]} * (DWELL + 4) / 60 )) min"
echo ""

for cfg in "${CONFIGS[@]}"; do
    run_config $cfg
done

kill_chain

echo ""
echo "=== TOP BY seq_header (then gop, then pic) ==="
{ head -1 "$RESULT"; tail -n +2 "$RESULT" | sort -t, -k8 -rn -k9 -rn -k10 -rn; } | column -t -s,

BEST=$(tail -n +2 "$RESULT" | sort -t, -k8 -rn -k9 -rn -k10 -rn | head -1)
BEST_SEQ=$(echo "$BEST" | awk -F, '{print $8}')

if [ "${BEST_SEQ:-0}" -gt 0 ]; then
    echo "*** WINNER WITH seq_header > 0 — that decodes!"
    echo "$BEST"
fi
