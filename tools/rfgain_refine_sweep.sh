#!/bin/bash
# rfgain_refine_sweep.sh — iteration 2 based on gain_anti_clip results.
# RFGAIN_SEL=1 (vs 5) DOUBLED picture-marker recovery (3232 vs 1646)
# at IFGR=59. This narrows in on the gain regime that works:
#   - finer RFGAIN_SEL grid (0, 1, 2, 3)
#   - try with SDR hardware AGC enabled (STVT_SDR_AGC=1) for comparison
#   - try slightly different IFGR (55, 57, 59) at RFGAIN_SEL=1

set -u
RESULT=/tmp/rfgain_refine.csv
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
TVLIVE_LOG=/home/user/Software-TV-Tuner/tools/data/tv_live/tv_tuner.tv_live.log
DWELL=180
PP=/home/user/pp.sh

> "$RESULT"
echo "label,ifgr,rfgain,sdr_agc,seq_h,gop,pic,oso,clip_max,ts_mb" >> "$RESULT"

# label IFGR RFGAIN SDR_AGC
CONFIGS=(
    "winner_so_far    59 1 0"
    "rfgain_0         59 0 0"
    "rfgain_2         59 2 0"
    "rfgain_3         59 3 0"
    "ifgr_55_rfg_1    55 1 0"
    "ifgr_57_rfg_1    57 1 0"
    "ifgr_60_rfg_1    60 1 0"
    "sdr_agc_on       59 5 1"
    "sdr_agc_rfg_1    59 1 1"
)

kill_chain() {
    for p in $($PP chain) $($PP mpv) $($PP ffmpeg); do
        # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign); loop also sweeps mpv/ffmpeg players
        kill -9 $p 2>/dev/null
    done
    sleep 3
}

run_config() {
    local label="$1" ifgr="$2" rfg="$3" agc="$4"

    kill_chain
    rm -f "$LIVE_TS" "$TVLIVE_LOG"

    cd /home/user
    nohup bash -c "
        source /tmp/auto_acquire_winner.env
        export STVT_IFGR=$ifgr
        export STVT_RFGAIN_SEL=$rfg
        export STVT_SDR_AGC=$agc
        export STVT_EQ=long
        export STVT_RS=erasure
        export STVT_RS_ERASURES=14
        ~/run_stvt_winner.sh long 34
    " > /tmp/rfgain_refine_chain.log 2>&1 &
    disown

    echo "  starting $label (IFGR=$ifgr RFG=$rfg AGC=$agc)"
    sleep "$DWELL"

    local sz=$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)
    local seq_h=0 gop=0 pic=0
    if [ "$sz" -gt 52428800 ]; then
        seq_h=$(tail -c 52428800 "$LIVE_TS" 2>/dev/null | grep -aoP '\x00\x00\x01\xb3' | wc -l)
        gop=$(tail -c 52428800 "$LIVE_TS" 2>/dev/null | grep -aoP '\x00\x00\x01\xb8' | wc -l)
        pic=$(tail -c 52428800 "$LIVE_TS" 2>/dev/null | grep -aoP '\x00\x00\x01\x00' | wc -l)
    fi
    local ts_mb=$((sz/1024/1024))
    local oso=$(grep -c '^OsO' "$TVLIVE_LOG" 2>/dev/null | head -1)
    [ -z "$oso" ] && oso=0
    local clip_max=$(grep -oP 'max\|x\|=\K[0-9.]+' "$TVLIVE_LOG" 2>/dev/null | sort -rn | head -1)
    [ -z "$clip_max" ] && clip_max=0
    echo "    → seq=$seq_h gop=$gop pic=$pic OsO=$oso clip_max=$clip_max ts=${ts_mb}MB"
    echo "$label,$ifgr,$rfg,$agc,$seq_h,$gop,$pic,$oso,$clip_max,$ts_mb" >> "$RESULT"
}

echo "=== rfgain refine sweep ($(date '+%H:%M:%S')) ==="
for cfg in "${CONFIGS[@]}"; do
    run_config $cfg
done

kill_chain

echo ""
echo "=== sorted by seq_header then pic ==="
{ head -1 "$RESULT"; tail -n +2 "$RESULT" | sort -t, -k5 -rn -k7 -rn; } | column -t -s,
