#!/bin/bash
# skip_resamp_test.sh — test STVT_SKIP_RESAMP=1 + STVT_NATIVE_RATE=6250000
# to eliminate the chain's software resampler. Hypothesis: the resampler
# block consumes CPU that occasionally causes the chain to fall behind the
# SDR sample rate, producing periodic OsO sample-overflow events. Without
# it, chain might keep up cleanly → fewer sample drops → fewer bit errors.

set -u
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
TVLIVE_LOG=/home/user/Software-TV-Tuner/tools/data/tv_live/tv_tuner.tv_live.log
DWELL=180
PP=/home/user/pp.sh

kill_chain() {
    for p in $($PP chain) $($PP mpv) $($PP ffmpeg); do
        kill -9 $p 2>/dev/null
    done
    sleep 3
}

run_config() {
    local label="$1" native_rate="$2" skip="$3"

    kill_chain
    rm -f "$LIVE_TS" "$TVLIVE_LOG"

    cd /home/user
    nohup bash -c "
        source /tmp/auto_acquire_winner.env
        export STVT_NATIVE_RATE=$native_rate
        export STVT_SKIP_RESAMP=$skip
        export STVT_EQ=long
        export STVT_RS=erasure
        export STVT_RS_ERASURES=14
        ~/run_stvt_winner.sh long 34
    " > /tmp/skip_resamp_chain.log 2>&1 &
    disown

    echo "=== $label (NATIVE=$native_rate SKIP=$skip) — ${DWELL}s ==="
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
    local oso=$(grep -c '^OsO' "$TVLIVE_LOG" 2>/dev/null | head -1)
    echo "  → seq=$seq_h gop=$gop pic=$pic OsO=$oso ts=${ts_mb}MB"
}

# Test 1: baseline (current behavior)
run_config "baseline_8M_with_resamp" 8000000 0

# Test 2: native 6.25M, skip resampler
run_config "native_6.25M_no_resamp" 6250000 1

# Test 3: native 8M, but ALSO with skip (would fail — keep for diagnostic)
# Skipped — would just produce wrong rate downstream

kill_chain
