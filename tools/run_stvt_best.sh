#!/bin/bash
# run_stvt_best.sh — STVT best-build launcher
#
# This branch (stvt-best-build) IS the best build — tv_live.py already contains
# the sweep-tuned chain (FPLL α=0.001, atsc_sync_soft, atsc_equalizer_long). This
# wrapper just applies the env vars and chassis that round out the configuration.
#
# Derived from overnight_sticky_pipeline.sh winner on 2026-05-11
# (variant F_s0_98_L5_0_U2_5_E1_i3, LockYes=134s on RF 34).
#
# Prereq: usbfs_memory_mb >= 1000 (set via grub: usbcore.usbfs_memory_mb=1000).
#
# Usage:
#   tools/run_stvt_best.sh        # RF 34 default
#   tools/run_stvt_best.sh 21     # different channel

set -u

cd "$(dirname "$0")/.."
RF="${1:-34}"
LOG="$HOME/stvt_best.log"

# Chassis (RT priority + GR buffering + sample-rate plumbing)
ulimit -r 99
export SDL_VIDEODRIVER=x11
export GR_VMCIRCBUF_BUFFER_TYPE=mmap
export GR_MAX_BUFF_SIZE=8388608
export STVT_NATIVE_RATE=6000000
export STVT_RESAMP_INTERP=25
export STVT_RESAMP_DECIM=24

# Player (avoid NVDEC path that caused issues in earlier runs)
export STVT_FFPLAY_HWACCEL=none

# Sweep-winner sync_soft tuning
export ATSC_SYNC_SOFT_STICKY=0.98
export ATSC_SYNC_SOFT_LOCK=5.0
export ATSC_SYNC_SOFT_UNLOCK=2.5
export ATSC_SYNC_SOFT_EMIT_UNLOCKED=1

USBFS=$(cat /sys/module/usbcore/parameters/usbfs_memory_mb 2>/dev/null || echo "?")

echo "============================================================"
echo "  STVT best build — launching"
echo "============================================================"
echo "  Branch:        $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
echo "  FPLL alpha:    0.001 (in tv_live.py)"
echo "  sync_soft:     STICKY=0.98 LOCK=5.0 UNLOCK=2.5 EMIT_UNLOCKED=1"
echo "  RF channel:    $RF"
echo "  usbfs_memory:  ${USBFS} MB  (need >=1000)"
echo "  Log:           $LOG"
echo ""
echo "  Press Ctrl-C to stop."
echo "============================================================"
echo ""

chrt -f 99 python3 tools/tv_tuner.py --rf "$RF" --play 2>&1 | tee "$LOG"
RC=$?

# Quick verdict
echo ""
echo "============================================================"
echo "  Post-run quick metrics"
echo "============================================================"
FFM="tools/data/tv_live/tv_tuner.ffmpeg.log"
if [ -f "$FFM" ]; then
    HD=$(grep -cE '(1920x1080|1280x720|1440x[0-9]+)' "$FFM" 2>/dev/null)
    INVALID=$(grep -c 'Invalid frame dimensions 0x0' "$FFM" 2>/dev/null)
    CORRUPT=$(grep -c 'Packet corrupt' "$FFM" 2>/dev/null)
    echo "  HD frame reads:        $HD"
    echo "  Invalid-dim errors:    $INVALID"
    echo "  Corrupt-packet warns:  $CORRUPT"
    if [ "${HD:-0}" -gt 0 ]; then
        echo "  Verdict: VIDEO DECODED — best build is shippable."
    else
        echo "  Verdict: NO VIDEO DECODED — sync may have locked but TS is unusable."
        echo "           Decode-side investigation needed (eq/viterbi/RS)."
    fi
fi
echo ""
exit $RC
