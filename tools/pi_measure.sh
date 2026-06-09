#!/bin/bash
# Pi throughput measurement harness.
# Usage: pi_measure.sh <rf> <secs> <label> [extra env assignments...]
# Prints real-time factor, OsO count, and TS PID health for one config.
set -u
RF="${1:?rf}"; SECS="${2:?secs}"; LABEL="${3:?label}"; shift 3

cd "$(dirname "$0")"
LOG="/tmp/pi_${LABEL}.log"
F="data/tv_live/live.ts"

# Graceful shutdown helper: SIGINT lets GNU Radio tear down and release the
# SDRplay device cleanly. A hard kill -9 leaves the sdrplay API claiming the
# device, so the next launch fails with "no available RSP devices".
stop_chain() {
  local pids; pids=$(pgrep -f 'python3 tv_live.py')
  [ -z "$pids" ] && return
  for p in $pids; do kill -INT "$p" 2>/dev/null; done
  for _ in 1 2 3 4 5 6; do
    pgrep -f 'python3 tv_live.py' >/dev/null || return
    sleep 1
  done
  for p in $(pgrep -f 'python3 tv_live.py'); do kill -9 "$p" 2>/dev/null; done
  sleep 1
}

stop_chain
rm -f "$F"

# base lean config; callers can override via extra "K=V" args
export STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long
export STVT_SPS=1.1 STVT_RRC_SYMS=4 STVT_TEISCRUB=0
export STVT_RXF_FUSED=1 STVT_ROTATE_GB=4
export STVT_IFGR=59 STVT_RFGAIN_SEL=5 STVT_ANTENNA="Antenna A"
for kv in "$@"; do export "$kv"; done

setsid python3 tv_live.py --rf "$RF" > "$LOG" 2>&1 &
sleep 8
grep -iE "equalizer:|fused_rx|FUSED rx|rx_filter:" "$LOG" | head -3

s1=$(stat -c%s "$F" 2>/dev/null || echo 0)
sleep "$SECS"
s2=$(stat -c%s "$F" 2>/dev/null || echo 0)

CPID=$(pgrep -f 'python3 tv_live.py' | head -1)
CPU=$(ps -o %cpu= -p "$CPID" 2>/dev/null | tr -d ' ')

python3 - "$s1" "$s2" "$SECS" "$LABEL" "$CPU" <<'PY'
import sys
s1,s2,secs=int(sys.argv[1]),int(sys.argv[2]),float(sys.argv[3])
label,cpu=sys.argv[4],sys.argv[5]
rt=(s2-s1)/secs/2.42e6*100
print(f"[{label}] real-time factor: {rt:.0f}%   (rate {(s2-s1)/secs/1e6:.2f} MB/s, CPU {cpu}%)")
PY
echo "[$LABEL] OsO: $(grep -c OsO "$LOG")"

# TS PID health on the tail
tail -c 3000000 "$F" 2>/dev/null | python3 -c '
import sys,collections
d=sys.stdin.buffer.read(); c=collections.Counter(); i=d.find(b"\x47")
while i>=0 and i+188<=len(d):
    if d[i]==0x47: c[((d[i+1]&0x1f)<<8)|d[i+2]]+=1; i+=188
    else: i=d.find(b"\x47",i)
tot=sum(c.values())
print("  TS unique PIDs: %d  (real mux ~25-35; hundreds = overflow garbage)"%len(c))
' 2>/dev/null

stop_chain
echo "[$LABEL] done"
