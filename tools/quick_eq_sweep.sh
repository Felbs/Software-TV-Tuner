#!/bin/bash
# quick_eq_sweep.sh — sweep EQ × antenna × RS combos against live SDR,
# measure MPEG-2 sequence_header count per 15s window, write CSV.
# Picks whichever config produces non-zero seq_headers.
#
# This bypasses auto_acquire's PAT-only verify (which passes configs
# that produce TS bytes but no usable video payload) by checking the
# specific marker mpv needs to decode: 0x000001B3.

set -u
RESULT_CSV=/tmp/quick_eq_sweep.csv
DWELL_SEC=18
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts

> "$RESULT_CSV"
echo "eq,rs,antenna,seq_h,gop,pic,ts_size" >> "$RESULT_CSV"

CONFIGS=(
    "long stock A"
    "long erasure A"
    "pilot_dd stock A"
    "multifs stock A"
    "long stock B"
    "long erasure B"
)

PP=/home/user/pp.sh

kill_all() {
    for p in $($PP chain) $($PP mpv) $($PP ffmpeg); do
        # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign); loop also sweeps mpv/ffmpeg players
        kill -9 $p 2>/dev/null
    done
    sleep 3
}

for cfg in "${CONFIGS[@]}"; do
    eq=$(echo $cfg | awk '{print $1}')
    rs=$(echo $cfg | awk '{print $2}')
    ant=$(echo $cfg | awk '{print $3}')
    ant_full="Antenna $ant"

    echo ""
    echo "=== eq=$eq rs=$rs antenna='$ant_full' ==="
    kill_all

    # Wipe live.ts so we start fresh (so we measure THIS config's output only)
    rm -f "$LIVE_TS"

    cd /home/user
    nohup bash -c "
        source /tmp/auto_acquire_winner.env
        export STVT_EQ=$eq
        export STVT_RS=$rs
        export STVT_ANTENNA='$ant_full'
        ~/run_stvt_winner.sh $eq 34
    " > /tmp/quick_eq_sweep_chain.log 2>&1 &
    disown

    # Wait DWELL_SEC for the chain to fill TS
    sleep $DWELL_SEC

    SZ=$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)
    if [ "$SZ" -lt 10000000 ]; then
        seq_h=0; gop=0; pic=0
        echo "  TS too small ($SZ bytes) — chain didn't acquire"
    else
        # Check last 20MB
        TAIL_SZ=$((20*1024*1024))
        seq_h=$(tail -c $TAIL_SZ "$LIVE_TS" 2>/dev/null | grep -aoP '\x00\x00\x01\xb3' | wc -l)
        gop=$(tail -c $TAIL_SZ "$LIVE_TS" 2>/dev/null | grep -aoP '\x00\x00\x01\xb8' | wc -l)
        pic=$(tail -c $TAIL_SZ "$LIVE_TS" 2>/dev/null | grep -aoP '\x00\x00\x01\x00' | wc -l)
        echo "  seq_header=$seq_h  GOP=$gop  picture=$pic  ts=$((SZ/1024/1024))MB"
    fi

    echo "$eq,$rs,$ant,$seq_h,$gop,$pic,$SZ" >> "$RESULT_CSV"
done

kill_all
echo ""
echo "=== RESULTS (sorted by seq_header) ==="
{ head -1 "$RESULT_CSV"; tail -n +2 "$RESULT_CSV" | sort -t, -k4 -rn; }
echo ""
# Print best config explicitly
BEST=$(tail -n +2 "$RESULT_CSV" | sort -t, -k4 -rn | head -1)
BEST_SH=$(echo "$BEST" | awk -F, '{print $4}')
if [ "${BEST_SH:-0}" -gt 0 ]; then
    echo "*** BEST: $BEST"
    echo "*** Will run: source winner.env; export EQ/RS/ANT from above; ~/run_stvt_winner.sh"
else
    echo "*** ALL CONFIGS PRODUCED 0 SEQUENCE HEADERS"
    echo "*** Conclusion: signal at antenna is below the decode floor"
    echo "*** for any chain config we have. Antenna or RF environment issue."
fi
