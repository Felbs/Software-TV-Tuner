#!/bin/bash
# quality_judge.sh — measures video + audio quality of live.ts and emits
# a score 0-100. Writes /tmp/quality_state.json so a test harness or the
# bot can read it instead of asking the user.
#
# Method:
#   1. ffmpeg null-decode a 15-second slice of live.ts (recent tail)
#   2. Count video frames actually decoded -> approximate fps
#   3. Count video decode errors (ac-tex damaged / MVs not available)
#   4. Count audio decode errors (AC3 'Error submitting packet')
#   5. Combine into score:
#       cable_quality (90+): 27+ fps, <5 video errors/sec, 0 audio errors
#       watchable (60-89):   20-27 fps, <20 video errors/sec, occasional audio
#       glitchy (30-59):     10-20 fps, many video errors, audio broken
#       broken (0-29):       <10 fps or 0 fps
#
# Usage:
#   ~/quality_judge.sh                       # one-shot measure (writes JSON)
#   ~/quality_judge.sh --loop                # loop forever, sample every 30s
#   ~/quality_judge.sh --window 30           # measure 30s window (default 15)
#
# Output JSON shape:
#   { "ts": "HH:MM:SS",
#     "score": int,
#     "tier": "cable_quality|watchable|glitchy|broken",
#     "fps": float,
#     "video_errors_per_sec": float,
#     "audio_errors_per_sec": float,
#     "decoded_frames": int,
#     "window_sec": int,
#     "reason": "human" }

set -u
STATE="${STVT_QUALITY_STATE:-/tmp/quality_state.json}"
# Repo-relative by default (this script lives in tools/), override with
# STVT_LIVE_TS. Was hardcoded to /home/user — broke on every other box.
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIVE_TS="${STVT_LIVE_TS:-$_HERE/data/tv_live/live.ts}"
WINDOW=15
LOOP=0
PROGRAM="${STVT_PROGRAM:-3}"

while [ $# -gt 0 ]; do
    case "$1" in
        --loop) LOOP=1; shift ;;
        --window) WINDOW=$2; shift 2 ;;
        --program) PROGRAM=$2; shift 2 ;;
        *) shift ;;
    esac
done

measure_once() {
    local tmp_log=$(mktemp /tmp/quality_judge_XXXXXX.log)
    # Bytes for ~WINDOW+6 s of a ~19.39 Mbps ATSC mux (~2.5 MB/s).
    local chunk_bytes=$(( (WINDOW + 6) * 2500000 ))
    if [ ! -f "$LIVE_TS" ] || [ "$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)" -lt "$chunk_bytes" ]; then
        emit 0 "broken" 0 0 0 0 "live.ts missing or too small for a ${WINDOW}s window"
        rm -f "$tmp_log"
        return
    fi

    # Sample the LIVE EDGE reliably: copy the most-recent chunk to a
    # COMPLETE static file, skip a 2s lead-in (start on a clean GOP, not a
    # mid-write region), then decode WINDOW seconds. ffmpeg's -sseof on a
    # GROWING ts lands on the same partial chunk every call and invents
    # decode "errors" (identical readings, phantom v/a errors) — copying a
    # settled chunk fixes that.
    local recent=$(mktemp /tmp/qj_recent_XXXXXX.ts)
    tail -c "$chunk_bytes" "$LIVE_TS" > "$recent"
    timeout $((WINDOW + 20)) ffmpeg -hide_banner -loglevel info \
        -err_detect ignore_err -ss 2 \
        -i "$recent" \
        -map "0:p:$PROGRAM:v?" -map "0:p:$PROGRAM:a?" \
        -t $WINDOW -f null - 2>"$tmp_log"
    rm -f "$recent"

    # Count metrics from ffmpeg's stderr
    local frames=$(grep -oP 'frame=\s*\K[0-9]+' "$tmp_log" | tail -1)
    [ -z "$frames" ] && frames=0
    local v_errors=$(grep -cE '(ac-tex damaged|MVs not available|mb incr damaged|Invalid mb type|end mismatch|skipped MB|motion_type)' "$tmp_log")
    local a_errors=$(grep -cE '(ac3|ac3_fixed).*Error|Error submitting.*ac3|exponent.*out-of-range|coupling' "$tmp_log")

    local fps=$(awk "BEGIN{printf \"%.2f\", $frames / $WINDOW}")
    local v_eps=$(awk "BEGIN{printf \"%.2f\", $v_errors / $WINDOW}")
    local a_eps=$(awk "BEGIN{printf \"%.2f\", $a_errors / $WINDOW}")

    # Score formula:
    # - Start with fps × 3 (90 = perfect 30fps).
    # - Subtract video errors/sec, capped at 30 points.
    # - Subtract audio errors/sec × 2, capped at 20 points.
    # - Clamp to 0-100.
    local score=$(awk "BEGIN{
        s = $fps * 3;
        ve = $v_eps; if (ve > 30) ve = 30; s -= ve;
        ae = $a_eps * 2; if (ae > 20) ae = 20; s -= ae;
        if (s > 100) s = 100;
        if (s < 0) s = 0;
        printf \"%d\", s
    }")
    # Tier
    local tier="broken"
    if [ "$score" -ge 90 ]; then tier="cable_quality"
    elif [ "$score" -ge 60 ]; then tier="watchable"
    elif [ "$score" -ge 30 ]; then tier="glitchy"
    fi

    local reason="$frames frames in ${WINDOW}s = ${fps}fps; video_err/s=${v_eps}; audio_err/s=${a_eps}"
    emit "$score" "$tier" "$fps" "$v_eps" "$a_eps" "$frames" "$reason"

    rm -f "$tmp_log"
}

emit() {
    local score="$1" tier="$2" fps="$3" v_eps="$4" a_eps="$5" frames="$6" reason="$7"
    cat > "$STATE" <<EOF
{
  "ts": "$(date '+%H:%M:%S')",
  "score": $score,
  "tier": "$tier",
  "fps": $fps,
  "video_errors_per_sec": $v_eps,
  "audio_errors_per_sec": $a_eps,
  "decoded_frames": $frames,
  "window_sec": $WINDOW,
  "reason": "$reason"
}
EOF
    echo "[$(date '+%H:%M:%S')] score=$score tier=$tier fps=$fps v_err=$v_eps/s a_err=$a_eps/s"
}

if [ $LOOP -eq 1 ]; then
    while true; do
        measure_once
        sleep 30
    done
else
    measure_once
fi
