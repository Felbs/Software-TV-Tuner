#!/usr/bin/env bash
# stvt_run.sh — one command to watch live TV, with hands-off recovery.
#
# Supervises BOTH halves of the live pipeline:
#   * the decoder chain (tv_live.py)  — restarts it if it dies OR slips into a
#     "noise drought" (locks the carrier but decodes garbage: the live edge shows
#     hundreds/thousands of unique PIDs instead of ~20-40). This is the recurring
#     OsO-accumulation failure on slower CPUs.
#   * the HD player (stvt_play_hd.sh) — which itself supervises mpv and exits when
#     the chain goes down; this script brings it back once the chain is healthy.
#
# Bounded: at most MAX_CHAIN_RESTARTS chain restarts per run, with a cooldown, so
# a genuinely dead SDR can't cause an infinite respawn storm.
#
# Usage:  tools/stvt_run.sh [rf] [program]
#   rf      : ATSC RF channel (default 34)
#   program : MPEG-TS program to play, 1080 HD (default 3)
#
# Stop everything:  pkill -f stvt_run.sh ; pkill -f tv_live.py ; pkill -f stvt_play_hd.sh
set -u
RF="${1:-34}"
PROG="${2:-3}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS="$HERE/data/tv_live/live.ts"
CLOG="$HERE/data/tv_live/tv_tuner.tv_live.log"
RUNLOG="/tmp/stvt_run.log"
MAX_CHAIN_RESTARTS=30
COOLDOWN=5
DROUGHT_PIDS=150            # unique-PID count above this = candidate drought
DROUGHT_STRIKES="${DROUGHT_STRIKES:-3}"  # consecutive high samples (~2s apart) before restart — filters transient relock spikes

# Lean real-time config (good for modest CPUs; harmless on fast ones).
export STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long
export STVT_SPS="${STVT_SPS:-1.1}" STVT_RRC_SYMS="${STVT_RRC_SYMS:-4}" STVT_TEISCRUB="${STVT_TEISCRUB:-0}"
export STVT_IFGR="${STVT_IFGR:-59}" STVT_RFGAIN_SEL="${STVT_RFGAIN_SEL:-5}" STVT_ANTENNA="${STVT_ANTENNA:-Antenna A}"

log(){ echo "$(printf '%(%H:%M:%S)T' -1) $*" | tee -a "$RUNLOG" ; }

chain_up(){ pgrep -f '[t]v_live.py' >/dev/null; }
player_up(){ pgrep -f '[s]tvt_play_hd.sh' >/dev/null; }

start_chain(){
  rm -f "$TS"
  [ -f "$CLOG" ] && mv "$CLOG" "$CLOG.$(printf '%(%H%M%S)T' -1)" 2>/dev/null
  ( cd "$HERE" && setsid python3 tv_live.py --rf "$RF" > "$CLOG" 2>&1 < /dev/null & )
  log "started chain (RF$RF, lean config)"
}

start_player(){
  ( setsid "$HERE/stvt_play_hd.sh" "$PROG" 25 >/dev/null 2>&1 < /dev/null & )
  log "started player supervisor (prog $PROG)"
}

# unique PIDs in the last 2 MB of live.ts (drought detector). 0 if no/short file.
unique_pids(){
  tail -c 2000000 "$TS" 2>/dev/null | python3 -c '
import sys
d=sys.stdin.buffer.read(); s=set(); i=d.find(b"\x47")
while i>=0 and i+188<=len(d):
    if d[i]==0x47: s.add(((d[i+1]&0x1f)<<8)|d[i+2]); i+=188
    else: i+=1
print(len(s))' 2>/dev/null || echo 0
}

# Single-instance guard. Two supervisors fight: when one restarts the chain on
# a drought, the other sees the gap as a fresh drought and restarts too, and
# they cascade into a restart storm (observed contaminating a stress run). Take
# an exclusive lock; a second invocation refuses rather than dueling.
LOCK="/tmp/stvt_run.lock"
exec 9>"$LOCK" || { echo "cannot open lock $LOCK" >&2; exit 3; }
if ! flock -n 9; then
  echo "stvt_run.sh is already running (lock $LOCK held). Refusing a 2nd instance." >&2
  echo "Stop the existing one first: pkill -f stvt_run.sh" >&2
  exit 3
fi

restarts=0
log "=== stvt_run starting (RF$RF, prog $PROG) ==="
# Adopt an already-running healthy chain/player instead of restarting them.
if chain_up; then log "adopting running chain"; else start_chain; sleep 8; fi
player_up || start_player

while true; do
  # 1. chain dead?
  if ! chain_up; then
    restarts=$((restarts+1))
    [ "$restarts" -gt "$MAX_CHAIN_RESTARTS" ] && { log "MAX_CHAIN_RESTARTS hit — giving up (check the SDR)"; exit 1; }
    log "chain DOWN — restart #$restarts"
    sleep "$COOLDOWN"; start_chain; sleep 10; continue
  fi

  # 2. noise drought? CONFIRM before acting. A single high PID sample is almost
  #    always a transient relock spike on an otherwise-healthy chain — measured:
  #    11/12 chains killed by the old single-sample check were at 99.8-99.99%
  #    segs_aligned (perfect decode). A REAL drought stays high across repeated
  #    samples; a transient clears within seconds. Require DROUGHT_STRIKES hits.
  if [ -f "$TS" ] && [ "$(stat -c%s "$TS" 2>/dev/null || echo 0)" -gt 3000000 ]; then
    u=$(unique_pids)
    if [ "$u" -gt "$DROUGHT_PIDS" ]; then
      hits=1
      for _ in $(seq 2 "$DROUGHT_STRIKES"); do
        sleep 2; v=$(unique_pids); [ "${v:-0}" -gt "$DROUGHT_PIDS" ] && hits=$((hits+1))
      done
      if [ "$hits" -ge "$DROUGHT_STRIKES" ]; then
        restarts=$((restarts+1))
        [ "$restarts" -gt "$MAX_CHAIN_RESTARTS" ] && { log "MAX_CHAIN_RESTARTS hit — giving up"; exit 1; }
        log "NOISE DROUGHT (sustained ${hits}/${DROUGHT_STRIKES}, ${u}+ PIDs) — restart chain #$restarts"
        pkill -f '[t]v_live.py'; sleep "$COOLDOWN"; start_chain; sleep 10; continue
      else
        log "transient PID spike (${u} PIDs, ${hits}/${DROUGHT_STRIKES}) — chain healthy, NOT restarting"
      fi
    fi
  fi

  # 3. player supervisor not running (e.g. it exited when the chain bounced)?
  if ! player_up; then start_player; fi

  sleep 20
done
