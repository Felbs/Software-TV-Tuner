#!/usr/bin/env bash
# stvt_stress_monitor.sh — low-overhead live-TV stress monitor.
#
# Watches a running stvt_run.sh session (chain + player) for a set
# duration and logs every glitch: noise droughts, chain restarts,
# player relaunches, playback freezes, and SDR sample-overflow (OsO)
# accumulation — then prints a report you can use to drive fixes.
#
# IMPORTANT: this reads only the logs the pipeline ALREADY writes
# (stvt_run.log, the mpv status log, the chain log). It does NOT
# re-sample live.ts or re-decode video, because that load would steal
# CPU from the single-thread matched filter and CAUSE the very droughts
# we're trying to measure. Cost is a few cheap greps every 30 s.
#
# Usage:  tools/stvt_stress_monitor.sh [minutes]   (default 60)
# Report: /tmp/stvt_stress_report.txt   (live timeline: /tmp/stvt_stress_events.log)
set -u
DUR_MIN="${1:-60}"
INTERVAL="${STVT_MON_INTERVAL:-30}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNLOG=/tmp/stvt_run.log
MPVLOG=/tmp/stvt_mpv.log
SUPLOG=/tmp/stvt_play_hd.sup.log
CLOG="$HERE/data/tv_live/tv_tuner.tv_live.log"
REPORT=/tmp/stvt_stress_report.txt
EVENTS=/tmp/stvt_stress_events.log

ts(){ date '+%H:%M:%S'; }
now(){ date +%s; }

# current mpv playback position (whole seconds) or "" if none
av_pos(){ grep -oE 'AV: [0-9:]+' "$MPVLOG" 2>/dev/null | tail -1 | grep -oE '[0-9:]+$' \
          | awk -F: '{n=NF; print (n==3?$1*3600+$2*60+$3:$1*60+$2)}'; }
cnt(){ grep -c "$1" "$2" 2>/dev/null || echo 0; }

start=$(now); end=$(( start + DUR_MIN*60 ))
b_drought=$(cnt "NOISE DROUGHT" "$RUNLOG")
b_chain=$(cnt "started chain" "$RUNLOG")
b_relaunch=$(cnt "relaunch #" "$SUPLOG")
b_oso=$(cnt "OsO" "$CLOG")

: > "$EVENTS"
echo "[$(ts)] stress monitor START — ${DUR_MIN} min, sampling every ${INTERVAL}s" | tee "$EVENTS"

last_pos="$(av_pos)"; freeze_streak=0; freeze_events=0; longest_freeze=0
samples=0; playing_samples=0
p_drought=$b_drought; p_chain=$b_chain; p_relaunch=$b_relaunch

while [ "$(now)" -lt "$end" ]; do
  sleep "$INTERVAL"
  samples=$((samples+1))
  c_drought=$(cnt "NOISE DROUGHT" "$RUNLOG")
  c_chain=$(cnt "started chain" "$RUNLOG")
  c_relaunch=$(cnt "relaunch #" "$SUPLOG")
  pos="$(av_pos)"

  [ "$c_drought" -gt "$p_drought" ] && echo "[$(ts)] DROUGHT (noise) — chain restarting" >> "$EVENTS"
  [ "$c_chain"   -gt "$p_chain"   ] && echo "[$(ts)] CHAIN RESTART" >> "$EVENTS"
  [ "$c_relaunch" -gt "$p_relaunch" ] && echo "[$(ts)] PLAYER RELAUNCH" >> "$EVENTS"

  # freeze detection: mpv up but position not advancing
  if pgrep -x mpv >/dev/null; then
    if [ -n "$pos" ] && [ -n "$last_pos" ] && [ "$pos" -le "$last_pos" ]; then
      freeze_streak=$((freeze_streak+1))
      [ "$freeze_streak" -eq 1 ] && echo "[$(ts)] FREEZE start (pos stuck at ${pos}s)" >> "$EVENTS"
    else
      if [ "$freeze_streak" -ge 1 ]; then
        dur=$(( freeze_streak*INTERVAL ))
        [ "$dur" -gt "$longest_freeze" ] && longest_freeze=$dur
        freeze_events=$((freeze_events+1))
        echo "[$(ts)] FREEZE end (~${dur}s)" >> "$EVENTS"
      fi
      freeze_streak=0
      playing_samples=$((playing_samples+1))
    fi
  else
    echo "[$(ts)] mpv DOWN" >> "$EVENTS"
  fi
  last_pos="$pos"
  p_drought=$c_drought; p_chain=$c_chain; p_relaunch=$c_relaunch
done

# trailing freeze
if [ "$freeze_streak" -ge 1 ]; then
  dur=$(( freeze_streak*INTERVAL )); freeze_events=$((freeze_events+1))
  [ "$dur" -gt "$longest_freeze" ] && longest_freeze=$dur
fi

f_drought=$(( $(cnt "NOISE DROUGHT" "$RUNLOG") - b_drought ))
f_chain=$(( $(cnt "started chain" "$RUNLOG") - b_chain ))
f_relaunch=$(( $(cnt "relaunch #" "$SUPLOG") - b_relaunch ))
f_oso=$(( $(cnt "OsO" "$CLOG") - b_oso ))
elapsed_min=$(( ($(now)-start)/60 ))
uptime_pct=0; [ "$samples" -gt 0 ] && uptime_pct=$(( playing_samples*100/samples ))
mtbf="n/a"; [ "$f_drought" -gt 0 ] && mtbf="$(( elapsed_min / f_drought )) min"

{
  echo "================ STVT STRESS REPORT ================"
  echo "duration        : ${elapsed_min} min   (samples: ${samples} @ ${INTERVAL}s)"
  echo "video uptime     : ~${uptime_pct}%  (samples with playback advancing)"
  echo "noise droughts   : ${f_drought}   (mean time between: ${mtbf})"
  echo "chain restarts   : ${f_chain}"
  echo "player relaunches: ${f_relaunch}"
  echo "playback freezes : ${freeze_events}   (longest ~${longest_freeze}s)"
  echo "OsO (overflow)   : +${f_oso} over the run"
  echo "---- final chain health (last FPLL line) ----"
  grep fpll "$CLOG" 2>/dev/null | tail -1
  echo "---- event timeline ----"
  cat "$EVENTS"
  echo "===================================================="
} > "$REPORT"
echo "[$(ts)] stress monitor DONE — report at $REPORT" >> "$EVENTS"
cat "$REPORT"
