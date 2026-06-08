#!/usr/bin/env bash
# stvt_drought_forensics.sh — OBSERVE (never restart) a running chain so a noise
# drought persists for inspection. Logs unique-PID count + the latest carrier
# (fpll) metrics every INTERVAL, and on the clean->drought transition dumps the
# fpll trace around the moment of collapse.
#
# Goal: see whether a drought is a CARRIER problem (max|x| clipping spike,
# in_rms signal loss, NCO frequency jump) or a DOWNSTREAM decode collapse
# (carrier stays clean while live.ts output turns to noise).
#
# Run alongside a chain started DIRECTLY (tv_live.py), NOT via stvt_run.sh —
# so nothing kills the chain when it droughts.
#
# Usage: tools/stvt_drought_forensics.sh [minutes] [interval_s]
set -u
DUR_MIN="${1:-25}"
INTERVAL="${2:-12}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS="$HERE/data/tv_live/live.ts"
CLOG="$HERE/data/tv_live/tv_tuner.tv_live.log"
OUT=/tmp/stvt_drought_forensics.log

# unique PIDs in the last 512 KB of live.ts (light: half the watchdog's window)
pids(){ tail -c 524288 "$TS" 2>/dev/null | python3 -c '
import sys
d=sys.stdin.buffer.read(); s=set(); i=d.find(b"\x47")
while i>=0 and i+188<=len(d):
    if d[i]==0x47: s.add(((d[i+1]&0x1f)<<8)|d[i+2]); i+=188
    else: i+=1
print(len(s))' 2>/dev/null; }

end=$(( $(date +%s) + DUR_MIN*60 ))
echo "# time,pids,state,  fpll_metrics" > "$OUT"
prev=clean; drought_seen=0
while [ "$(date +%s)" -lt "$end" ]; do
  u=$(pids); u=${u:-0}
  fp=$(grep '\[fpll' "$CLOG" 2>/dev/null | tail -1 | sed -E 's/.*\] //')
  state=clean; [ "$u" -gt 150 ] && state=DROUGHT
  printf '%s,%s,%s,  %s\n' "$(date +%H:%M:%S)" "$u" "$state" "$fp" | tee -a "$OUT"
  if [ "$state" = DROUGHT ] && [ "$prev" = clean ]; then
    drought_seen=$((drought_seen+1))
    {
      echo ">>> DROUGHT ONSET #$drought_seen at $(date +%H:%M:%S) (pids=$u) — fpll trace around collapse:"
      grep '\[fpll' "$CLOG" 2>/dev/null | tail -20
      echo "<<< end onset dump"
    } >> "$OUT"
  fi
  prev=$state
  sleep "$INTERVAL"
done
echo "# forensics done ($drought_seen drought onsets captured)" | tee -a "$OUT"
