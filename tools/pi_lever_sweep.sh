#!/usr/bin/env bash
# Document the Pi's BEST achievable real-time factor by sweeping runtime levers
# on the DETERMINISTIC replay (no SDR; reproducible). For each config: replay the
# IQ, record real-time factor + a decode-quality proxy (PAT present? unique PIDs).
# The "best" config is the FASTEST one that still decodes (PAT + sane PID count).
#
# Won't cross the ~0.46x Pi-4 ceiling (core-count floor) — this nails down the
# exact operating point and confirms no runtime knob beats it. Only runtime knobs
# (no rebuilds): FUSED, SPS, RRC_SYMS, EQ. ~25 min.
#
# Usage: pi_lever_sweep.sh <iq_file> <results_csv>
set -u
IQ="${1:?iq file}"
OUT="${2:-$HOME/pi_autobot/sweep_results.csv}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
mkdir -p "$(dirname "$OUT")"

DUR=$(python3 -c "import os;print(os.path.getsize('$IQ')/64e6)")
echo "fused,sps,rrc,eq,rt_factor,wall_s,pat,unique_pids" > "$OUT"
echo "[sweep] IQ=$IQ  signal=${DUR}s  -> $OUT"

# focused grid of runtime knobs
for FUSED in 1 0; do
for EQ in long stock; do
for SPS in 1.1 1.2 1.3; do
for RRC in 3 4 6; do
  export STVT_RS=stock STVT_VITERBI=hard STVT_TEISCRUB=0
  export STVT_EQ="$EQ" STVT_SPS="$SPS" STVT_RRC_SYMS="$RRC" STVT_RXF_FUSED="$FUSED"
  TS="/tmp/sweep_$$.ts"
  SECONDS=0
  timeout 150 python3 tv_replay.py --iq "$IQ" --out "$TS" --log /tmp/sweep_$$.log >/dev/null 2>&1
  WALL=$SECONDS
  RT=$(python3 -c "print('%.3f' % ($DUR/$WALL))" 2>/dev/null || echo 0)
  read PAT PIDS < <(tail -c 3000000 "$TS" 2>/dev/null | python3 -c '
import sys,collections
d=sys.stdin.buffer.read(); c=collections.Counter(); i=d.find(b"\x47")
while i>=0 and i+188<=len(d):
    if d[i]==0x47: c[((d[i+1]&0x1f)<<8)|d[i+2]]+=1; i+=188
    else: i=d.find(b"\x47",i)
print(int(0 in c), len(c))' 2>/dev/null || echo "0 0")
  echo "$FUSED,$SPS,$RRC,$EQ,$RT,$WALL,$PAT,$PIDS" >> "$OUT"
  echo "[sweep] fused=$FUSED eq=$EQ sps=$SPS rrc=$RRC -> RT=${RT}x  PAT=$PAT  PIDs=$PIDS"
  rm -f "$TS" /tmp/sweep_$$.log
done; done; done; done

echo "[sweep] === TOP 8 by real-time factor (PAT=1 = still decodes) ==="
# rank: decoding configs first (PAT=1), then by RT factor desc
{ head -1 "$OUT"; tail -n +2 "$OUT" | sort -t, -k7,7nr -k5,5nr | head -8; } | column -t -s,
echo "[sweep] full results: $OUT"
