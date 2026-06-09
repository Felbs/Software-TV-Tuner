#!/bin/bash
# Launch the chain and report per-thread CPU mapped to GNU Radio block names.
# The thread pinned near 100% (one full core) is the real-time limiter.
# Usage: pi_profile.sh <rf> [extra env K=V ...]
set -u
RF="${1:?rf}"; shift || true
cd "$(dirname "$0")"
LOG=/tmp/pi_profile.log

stop_chain() {
  local pids; pids=$(pgrep -f 'python3 tv_live.py')
  [ -z "$pids" ] && return
  for p in $pids; do kill -INT "$p" 2>/dev/null; done
  for _ in 1 2 3 4 5 6; do pgrep -f 'python3 tv_live.py' >/dev/null || return; sleep 1; done
  for p in $(pgrep -f 'python3 tv_live.py'); do kill -9 "$p" 2>/dev/null; done; sleep 1
}
stop_chain

export STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long
export STVT_SPS=1.1 STVT_RRC_SYMS=4 STVT_TEISCRUB=0
export STVT_RXF_FUSED=1 STVT_ROTATE_GB=4
export STVT_IFGR=59 STVT_RFGAIN_SEL=5 STVT_ANTENNA="Antenna A"
for kv in "$@"; do export "$kv"; done

setsid python3 tv_live.py --rf "$RF" > "$LOG" 2>&1 &
sleep 18

# real python pid (not the bash wrapper): comm==python3 and cmdline has tv_live
CPID=""
for p in $(pgrep -f 'tv_live.py'); do
  [ "$(cat /proc/$p/comm 2>/dev/null)" = "python3" ] && CPID=$p && break
done
echo "chain pid: ${CPID:-NONE}"
[ -z "$CPID" ] && { tail -5 "$LOG"; exit 1; }

# sample per-thread CPU over a 4s window using /proc jiffies
declare -A t0 nm
clk=$(getconf CLK_TCK)
for t in /proc/$CPID/task/*; do
  tid=$(basename $t); s=$(cat $t/stat 2>/dev/null) || continue
  # utime=14 stime=15 after the (comm) field; strip comm to keep field nums stable
  rest=${s#*) }; set -- $rest
  t0[$tid]=$(( ${12} + ${13} )); nm[$tid]=$(cat $t/comm 2>/dev/null)
done
sleep 4
echo "=== per-thread CPU over 4s (one core = 100%); limiter = highest ==="
for t in /proc/$CPID/task/*; do
  tid=$(basename $t); s=$(cat $t/stat 2>/dev/null) || continue
  rest=${s#*) }; set -- $rest
  now=$(( ${12} + ${13} )); d=$(( now - ${t0[$tid]:-0} ))
  pct=$(( d * 100 / clk / 4 ))
  [ "$pct" -gt 0 ] && echo "$pct% ${nm[$tid]}"
done | sort -rn | head -14
stop_chain
