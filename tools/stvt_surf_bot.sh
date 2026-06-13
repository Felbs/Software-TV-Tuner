#!/usr/bin/env bash
# stvt_surf_bot.sh — robot thumb for the channel surfer.
#
# Drives a RUNNING stvt_surf.sh through its control FIFO with realistic and
# adversarial press patterns — single presses, rapid bursts, presses landing
# mid-tune — and verifies after each burst that the surfer settles on the
# EXPECTED channel with a live, advancing player. Prints a scorecard.
#
# The burst patterns reproduce the measured 2026-06-13 user report ("pressed
# page up 3 times, stuck, then jumped 3 channels really fast") that the
# real-TV control-loop rewrite fixes: N quick presses must produce ONE tune
# to the net target.
#
# Usage:  tools/stvt_surf_bot.sh [cycles]      # default 15; surfer must be up
set -u
FIFO=/tmp/stvt_surf.fifo
LOG=/tmp/stvt_surf.log
MPVLOG=/tmp/stvt_surf_mpv.log
CYCLES="${1:-15}"

[ -p "$FIFO" ] || { echo "[bot] no surfer FIFO at $FIFO — start tools/stvt_surf.sh first"; exit 1; }
N=$(grep -oE 'loaded [0-9]+ channels' "$LOG" | tail -1 | grep -oE '[0-9]+' || true)
[ -n "$N" ] || { echo "[bot] cannot read channel count from $LOG"; exit 1; }

# Current (0-based) index from the surfer's last 'tuned [i/N]' line.
cur_idx(){
  local i
  i=$(grep -oE 'tuned \[[0-9]+/' "$LOG" | tail -1 | tr -dc '0-9/' | cut -d/ -f1)
  echo $(( ${i:-1} - 1 ))
}

EXP=$(cur_idx)
ok=0; bad=0; deaths=0; n_lat=0
lat_sum=0; lat_max=0; lat_min=9999

press(){ echo "$1" > "$FIFO"; }

# -a forces text mode: the mpv log contains binary OSD bytes and a bare
# grep prints 'binary file matches' instead of the line.
av_pos(){ tr '\r' '\n' < "$MPVLOG" 2>/dev/null | grep -a -oE '^AV: [0-9:]+' | tail -1; }

echo "[bot] $CYCLES cycles against $N channels, starting at index $((EXP+1))"
for c in $(seq 1 "$CYCLES"); do
  lines0=$(wc -l < "$LOG")
  pat=$(( RANDOM % 4 ))
  desc=""
  case "$pat" in
    0) press up;   EXP=$(( (EXP + 1) % N ));            desc="single up";;
    1) press down; EXP=$(( (EXP - 1 + N) % N ));        desc="single down";;
    2) k=$(( 2 + RANDOM % 3 ))                          # rapid burst of 2-4
       for i in $(seq 1 "$k"); do press up; sleep 0.12; done
       EXP=$(( (EXP + k) % N ));                        desc="burst of $k ups";;
    3) press up; sleep 1.2; press up; press up          # presses MID-TUNE
       EXP=$(( (EXP + 3) % N ));                        desc="3 ups straddling a tune";;
  esac
  t0=$(date +%s.%N)
  want=$(( EXP + 1 ))

  # Wait (<=75s) for the surfer to report it tuned the expected channel.
  landed=""
  deadline=$(( $(date +%s) + 75 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    landed=$(tail -n +"$(( lines0 + 1 ))" "$LOG" | grep -oE 'tuned \[[0-9]+/' | tail -1 | tr -dc '0-9/' | cut -d/ -f1)
    [ -n "$landed" ] && [ "$landed" = "$want" ] && break
    sleep 0.5
  done
  lat=$(awk -v a="$t0" -v b="$(date +%s.%N)" 'BEGIN{printf "%.1f", b-a}')

  if [ "${landed:-}" != "$want" ]; then
    echo "[bot] $c/$CYCLES  $desc -> FAIL: expected ch#$want, surfer reports '${landed:-none}' after ${lat}s"
    bad=$(( bad + 1 ))
    EXP=$(cur_idx)     # resync the model so one failure doesn't cascade
    continue
  fi

  # Player must be alive with playback advancing. Give mpv a fair start
  # window (probe + first frames take ~3-4s after the tune is reported).
  sleep 4
  a1=$(av_pos); sleep 4; a2=$(av_pos)
  if ! pgrep -x mpv >/dev/null; then
    echo "[bot] $c/$CYCLES  $desc -> ch#$want in ${lat}s but PLAYER DEAD"
    deaths=$(( deaths + 1 )); bad=$(( bad + 1 )); continue
  fi
  if [ -z "$a1" ] || [ "$a1" = "$a2" ]; then
    echo "[bot] $c/$CYCLES  $desc -> ch#$want in ${lat}s but playback NOT advancing ('$a1')"
    bad=$(( bad + 1 )); continue
  fi

  ok=$(( ok + 1 )); n_lat=$(( n_lat + 1 ))
  lat_sum=$(awk -v s="$lat_sum" -v l="$lat" 'BEGIN{printf "%.1f", s+l}')
  lat_max=$(awk -v m="$lat_max" -v l="$lat" 'BEGIN{print (l>m)?l:m}')
  lat_min=$(awk -v m="$lat_min" -v l="$lat" 'BEGIN{print (l<m)?l:m}')
  echo "[bot] $c/$CYCLES  $desc -> ch#$want OK in ${lat}s, playing"
done

echo "[bot] ===== SCORECARD ====="
echo "[bot] cycles: $CYCLES   ok: $ok   failed: $bad   player-deaths: $deaths"
if [ "$n_lat" -gt 0 ]; then
  echo "[bot] press->playing latency: min ${lat_min}s  avg $(awk -v s="$lat_sum" -v n="$n_lat" 'BEGIN{printf "%.1f", s/n}')s  max ${lat_max}s"
fi
[ "$bad" -eq 0 ] && echo "[bot] VERDICT: PASS" || echo "[bot] VERDICT: FAIL ($bad)"
