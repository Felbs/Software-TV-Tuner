#!/usr/bin/env bash
# stvt_audio_meter.sh — objective "does the audio play smoothly?" meter.
#
# Records what is CURRENTLY playing (the PulseAudio sink monitor) and counts
# dropouts = silence gaps, which is exactly what a stuttering/skipping player
# sounds like. A player must already be running and producing sound.
#
# This is the "ears" for the audio autotuner — lets the agent test player
# configs without a human listening.
#
# Usage:  tools/stvt_audio_meter.sh [seconds]
# Env:    STVT_PULSE_MON  (default RDPSink.monitor — WSLg's sink monitor)
#         STVT_AUDIO_STATE (default /tmp/stvt_audio_state.json)
# Output: one JSON line + writes it to the state file:
#   {"seconds":N,"dropouts":N,"silence_s":F,"silence_pct":F,"mean_db":F,"score":N}
set -u
SECS="${1:-15}"
JSON="${STVT_AUDIO_STATE:-/tmp/stvt_audio_state.json}"
MON="${STVT_PULSE_MON:-RDPSink.monitor}"
export PULSE_SERVER="${PULSE_SERVER:-unix:/mnt/wslg/PulseServer}"

WAV="$(mktemp /tmp/audmeter_XXXXXX.wav)"
timeout "$((SECS + 5))" ffmpeg -hide_banner -loglevel error \
  -f pulse -i "$MON" -t "$SECS" -ac 2 "$WAV" -y 2>/dev/null

# Silence gaps >= 0.2s below -45 dBFS = the player pausing/skipping.
det="$(ffmpeg -hide_banner -i "$WAV" \
        -af silencedetect=n=-45dB:d=0.2 -f null - 2>&1)"
drops="$(printf '%s\n' "$det" | grep -c silence_start)"
totsil="$(printf '%s\n' "$det" | grep -oP 'silence_duration: \K[0-9.]+' \
          | awk '{s+=$1} END{printf "%.2f", s+0}')"
mean="$(ffmpeg -hide_banner -i "$WAV" -af volumedetect -f null - 2>&1 \
        | grep -oP 'mean_volume: \K[-0-9.]+' | head -1)"
rm -f "$WAV"

read -r pct score <<EOF
$(awk -v sec="$SECS" -v sil="${totsil:-0}" -v d="${drops:-0}" 'BEGIN{
    silfrac = (sec>0)? sil/sec : 1;
    s = 100 - silfrac*100 - d*2;     # silence fraction dominates; -2/dropout
    if (s<0) s=0; if (s>100) s=100;
    printf "%.1f %d", silfrac*100, s }')
EOF

printf '{"seconds":%s,"dropouts":%s,"silence_s":%s,"silence_pct":%s,"mean_db":%s,"score":%s}\n' \
  "$SECS" "${drops:-0}" "${totsil:-0}" "$pct" "${mean:-0}" "$score" | tee "$JSON"
