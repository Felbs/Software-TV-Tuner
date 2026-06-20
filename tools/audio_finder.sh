#!/usr/bin/env bash
# AUTOBOT: find which audio output actually reaches your speakers.
# Plays a labeled beep through every output device in turn. Listen, and
# note the NUMBER you hear sound on.
set -u

# device | human label  (each tone uses a different pitch so they're distinct)
DEVICES=(
  "pipewire/alsa_output.pci-0000_09_00.1.hdmi-stereo|GPU HDMI (pipewire default)"
  "alsa/hdmi:CARD=NVidia,DEV=0|NVidia HDMI endpoint 0"
  "alsa/hdmi:CARD=NVidia,DEV=1|NVidia HDMI endpoint 1"
  "alsa/hdmi:CARD=NVidia,DEV=2|NVidia HDMI endpoint 2"
  "alsa/hdmi:CARD=NVidia,DEV=3|NVidia HDMI endpoint 3"
  "pipewire/alsa_output.pci-0000_0b_00.3.analog-stereo|Onboard analog (green jack)"
)

echo "==================================================================="
echo " AUDIO OUTPUT FINDER — listen for a beep, note the NUMBER you hear"
echo "==================================================================="
i=0
for entry in "${DEVICES[@]}"; do
  i=$((i+1))
  dev="${entry%%|*}"; label="${entry#*|}"
  freq=$((300 + i*120))   # distinct pitch per device
  echo ""
  echo ">>> TEST $i — $label   (${freq} Hz, 3s)"
  mpv "av://lavfi:sine=frequency=${freq}:duration=3" \
      --audio-device="$dev" --no-video --really-quiet 2>/dev/null
  sleep 1
done
echo ""
echo "==================================================================="
echo " Done. Tell Claude the NUMBER(s) where you heard a beep."
echo "==================================================================="
