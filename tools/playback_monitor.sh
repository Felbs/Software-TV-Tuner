#!/bin/bash
# playback_monitor.sh — continuously observe whether STVT video is
# actually playing and write structured state to /tmp/playback_state.json
# so the bot can poll it instead of asking the user.
#
# State values:
#   PLAYING   mpv reading >50 KB/s for last 2 samples
#   STUTTER   mpv reading >0 but <50 KB/s (probably cache-paused but
#             receiving short bursts — sometimes precedes freeze)
#   FROZEN    mpv reading 0 KB/s for 2+ consecutive 6s samples
#   DEAD      mpv process missing
#   NO_CHAIN  chain process missing
#   BAD_BITS  chain alive + writing TS, but live.ts has zero MPEG-2
#             sequence headers in last 50 MB — SDR firmware producing
#             garbage. Per memory, needs physical USB replug.
#
# JSON shape:
#   { "ts": "HH:MM:SS",
#     "state": "PLAYING|STUTTER|FROZEN|DEAD|NO_CHAIN|BAD_BITS",
#     "mpv_kbps": int,
#     "ts_growth_kbps": int,
#     "seq_headers_50mb": int,
#     "reason": "human-readable explanation",
#     "consecutive_frozen": int }
#
# Run:  nohup ~/playback_monitor.sh >/dev/null 2>&1 & disown
# Read: cat /tmp/playback_state.json

set -u
STATE=/tmp/playback_state.json
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
PP=/home/user/pp.sh

SAMPLE_SEC=6
SEQHEADER_CHECK_EVERY=10   # every Nth cycle, check seq headers (expensive grep)
cycle=0
consecutive_frozen=0
last_seqh=-1

emit() {
    local state="$1" kbps="$2" growth="$3" seqh="$4" reason="$5"
    cat > "$STATE" <<EOF
{
  "ts": "$(date '+%H:%M:%S')",
  "state": "$state",
  "mpv_kbps": $kbps,
  "ts_growth_kbps": $growth,
  "seq_headers_50mb": $seqh,
  "reason": "$reason",
  "consecutive_frozen": $consecutive_frozen
}
EOF
}

while true; do
    cycle=$((cycle + 1))

    # Sample chain + mpv state
    CHAIN_PIDS=$($PP chain)
    MPV=$($PP mpv | head -1)

    if [ -z "$CHAIN_PIDS" ]; then
        consecutive_frozen=0
        emit "NO_CHAIN" 0 0 "$last_seqh" "no tv_live/tv_tuner process running"
        sleep "$SAMPLE_SEC"
        continue
    fi

    if [ -z "$MPV" ]; then
        consecutive_frozen=0
        emit "DEAD" 0 0 "$last_seqh" "chain alive but no mpv process"
        sleep "$SAMPLE_SEC"
        continue
    fi

    # mpv read rate over SAMPLE_SEC
    R1=$(awk '/^rchar/{print $2}' /proc/$MPV/io 2>/dev/null)
    SZ1=$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)
    sleep "$SAMPLE_SEC"
    R2=$(awk '/^rchar/{print $2}' /proc/$MPV/io 2>/dev/null)
    SZ2=$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)

    if [ -z "$R2" ]; then
        # mpv died during sample
        consecutive_frozen=0
        emit "DEAD" 0 0 "$last_seqh" "mpv died mid-sample"
        continue
    fi

    mpv_kbps=$(( (R2 - R1) / SAMPLE_SEC / 1024 ))
    ts_kbps=$(( (SZ2 - SZ1) / SAMPLE_SEC / 1024 ))

    # Periodic seq_header check — only when chain has had time to fill
    # the TS, and not every cycle (expensive grep on 50MB).
    if [ $((cycle % SEQHEADER_CHECK_EVERY)) -eq 1 ] && [ "$SZ2" -gt 52428800 ]; then
        last_seqh=$(tail -c 52428800 "$LIVE_TS" 2>/dev/null \
            | grep -aoP '\x00\x00\x01\xb3' | wc -l)
    fi

    # Classify
    if [ "$ts_kbps" -gt 100 ] && [ "$last_seqh" = "0" ]; then
        consecutive_frozen=0
        emit "BAD_BITS" "$mpv_kbps" "$ts_kbps" "$last_seqh" \
            "chain writing TS at ${ts_kbps}KB/s but 0 MPEG-2 seq headers — SDR firmware bad (per memory: replug needed)"
    elif [ "$mpv_kbps" -gt 50 ]; then
        consecutive_frozen=0
        emit "PLAYING" "$mpv_kbps" "$ts_kbps" "$last_seqh" \
            "mpv reading ${mpv_kbps}KB/s, chain ${ts_kbps}KB/s"
    elif [ "$mpv_kbps" -gt 0 ]; then
        emit "STUTTER" "$mpv_kbps" "$ts_kbps" "$last_seqh" \
            "mpv reading only ${mpv_kbps}KB/s (cache likely paused, sometimes precedes freeze)"
    else
        consecutive_frozen=$((consecutive_frozen + 1))
        emit "FROZEN" "$mpv_kbps" "$ts_kbps" "$last_seqh" \
            "mpv 0 KB/s for ${consecutive_frozen} consecutive ${SAMPLE_SEC}s samples (chain ${ts_kbps}KB/s — bytes available but not consumed)"
    fi
done
