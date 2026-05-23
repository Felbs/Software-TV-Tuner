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
STATE=/tmp/quality_state.json
LIVE_TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
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
    if [ ! -f "$LIVE_TS" ] || [ "$(stat -c%s "$LIVE_TS" 2>/dev/null || echo 0)" -lt 50000000 ]; then
        emit 0 "broken" 0 0 0 0 "live.ts missing or <50MB"
        rm -f "$tmp_log"
        return
    fi

    # Decode the LATEST $WINDOW seconds worth of TS. The chain writes
    # ~1.5MB/s, so window*1.5MB is recent. Use ffmpeg's seek-to-end-relative
    # trick: -sseof negates EOF-relative seek (negative = from end).
    timeout $((WINDOW + 15)) ffmpeg -hide_banner -loglevel info \
        -fflags +genpts+igndts -err_detect ignore_err \
        -sseof -$((WINDOW * 2)) \
        -f mpegts -i "$LIVE_TS" \
        -map "0:p:$PROGRAM:v?" -map "0:p:$PROGRAM:a?" \
        -t $WINDOW -f null - 2>"$tmp_log"

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
    # - Subtract v_eps (video errors per sec) up to 30 points.
    # - Subtract a_eps × 2 up to 20 points.
    # - Cap at 100.
    local score=$(awk "BEGIN{
        s = $fps * 3;
        s -= (v_eps_var=$v_eps); if (v_eps_var > 30) s -= 30 - v_eps_var;
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
