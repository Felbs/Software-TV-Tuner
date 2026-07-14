#!/usr/bin/env bash
# native_soak.sh — overnight/long soak for the native-Linux port.
#
# Mirrors the real Jellyfin use case: start the HDHomeRun server, keep ONE
# persistent client stream open (a "Jellyfin viewer"), and sample decode health
# from the growing live.ts every INTERVAL seconds. Answers "does it hold up as
# well as Windows (clean decode, no drift) over hours?" and catches droughts,
# stalls, overflow storms, and retune failures.
#
# Env: RF (default 36), INTERVAL (60s), DURATION_MIN (150), RETUNE_EVERY (0=off)
set -u
cd "$(dirname "$0")/.." || exit 1        # repo root (script lives in tools/)
RF="${RF:-36}"
INTERVAL="${INTERVAL:-60}"
DURATION_MIN="${DURATION_MIN:-150}"
RETUNE_EVERY="${RETUNE_EVERY:-0}"
ALT_RF="${ALT_RF:-34}"
PORT="${STVT_HDHR_PORT:-5006}"
LIVE="tools/data/tv_live/live.ts"
OUT="$HOME/native_sweep"; mkdir -p "$OUT"
REPORT="$OUT/SOAK.md"
CLOG="tools/data/tv_live/tv_tuner.tv_live.log"

pkill -f '[t]v_live.py' 2>/dev/null; pkill -f '[s]tvt_hdhr.py' 2>/dev/null; sleep 1

# start HDHomeRun server (it spawns the chain on first stream request)
STVT_HDHR_PORT="$PORT" nohup python3 -u tools/stvt_hdhr.py > "$OUT/soak_hdhr.log" 2>&1 &
HDHR=$!
sleep 2
# persistent "Jellyfin viewer": stream RF.1 to /dev/null, forever (auto-reconnect)
( while true; do curl -s "http://localhost:$PORT/stream/$RF/1" -o /dev/null; sleep 2; done ) &
WATCHER=$!

{
  echo "# Native soak — $(date '+%Y-%m-%d %H:%M')  RF$RF  every ${INTERVAL}s for ${DURATION_MIN}min"
  echo ""
  echo "| elapsed | ts_MB | growth_MB/s | TEI-bad% | uniq_PID | chain_CPU% | overflows | relocks | note |"
  echo "|--------:|------:|------------:|---------:|---------:|-----------:|----------:|--------:|------|"
} > "$REPORT"

iters=$(( DURATION_MIN * 60 / INTERVAL ))
prev_size=0
t0=$(date +%s)
for ((k=1; k<=iters; k++)); do
  sleep "$INTERVAL"
  now=$(date +%s); el=$(( now - t0 ))
  if [ "$RETUNE_EVERY" -gt 0 ] && [ $(( k % RETUNE_EVERY )) -eq 0 ]; then
    # retune test: point the watcher at ALT_RF briefly, then back
    curl -s --max-time 12 "http://localhost:$PORT/stream/$ALT_RF/1" -o /dev/null || true
    note="retune->RF$ALT_RF->back"
  else
    note=""
  fi
  python3 - "$LIVE" "$CLOG" "$prev_size" "$INTERVAL" "$el" "$note" >> "$REPORT" <<'PY'
import sys, os, subprocess
live, clog, prev, interval, el, note = sys.argv[1:7]
prev=int(prev); interval=float(interval); el=int(el)
try: sz=os.path.getsize(live)
except OSError: sz=0
growth=(sz-prev)/1e6/interval if prev else 0.0
# TEI-bad% over last 4MB, unique PIDs over last 2MB
tei=n=0; pids=set()
try:
    with open(live,'rb') as f:
        f.seek(max(0,sz-4_000_000)); d=f.read()
    i=d.find(b'\x47')
    while i>=0 and i+188<=len(d):
        if d[i]!=0x47: i+=1; continue
        if d[i+1]&0x80: tei+=1
        pids.add(((d[i+1]&0x1f)<<8)|d[i+2]); n+=1; i+=188
except OSError: pass
teip=100*tei/max(n,1)
# chain CPU%
cpu="?"
try:
    out=subprocess.check_output(["ps","-C","python3","-o","%cpu,cmd","--no-headers"],text=True)
    for ln in out.splitlines():
        if "tv_live.py" in ln: cpu=ln.split()[0]; break
except Exception: pass
# overflow + relock counts from chain log
ov=rl="?"
try:
    dd=open(clog,'rb').read()
    ov=dd.count(b'OsO'); rl=dd.count(b'relock')
except OSError: pass
drought=" DROUGHT" if len(pids)>150 else ("" if n else " NO-DATA")
print(f"| {el//60}m{el%60:02d}s | {sz/1e6:.0f} | {growth:.2f} | {teip:.3f} | {len(pids)} | {cpu} | {ov} | {rl} | {note}{drought} |")
PY
  # write size for next growth calc
  prev_size=$(stat -c%s "$LIVE" 2>/dev/null || echo 0)
done

echo "" >> "$REPORT"
echo "soak complete: $(date '+%H:%M')" >> "$REPORT"
kill $WATCHER $HDHR 2>/dev/null
pkill -f '[t]v_live.py' 2>/dev/null
