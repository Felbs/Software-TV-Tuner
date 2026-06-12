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
mkdir -p "$HERE/data/tv_live"   # fresh checkout: chain dies instantly if its log dir is missing
RUNLOG="/tmp/stvt_run.log"
MAX_CHAIN_RESTARTS=30
COOLDOWN=5
DROUGHT_PIDS=150            # unique-PID count above this = candidate drought
DROUGHT_STRIKES="${DROUGHT_STRIKES:-3}"  # consecutive high samples (~2s apart) to confirm a drought (filters transient relock spikes)
# The chain now self-heals most droughts internally (atsc_fs_checker re-acquire,
# commit 7d79758) in ~12-24s. So don't restart on the FIRST confirmed drought —
# wait DROUGHT_GRACE_LOOPS consecutive ~20s checks (give the in-chain recovery
# time), then restart only if it truly hasn't recovered. 1 = old behavior.
DROUGHT_GRACE_LOOPS="${DROUGHT_GRACE_LOOPS:-2}"

# Lean real-time config, Pi 5 flavor (2026-06-12). The Pi 5 decodes live at
# ~1.1x real-time ONLY with all three of: the fused front-end, the int16 NEON
# equalizer, and enlarged front-end buffers (STVT_MIN_BUF_BYTES — GR's default
# 32KB buffers run the 4-core chain in lockstep at 0.91x). IFGR=50 is the
# DVR-validated gain for this antenna (59 was the old Ryzen-era value).
export STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long
export STVT_SPS="${STVT_SPS:-1.1}" STVT_RRC_SYMS="${STVT_RRC_SYMS:-4}" STVT_TEISCRUB="${STVT_TEISCRUB:-0}"
export STVT_IFGR="${STVT_IFGR:-50}" STVT_RFGAIN_SEL="${STVT_RFGAIN_SEL:-5}" STVT_ANTENNA="${STVT_ANTENNA:-Antenna A}"
export STVT_RXF_FUSED="${STVT_RXF_FUSED:-1}" STVT_EQ_S16="${STVT_EQ_S16:-1}"
export STVT_MIN_BUF_BYTES="${STVT_MIN_BUF_BYTES:-8388608}"

log(){ echo "$(printf '%(%H:%M:%S)T' -1) $*" | tee -a "$RUNLOG" ; }

# Optional CPU isolation (STVT_CPU_ISOLATE=1): born-pin the chain to dedicated
# cores and the player off them, so the chain's single-thread bottleneck is
# never preempted (preemption -> pipeline stall -> SDR overflow -> drought).
# Persists across restarts because each spawn is taskset'd. Ryzen 1600X layout;
# override via STVT_CHAIN_CPUS/STVT_OTHER_CPUS/STVT_SDR_CPUS. See
# tools/stvt_cpu_isolate.sh for a live/one-shot version + the core map.
CPU_ISOLATE="${STVT_CPU_ISOLATE:-0}"
ISO_CHAIN_CPUS="${STVT_CHAIN_CPUS:-0-3,6-9}"
ISO_OTHER_CPUS="${STVT_OTHER_CPUS:-5,11}"
ISO_SDR_CPUS="${STVT_SDR_CPUS:-4,10}"
TSET_CHAIN=""; TSET_OTHER=""
if [ "$CPU_ISOLATE" = 1 ]; then
  TSET_CHAIN="taskset -c $ISO_CHAIN_CPUS"
  TSET_OTHER="taskset -c $ISO_OTHER_CPUS"
fi

chain_up(){ pgrep -f '[t]v_live.py' >/dev/null; }
player_up(){ pgrep -f '[s]tvt_play_hd.sh' >/dev/null; }

start_chain(){
  rm -f "$TS"
  [ -f "$CLOG" ] && mv "$CLOG" "$CLOG.$(printf '%(%H%M%S)T' -1)" 2>/dev/null
  # exec 9>&- closes the inherited single-instance lock fd so the detached
  # chain (and its descendants) don't hold the lock after THIS supervisor exits.
  ( exec 9>&- 2>/dev/null; cd "$HERE" && setsid $TSET_CHAIN python3 tv_live.py --rf "$RF" > "$CLOG" 2>&1 < /dev/null & )
  log "started chain (RF$RF, lean config${TSET_CHAIN:+, cpus $ISO_CHAIN_CPUS})"
}

start_player(){
  # close the inherited lock fd (see start_chain) so the detached player tree
  # (stvt_play_hd.sh -> tail|ffmpeg|mpv) doesn't keep the lock held.
  ( exec 9>&- 2>/dev/null; setsid $TSET_OTHER "$HERE/stvt_play_hd.sh" "$PROG" 25 >/dev/null 2>&1 < /dev/null & )
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
drought_loops=0          # consecutive checks the output has been a confirmed drought
clean_streak=0           # consecutive checks the output has been clean
# MAX_CHAIN_RESTARTS is a RATE guard (catch a dead-SDR respawn storm), NOT a
# lifetime budget — otherwise a long night of occasional droughts exhausts it and
# the watchdog gives up for good (observed: hit the cap at ~3h, dead until morning).
# So reset the budget once the chain has run clean for RESTART_BUDGET_RESET checks
# (~3 min): a genuinely-recovered chain shouldn't be charged for past droughts.
RESTART_BUDGET_RESET="${RESTART_BUDGET_RESET:-9}"
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
    clean_streak=0
    sleep "$COOLDOWN"; start_chain; sleep 10; last_size=0; stall_loops=0; continue
  fi

  # 1b. chain WEDGED? Alive but live.ts not growing. A healthy chain writes
  #     ~2.4 MB/s continuously, so zero growth across STALL_LOOPS checks (~60s,
  #     far longer than the <1s rotation reset) means the flowgraph is stuck
  #     (e.g. file_sink blocked -> backpressure -> all threads at 0% CPU). The
  #     drought detector can't catch this — it's skipped while the file is tiny.
  #     Conservative threshold so a rotation can't false-trip it.
  STALL_LOOPS="${STALL_LOOPS:-3}"
  cur_size=$(stat -c%s "$TS" 2>/dev/null || echo 0)
  if [ "$cur_size" -le "${last_size:-0}" ]; then
    stall_loops=$((${stall_loops:-0}+1))
    if [ "$stall_loops" -ge "$STALL_LOOPS" ]; then
      restarts=$((restarts+1))
      [ "$restarts" -gt "$MAX_CHAIN_RESTARTS" ] && { log "MAX_CHAIN_RESTARTS hit — giving up (check the SDR)"; exit 1; }
      log "chain WEDGED — live.ts hasn't grown in ~$((stall_loops*20))s (size $cur_size) — restart #$restarts"
      pkill -f '[t]v_live.py'; sleep "$COOLDOWN"; start_chain; sleep 10
      last_size=0; stall_loops=0; clean_streak=0; continue
    fi
  else
    stall_loops=0
  fi
  last_size=$cur_size

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
        drought_loops=$((drought_loops+1))
        if [ "$drought_loops" -ge "$DROUGHT_GRACE_LOOPS" ]; then
          restarts=$((restarts+1))
          [ "$restarts" -gt "$MAX_CHAIN_RESTARTS" ] && { log "MAX_CHAIN_RESTARTS hit — giving up"; exit 1; }
          log "NOISE DROUGHT persisted ${drought_loops} checks (~$((drought_loops*20))s, ${u}+ PIDs) — in-chain re-acquire didn't recover; restart #$restarts"
          pkill -f '[t]v_live.py'; sleep "$COOLDOWN"; start_chain; sleep 10; drought_loops=0; clean_streak=0; last_size=0; stall_loops=0; continue
        else
          log "drought (check ${drought_loops}/${DROUGHT_GRACE_LOOPS}, ${u} PIDs) — letting the chain's re-acquire self-heal first"
        fi
      else
        drought_loops=0
        log "transient PID spike (${u} PIDs, ${hits}/${DROUGHT_STRIKES}) — chain healthy, NOT restarting"
      fi
    else
      drought_loops=0   # output clean — reset the persistence counter
      clean_streak=$((clean_streak+1))
      if [ "$restarts" -gt 0 ] && [ "$clean_streak" -ge "$RESTART_BUDGET_RESET" ]; then
        log "chain stable ${clean_streak} checks (~$((clean_streak*20))s clean) — resetting restart budget (was $restarts/$MAX_CHAIN_RESTARTS)"
        restarts=0; clean_streak=0
      fi
    fi
  fi

  # 3. player supervisor not running (e.g. it exited when the chain bounced)?
  if ! player_up; then start_player; fi

  sleep 20
done
