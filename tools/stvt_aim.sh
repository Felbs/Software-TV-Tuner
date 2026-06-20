#!/usr/bin/env bash
# stvt_aim — live antenna aim-assist. Run it WHILE the TV is playing
# (stvt_run.sh / the chain must already be up). It reads the decoder's
# live.ts growth rate — a real-time "how much of the picture is getting
# through" meter — and prints a live bar with a peak-hold marker, so you
# can move the antenna like tuning rabbit ears and watch the signal climb.
#
# Zero cost to the chain: it only stat()s the file (no ffprobe, no SDR) —
# so it never steals the decoder's CPU or causes a drought.
#
# Usage:  tools/stvt_aim.sh [interval_sec]   (Ctrl-C to stop)
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS="$HERE/data/tv_live/live.ts"
INT="${1:-2}"
REALTIME_BPS=2540000          # 100% real-time payload

[ -f "$TS" ] || { echo "no live.ts at $TS — is the chain running? (tools/stvt_run.sh <rf> <prog>)"; exit 1; }

echo "LIVE AIM-ASSIST — move the antenna slowly; hold where the bar peaks."
echo "  >=95% = excellent   85-95% = watchable   <85% = keep moving"
echo "  (TV keeps playing; Ctrl-C to stop)"
echo
peak=0; pk_age=0
trap 'echo; echo "best seen: ${peak}%"; exit 0' INT
while :; do
  s1=$(stat -c%s "$TS" 2>/dev/null || echo 0)
  sleep "$INT"
  s2=$(stat -c%s "$TS" 2>/dev/null || echo 0)
  d=$((s2 - s1))
  pct=$(awk -v d="$d" -v i="$INT" -v r="$REALTIME_BPS" 'BEGIN{p=d/i/r*100; print (p<0)?0:int(p)}')
  if [ "$pct" -gt "$peak" ]; then peak=$pct; pk_age=0; hint="<< NEW PEAK"; else
    pk_age=$((pk_age+1)); hint=""
  fi
  # 20-cell bar, colour-coded by tier
  bar=$(awk -v p="$pct" 'BEGIN{n=int(p/5); s=""; for(i=0;i<n&&i<20;i++)s=s"#"; for(i=n;i<20;i++)s=s"-"; print s}')
  tier="keep moving "; [ "$pct" -ge 85 ] && tier="watchable  "; [ "$pct" -ge 95 ] && tier="EXCELLENT  "
  printf "\r%3d%% |%s| %s peak=%d%% %-11s" "$pct" "$bar" "$tier" "$peak" "$hint"
done
