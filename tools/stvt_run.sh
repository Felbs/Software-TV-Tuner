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
RUNLOG="/tmp/stvt_run.log"
MAX_CHAIN_RESTARTS=30
COOLDOWN=5
DROUGHT_PIDS=150            # unique-PID count above this = noise drought

# Lean real-time config (good for modest CPUs; harmless on fast ones). Windows
# (Threadripper) keeps its own gain (IFGR=59) and does NOT default the Pi's
# speed env: the fused front-end, int16-NEON equalizer (STVT_EQ_S16, ARM-only),
# enlarged front-end buffers (STVT_MIN_BUF_BYTES), and the FPLL fold are all
# "barely-enough-CPU" trades the Pi needed to clear real-time. This 64-thread
# box runs the chain at several x real-time, so they're off by default — export
# any of them to opt in. The FPLL fold C++ is built and verified bit-identical
# on x86 (cmp-clean A/B on Ubuntu's RF34 capture). It serialises dc_blocker+agc
# into the fpll thread → ~½ a core cheaper but ~7% slower wall-clock on a
# 6-core Ryzen. On 64 threads the parallelism loss is meaningless, so it's
# expected to be a free win here. STVT_FPLL_FOLD=1 to enable.
export STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long
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

log(){ echo "$(printf '%(%H:%M:%S)T' -1) $*" | tee -a "$RUNLOG" ; }

chain_up(){ pgrep -f '[t]v_live.py' >/dev/null; }
player_up(){ pgrep -f '[s]tvt_play_hd.sh' >/dev/null; }

start_chain(){
  rm -f "$TS"
  [ -f "$CLOG" ] && mv "$CLOG" "$CLOG.$(printf '%(%H%M%S)T' -1)" 2>/dev/null
  ( cd "$HERE" && setsid python3 tv_live.py --rf "$RF" > "$CLOG" 2>&1 < /dev/null 9>&- & )
  # Pi: chain outranks the nice+10 player stack (passwordless sudo; no-op if absent)
  if [ "$(uname -m)" = "aarch64" ]; then
    snooze 1
    sudo -n renice -n -10 -p "$(pgrep -f "[t]v_live.py" | head -1)" >/dev/null 2>&1 || true
  fi
  log "started chain (RF$RF, lean config)"
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
  ( setsid "$HERE/stvt_play_hd.sh" "$PROG" 25 >/dev/null 2>&1 < /dev/null 9>&- & )
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

restarts=0
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
    snooze "$COOLDOWN"; start_chain; snooze 10; continue
  fi

  # 2. noise drought? (only meaningful once the file has grown a bit)
  if [ -f "$TS" ] && [ "$(stat -c%s "$TS" 2>/dev/null || echo 0)" -gt 3000000 ]; then
    u=$(unique_pids)
    if [ "$u" -gt "$DROUGHT_PIDS" ]; then
      restarts=$((restarts+1))
      [ "$restarts" -gt "$MAX_CHAIN_RESTARTS" ] && { log "MAX_CHAIN_RESTARTS hit — giving up"; exit 1; }
      log "NOISE DROUGHT ($u PIDs) — restart chain #$restarts"
      stop_chain; start_chain; snooze 10; continue
    fi
  fi

  # 3. player supervisor not running (e.g. it exited when the chain bounced)?
  if ! player_up; then start_player; fi

  snooze 20
done
