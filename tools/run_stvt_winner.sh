#!/bin/bash
# run_stvt_winner.sh — run STVT with the chosen equalizer.
#
# Usage: ~/run_stvt_winner.sh [eq_name] [rf]
#   eq_name: long|pilot|pilot_dd|pilot_dd_soft|multifs|multifs_dd|cma|stock
#   rf:      ATSC RF channel (default 34)
#
# 2026-05-16: stripped down to match auto_acquire.py's verify environment.
# The verify-vs-launch mismatch was killing locks on marginal RF: auto_acquire
# would find a winner using bare tv_live.py + STVT_RS=stock, then this script
# would overlay STVT_RS=erasure + ATSC_SYNC_SOFT_LOCK=3.5 + STVT_DCR_TAPS=128
# + STVT_NATIVE_RATE=6000000 + ATSC_T6_* + a tv_live_ab.py overlay, none of
# which were exercised in verify. The "tuner-converged" overrides were tuned
# in clean-RF sessions; on marginal RF they prevent the chain from acquiring
# lock at all. If you want to A/B those again, do it from a clean baseline.

set -u

ANT="${STVT_ANTENNA_OVERRIDE:-${STVT_ANTENNA:-Antenna A}}"
export STVT_ANTENNA="$ANT"
EQ="${STVT_EQ_OVERRIDE:-${1:-cma}}"
RF="${2:-34}"
STVT="$HOME/Software-TV-Tuner"
LOG="$HOME/stvt_winner_${EQ}.log"

restore() {
    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
    pkill -9 -f 'tools/tv_tuner.py' 2>/dev/null
    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
    pkill -9 -f 'tv_live.py'        2>/dev/null
    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
    pkill -9 -f 'ffplay.*tv_live'   2>/dev/null
    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
    pkill -9 -f 'mpv.*tv_live'      2>/dev/null
}
trap restore EXIT INT TERM

ulimit -r 99
export SDL_VIDEODRIVER=x11
export GR_VMCIRCBUF_BUFFER_TYPE=mmap
export GR_MAX_BUFF_SIZE=8388608
export STVT_FFPLAY_HWACCEL=none

# Equalizer choice from arg.
export STVT_EQ="$EQ"

# Match auto_acquire.py defaults — these are what produced PAT-positive
# verify runs (see tools/auto_acquire.py:run_cell). Keeping these stripped
# avoids the "verify passes, tv_tuner can't converge" trap.
# 2026-05-22 18:18 BUG FIX: honor pre-set env vars. Previous code
# unconditionally clobbered STVT_VITERBI and STVT_RS, so ALL of today's
# A/B experiments that set these env vars before invoking this script
# were silently overwritten — quick_eq_sweep's "erasure RS" rows were
# actually running stock. Now: default safely, but caller can override.
export STVT_VITERBI="${STVT_VITERBI:-hard}"
# STVT_RS=erasure breaks run_stvt_player.sh path (psi_repair can't seed
# PMT cache from it) but the chain's own ffmpeg+mpv handles it fine.
# Allow caller's choice; safe default is stock.
export STVT_RS="${STVT_RS:-stock}"
# export STVT_NB=1  # disabled to match yesterday

# Player + watchdog timing — these don't affect RF/chain convergence.
export STVT_PLAYER=mpv
export STVT_MPV_CACHE_SECS="${STVT_MPV_CACHE_SECS:-120}"
export STVT_CONVERGENCE_SEC=20
export STVT_MIN_PAT="${STVT_MIN_PAT:-1}"

# IFGR / RFGAIN_SEL / ANTENNA inherited from /tmp/auto_acquire_winner.env
# (auto_play_forever.sh sources it before invoking us).

echo "============================================================"
echo "  STVT winner build (bare chain, matches auto_acquire)"
echo "  equalizer = $EQ      RF channel = $RF"
echo "  STVT_RS=$STVT_RS  STVT_VITERBI=$STVT_VITERBI"
echo "  log: $LOG"
echo "  Ctrl-C to stop"
echo "============================================================"

cd "$STVT"
export STVT_NATIVE_RATE=8000000
export STVT_RESAMP_INTERP=25
export STVT_RESAMP_DECIM=32
export ATSC_SYNC_SOFT_LOCK=3.5
export ATSC_SYNC_SOFT_UNLOCK=2.0
python3 tools/tv_tuner.py --rf "$RF" --play 2>&1 | tee "$LOG"
