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

# ONE SUPERVISOR AT A TIME (docs/PI_ARCHITECTURE.md law): two stvt_run
# instances fight over the SDR and the player, each restarting the other's
# children. flock is immune to pgrep argv self-match tricks.
exec 9>/tmp/stvt_run.lock
# LOCK_FD_LEAK (2026-07-31): every child of this script inherits fd 9, so a
# plain `sleep 20` kept the lock ALIVE for up to 20 s after the supervisor
# itself was killed. Symptom for a user: stop TV, start TV again straight
# away, and it silently refuses with the message below and exits 1 —
# reproduced twice on this Pi. Two independent fixes:
#   1. `snooze` closes fd 9 in the child (below), so nothing outlives us
#      holding the lock;
#   2. wait a bounded 15 s for the lock rather than failing instantly, which
#      also covers a predecessor that is still shutting down. A genuinely
#      concurrent supervisor still loses — it just takes 15 s to say so.
flock -w 15 9 || { echo "another stvt_run.sh holds /tmp/stvt_run.lock — refusing to double-supervise"; exit 1; }

# Sleep without handing the lock fd to the child. Use this, never bare `sleep`.
snooze(){ sleep "$1" 9>&- ; }

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

# Lean real-time config (good for modest CPUs; harmless on fast ones). x86
# keeps its own gain (IFGR=59) and does NOT default the rest of the Pi's speed
# env: the fused front-end (a wash on x86), the int16-NEON equalizer
# (STVT_EQ_S16, ARM-only, no-op here), and the enlarged front-end buffers
# (STVT_MIN_BUF_BYTES) are Pi "barely-enough-CPU" trades this box doesn't need.
#
# EXCEPTION — the FPLL fold IS on by default here. It's the one Pi lever that's
# a genuine free win on x86: an exact bit-identical replica (cmp-clean A/B on
# the RF34 capture) that folds dc_blocker+agc into the fpll thread, measured at
# ~6% less CPU work for live decode (deterministic tv_replay A/B, 2026-06-13).
# The only cost is ~7% less flat-out throughput headroom, which is irrelevant
# for real-time live TV. Set STVT_FPLL_FOLD=0 to revert to separate blocks.
export STVT_FPLL_FOLD="${STVT_FPLL_FOLD:-1}"
# BLOCK_NCO (commit 3ef9b31): block-wise carrier loop in atsc_fpll_tight — a
# phasor recurrence + once-per-block loop filter (gain scaled by block len so
# acquisition is preserved) replaces per-sample sincos/atan2. Measured ~6-8%
# LESS CPU on top of the fold (deterministic tv_replay A/B), decode bit-identical
# (null/top_pid unchanged), and live-validated: cold-acquires, full real-time,
# 0 overflows over a clean soak. NOT bit-exact (recurrence vs sincosf), so the
# escape hatch matters: STVT_FPLL_BLOCK_NCO=0 reverts to the per-sample loop, and
# the drought-recovery watchdog below catches any rare regression. The flag is
# read inside the C++ work(), so it also applies to tv_replay/DVR with no wiring.
export STVT_FPLL_BLOCK_NCO="${STVT_FPLL_BLOCK_NCO:-1}"
export STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long
<<<<<<< pi-port-radiopi2
export STVT_SPS="${STVT_SPS:-1.1}" STVT_RRC_SYMS="${STVT_RRC_SYMS:-4}" STVT_TEISCRUB="${STVT_TEISCRUB:-0}"
# ── PER-ANTENNA GAIN PROFILE ────────────────────────────────────────────────
# This script used to hardcode IFGR=40 / rfgain_sel=3 for every antenna. Those
# are Antenna B (the UHF yagi) numbers, and they are not merely suboptimal
# elsewhere -- they are the difference between a channel and no channel.
# Measured on this Pi, 2026-07-31, 30 s chain-only probes:
#
#   Antenna A (attic AM loop), VHF RF9, IFGR=40 rfsel=3  (the old hardcode)
#       in_rms 1148, max|x| 1.5707 -> front end railed, 100% TEI, 0 bytes decoded
#   Antenna A (attic AM loop), VHF RF9, IFGR=45 rfsel=4
#       in_rms  612, max|x| 0.31   -> 36 real PIDs, MER 0.70, 14 CC err/s
#
# A channel that read as stone dead was one gain setting away from decoding.
# The same yagi numbers are also wrong in the other direction on the loop at
# UHF (in_rms 51, starved). Gains are therefore chosen per antenna AND per
# band -- an RSPdx LNA state that is right at 605 MHz is not right at 189 MHz.
# An explicit STVT_IFGR / STVT_RFGAIN_SEL in the environment always wins, so
# the tuners and lab harnesses that set them are unaffected.
#
# Honest limits of this table: Antenna A at UHF does not decode at any gain
# tried (best was IFGR=32 rfsel=2, still ~900 CC errors/s) -- the loop is
# simply not a UHF antenna, and no gain fixes that. Antenna B at VHF is deaf
# for the mirror-image reason (in_rms 24 at IFGR=50, 147 at IFGR=40, never
# locks). Antenna C is not characterised here; see docs/ANTENNA_PORTS.
STVT_ANTENNA="${STVT_ANTENNA:-Antenna B}"
if [ "$RF" -le 13 ]; then _band=vhf; else _band=uhf; fi
case "$STVT_ANTENNA/$_band" in
  "Antenna A/vhf") _ifgr=45; _rfsel=4 ;;   # measured; the loop IS the VHF antenna
  "Antenna A/uhf") _ifgr=32; _rfsel=2 ;;   # least-bad of the sweep; still no lock
  *)               _ifgr=40; _rfsel=3 ;;   # Antenna B / unknown: the proven yagi pair
esac
export STVT_IFGR="${STVT_IFGR:-$_ifgr}" STVT_RFGAIN_SEL="${STVT_RFGAIN_SEL:-$_rfsel}" STVT_ANTENNA

# ── WARM START: per-antenna+channel equalizer tap cache ─────────────────────
# The single biggest thing a viewer notices is that a fresh tune "starts
# glitchy and gets strong over time": the adaptive equalizer converges from
# cold on EVERY tune. The cache that fixes it has shipped in
# gr-atscplus/lib/atsc_equalizer_long_impl.cc since 2026-07-05, but nothing in
# the Pi's launch path ever set the directory, so every tune here was cold.
#
# tv_live.py turns this directory into <dir>/taps_<ANTENNA>_rf<RF>.bin, so the
# cache is keyed by antenna AND channel — an Antenna A cache can never seed an
# Antenna B tune (the taps are a fingerprint of the whole RF path, and the AM
# loop and the yagi see completely different multipath). tv_live.py also
# defaults STVT_EQ_LKG=1 when a cache dir is set, without which the equalizer
# would never fill the snapshot the cache is written from.
#
# STVT_EQ_CACHE_EVERY (default 128 field syncs ~= 3.1 s) is load-bearing, not
# a tuning knob: this supervisor kills the chain on a noise drought and stop()
# is not guaranteed to run, so "save only on stop()" would bank nothing on
# exactly the runs that most need a warm restart.
#
# The directory is gitignored (`**/tapcache/`) and self-healing: a missing,
# truncated, stale or foreign file just falls back to a cold start.
export STVT_EQ_TAP_CACHE="${STVT_EQ_TAP_CACHE:-$HERE/data/tv_live/tapcache}"
mkdir -p "$STVT_EQ_TAP_CACHE" 2>/dev/null || true

# Pi/ARM real-time trades (docs/PI_ARCHITECTURE.md, measured + bit-identical):
# GR's stock ~32KB edge buffers run the 4-core Pi 5 in pipeline lockstep
# (<1x real-time -> OsO garbage bursts -> noise-drought restart storms), and
# the float equalizer is the hottest block. 8MB/edge buffers measured
# 0.91x -> 1.09x real-time; the NEON int16 kernel wins on ARM. Default ON
# for aarch64 ONLY (export 0 to opt out); x86 behavior unchanged.
if [ "$(uname -m)" = "aarch64" ]; then
  export STVT_EQ_S16="${STVT_EQ_S16:-1}"
  export STVT_MIN_BUF_BYTES="${STVT_MIN_BUF_BYTES:-8388608}"
  export STVT_FPLL_FOLD="${STVT_FPLL_FOLD:-1}"
fi
=======
# SPS default bumped 1.1 -> 1.3: on this box (Ryzen 1600X, native RSPdx) SPS 1.3
# is markedly more robust on marginal signal than the old lean 1.1 — validated
# on a real RF36 capture pushed to the decode cliff (tools/native_sps_11v13.py):
# at cliff-edge noise 1000, TEI-bad 5.6% @1.3 vs 11.8% @1.1 (2x fewer RS fails),
# and 1.3 beat 1.1 at every level tested. 1.3 is less front-end work than tv_live's
# own 1.5 default, which live-decodes real-time here with margin, so 1.3 is safe
# real-time. STVT_SPS=1.1 reverts the lean profile on a slower CPU.
export STVT_SPS="${STVT_SPS:-1.3}" STVT_RRC_SYMS="${STVT_RRC_SYMS:-4}" STVT_TEISCRUB="${STVT_TEISCRUB:-0}"
# Per-antenna gain profile: stvt_autocal.py writes ~/.stvt_autocal.env (STVT_IFGR
# + eq knobs) — source it here so a calibrated box "just works". Only load it when
# the user hasn't set STVT_IFGR explicitly, so an inline override still wins. This
# is the intended autocal hookup (the writer says "an env file the run-scripts
# source"); without it a passive antenna never locks at the active-antenna default.
if [ -z "${STVT_IFGR:-}" ] && [ -f "$HOME/.stvt_autocal.env" ]; then
  set -a; . "$HOME/.stvt_autocal.env"; set +a
fi
export STVT_IFGR="${STVT_IFGR:-59}" STVT_RFGAIN_SEL="${STVT_RFGAIN_SEL:-5}" STVT_ANTENNA="${STVT_ANTENNA:-Antenna A}"
>>>>>>> rig/test-merge

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

# Anchored to the interpreter at argv[0] so a SHELL whose command text merely
# MENTIONS the script (an editor, a git commit, an assistant running
# "git add tools/stvt_play_hd.sh") can't false-positive. Measured failure
# 2026-06-13: a lingering shell containing that text made player_up() true
# and the player never started. Same pp.sh lesson, now fixed at the source.
chain_up(){ pgrep -f '^python3 [^ ]*tv_live\.py' >/dev/null; }
player_up(){ pgrep -f '^bash [^ ]*stvt_play_hd\.sh' >/dev/null; }

start_chain(){
  rm -f "$TS"
  [ -f "$CLOG" ] && mv "$CLOG" "$CLOG.$(printf '%(%H%M%S)T' -1)" 2>/dev/null
<<<<<<< pi-port-radiopi2
  ( cd "$HERE" && setsid python3 tv_live.py --rf "$RF" > "$CLOG" 2>&1 < /dev/null 9>&- & )
  # Pi: chain outranks the nice+10 player stack (passwordless sudo; no-op if absent)
  if [ "$(uname -m)" = "aarch64" ]; then
    snooze 1
    sudo -n renice -n -10 -p "$(pgrep -f "[t]v_live.py" | head -1)" >/dev/null 2>&1 || true
  fi
  log "started chain (RF$RF, lean config)"
=======
  # exec 9>&- closes the inherited single-instance lock fd so the detached
  # chain (and its descendants) don't hold the lock after THIS supervisor exits.
  # --rotate-gb 16 (was tv_live's 1GB default): each rotation costs one
  # deterministic ~10s player relaunch (stvt_play_hd rotation-relaunch).
  # 16GB ≈ 1h50m of TV between blips; ~16GB of SD is cheap (100GB free).
  ( exec 9>&- 2>/dev/null; cd "$HERE" && setsid $TSET_CHAIN python3 tv_live.py --rf "$RF" --rotate-gb "${STVT_ROTATE_GB:-16}" > "$CLOG" 2>&1 < /dev/null & )
  log "started chain (RF$RF, lean config${TSET_CHAIN:+, cpus $ISO_CHAIN_CPUS})"
>>>>>>> rig/test-merge
}

# The radio is single-tenant: a replacement chain must never be started while
# the outgoing one still holds the SDR. SIGINT (never TERM/KILL — those wedge
# the sdrplay API) and then WAIT for the process to be gone. Measured teardown
# on this Pi is ~2 s; the 30 s bound is pure insurance.
stop_chain(){
  pkill -INT -f '[t]v_live.py' 2>/dev/null
  for _i in $(seq 1 30); do
    chain_up || { log "chain stopped in ${_i}s"; return 0; }
    snooze 1
  done
  log "WARNING: chain did not exit 30s after SIGINT — NOT escalating (kill -9 wedges the sdrplay API)"
  return 1
}

start_player(){
<<<<<<< pi-port-radiopi2
  ( setsid "$HERE/stvt_play_hd.sh" "$PROG" 25 >/dev/null 2>&1 < /dev/null 9>&- & )
=======
  # close the inherited lock fd (see start_chain) so the detached player tree
  # (stvt_play_hd.sh -> tail|ffmpeg|mpv) doesn't keep the lock held.
  ( exec 9>&- 2>/dev/null; setsid $TSET_OTHER "$HERE/stvt_play_hd.sh" "$PROG" 25 >/dev/null 2>&1 < /dev/null & )
>>>>>>> rig/test-merge
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
# -w 15: a freshly-killed instance releases its lock asynchronously
# (measured: up to ~10s) — wait briefly instead of refusing a restart;
# a genuinely-running duplicate still gets refused after the wait.
if ! flock -w 15 9; then
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
if chain_up; then log "adopting running chain"; else start_chain; snooze 8; fi
player_up || start_player

while true; do
  # 1. chain dead?
  if ! chain_up; then
    restarts=$((restarts+1))
    [ "$restarts" -gt "$MAX_CHAIN_RESTARTS" ] && { log "MAX_CHAIN_RESTARTS hit — giving up (check the SDR)"; exit 1; }
    log "chain DOWN — restart #$restarts"
<<<<<<< pi-port-radiopi2
    snooze "$COOLDOWN"; start_chain; snooze 10; continue
=======
    clean_streak=0
    sleep "$COOLDOWN"; start_chain; sleep 10; last_size=0; stall_loops=0; continue
>>>>>>> rig/test-merge
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
<<<<<<< pi-port-radiopi2
      # DROUGHT CONFIRMATION (2026-07-31). The detector reads the last 2 MB of
      # live.ts, which at the full mux rate is only ~0.85 s of stream. The
      # residual errors on a healthy channel are BURSTY, not a steady drip
      # (measured: 94% of one-second bins completely clean, Fano factor 24),
      # so a single ordinary RF burst can spike the unique-PID count in that
      # short window while the chain is perfectly healthy. Restarting on one
      # sample therefore costs a ~10 s black screen for nothing.
      #
      # Evidence from the 2026-07-31 soak: a once-a-minute sampler recorded
      # 231 unique PIDs at minute 19 and the stream carried on healthily with
      # no restart, while the supervisor read 233 at minute 46 and restarted.
      # Same magnitude, opposite outcome — decided purely by poll timing.
      #
      # So re-sample before spending a restart. A REAL drought (the chain
      # locked onto noise) persists for many seconds and still trips this;
      # only the single-burst false positive is filtered out. This can only
      # ever make the supervisor restart LESS eagerly.
      snooze 3
      u2=$(unique_pids)
      if [ "$u2" -le "$DROUGHT_PIDS" ]; then
        log "drought reading ($u PIDs) NOT confirmed ($u2 on re-sample) — no restart"
      else
        restarts=$((restarts+1))
        [ "$restarts" -gt "$MAX_CHAIN_RESTARTS" ] && { log "MAX_CHAIN_RESTARTS hit — giving up"; exit 1; }
        log "NOISE DROUGHT ($u PIDs, confirmed $u2) — restart chain #$restarts"
        stop_chain; start_chain; snooze 10; continue
=======
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
>>>>>>> rig/test-merge
      fi
    fi
  fi

  # 3. player supervisor not running (e.g. it exited when the chain bounced)?
  if ! player_up; then start_player; fi

  snooze 20
done
