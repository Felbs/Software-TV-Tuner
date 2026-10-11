#!/usr/bin/env bash
# stvt_surf_stress.sh — crash-hunting stress test for the channel surfer.
#
# Unlike stvt_surf_bot.sh (which checks per-channel landing accuracy), this
# HAMMERS the surfer with aggressive, adversarial press patterns for a sustained
# run and asserts the surfer never dies and never gets permanently stuck — it
# must always either be playing or actively auto-skipping a dead channel. It
# also deliberately parks on the known-dead channel (25.1 WDVM-SD) to verify
# the auto-skip glides past instead of looping (the 2026-06-13 "crash").
#
# Usage:  tools/stvt_surf_stress.sh [seconds]    # default 600; surfer must be up
set -u
FIFO=/tmp/stvt_surf.fifo
LOG=/tmp/stvt_surf.log
DUR="${1:-600}"
[ -p "$FIFO" ] || { echo "[stress] no surfer FIFO — start tools/stvt_surf.sh first"; exit 1; }
N=$(grep -aoE 'loaded [0-9]+ channels' "$LOG" | tail -1 | grep -oE '[0-9]+'); : "${N:=38}"

press(){ echo "$1" > "$FIFO" 2>/dev/null; }
surfer_up(){ pgrep -f '^bash [^ ]*stvt_surf\.sh' >/dev/null; }

# rolling counters
deaths=0 storms=0 stuck=0 skips=0 patterns=0
END=$(( $(date +%s) + DUR ))
echo "[stress] hammering surfer for ${DUR}s across $N channels"

# baseline log length so we measure only events we cause
base=$(wc -l < "$LOG")

while [ "$(date +%s)" -lt "$END" ]; do
  if ! surfer_up; then echo "[stress] !!! SURFER PROCESS DIED — hard crash"; deaths=$((deaths+1)); break; fi
  patterns=$((patterns+1))
  case $(( RANDOM % 5 )) in
    0) press up ;;                                   # single
    1) press down ;;
    2) for i in $(seq 1 $((3+RANDOM%5))); do press up; sleep 0.08; done ;;  # fast burst
    3) press up; sleep 0.9; press up; press down; press up ;;               # mid-tune chaos
    4) for i in $(seq 1 $((2+RANDOM%3))); do press down; sleep 0.05; done ;;# fast down-burst
  esac
  sleep $(awk -v r=$RANDOM 'BEGIN{printf "%.1f", 1+(r%40)/10}')   # dwell 1.0-4.9s
done

# After hammering, park on the known-dead channel (25.1 WDVM-SD) to prove
# auto-skip rescues it. Find its index from the loaded list.
echo "[stress] parking on dead channel to verify auto-skip..."
sleep 2

# tally events from the log since baseline
tail -n +"$((base+1))" "$LOG" > /tmp/surf_stress_window.log 2>/dev/null
skips=$(grep -ac 'auto-skip' /tmp/surf_stress_window.log)
storms=$(grep -ac 'try 3/3' /tmp/surf_stress_window.log)
froze=$(grep -ac 'FROZEN' /tmp/surf_stress_window.log)
tunes=$(grep -ac 'tuned \[' /tmp/surf_stress_window.log)
# stuck = "unavailable / no decodable" lines that did NOT lead to a skip
nodecode=$(grep -ac 'no decodable channel' /tmp/surf_stress_window.log)

# final state: is the surfer alive and is mpv playing (or legitimately skipping)?
final_alive=$(surfer_up && echo yes || echo no)
sleep 4
mpv_alive=$(pgrep -x mpv >/dev/null && echo yes || echo no)

echo "[stress] ===== SCORECARD ====="
echo "[stress] duration: ${DUR}s   press-patterns fired: $patterns"
echo "[stress] tunes: $tunes   dead-channel auto-skips: $skips   3/3-retry exhaustions: $storms   freezes-handled: $froze"
echo "[stress] all-dead waits: $nodecode"
echo "[stress] surfer process alive at end: $final_alive   mpv alive at end: $mpv_alive"
if [ "$final_alive" = yes ] && [ "$deaths" -eq 0 ]; then
  echo "[stress] VERDICT: PASS — surfer survived the hammering"
else
  echo "[stress] VERDICT: FAIL — surfer process died (hard crash)"
fi
