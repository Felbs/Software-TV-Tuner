#!/usr/bin/env bash
# stvt_cpu_isolate.sh — protect the decoder chain's single-thread bottleneck from
# preemption by confining competing work (player, SDR daemon, desktop, and any
# monitoring) to a SEPARATE set of CPUs. Applied live to running processes — no
# restart, no decode change — and fully reversible (`stvt_cpu_isolate.sh undo`).
#
# The chain is gated by one hot thread (the matched filter). When the player,
# sdrplay daemon, desktop, or a stray monitor lands on that thread's core, the
# chain stalls and the SDR ring overflows (OsO -> drought). Pinning everything
# else away gives the chain's cores no competition.
#
# Ryzen 1600X: 6 physical cores, SMT sibling pairs (0,6)(1,7)(2,8)(3,9)(4,10)(5,11).
#   chain -> cores 0,1,2,4 (cpus 0-3,6-9)  4 protected physical cores (~3.75 needed)
#   sdr   -> core 5        (cpus 4,10)      SDR daemon its own core (must not drop)
#   other -> core 6        (cpus 5,11)      player + desktop + monitoring
#
# NOTE: one-shot — pins the CURRENT pids. A watchdog chain restart spawns new
# pids; re-run after a restart, or born-pin via stvt_run (taskset in start_chain).
set -u
CHAIN_CPUS="${STVT_CHAIN_CPUS:-0-3,6-9}"
SDR_CPUS="${STVT_SDR_CPUS:-4,10}"
OTHER_CPUS="${STVT_OTHER_CPUS:-5,11}"
ALL_CPUS="0-11"

pin(){ local cpus="$1"; shift; for p in "$@"; do
         [ -n "$p" ] && taskset -a -cp "$cpus" "$p" >/dev/null 2>&1 && echo "  pid $p -> $cpus"
       done; }
pids_chain(){   ps -eo pid,args | grep '[t]v_live.py --rf'                       | grep -v grep | awk '{print $1}'; }
pids_player(){  ps -eo pid,args | grep -E '[m]pv |[f]fmpeg|tail -c .*live.ts|[s]tvt_play_hd' | grep -v grep | awk '{print $1}'; }
pids_sdr(){     ps -eo pid,args | grep '[s]drplay_apiService'                    | grep -v grep | awk '{print $1}'; }
pids_desktop(){ ps -eo pid,args | grep -E '[g]nome-shell|[g]js'                  | grep -v grep | awk '{print $1}'; }

if [ "${1:-apply}" = "undo" ]; then
  echo "restoring all to $ALL_CPUS:"
  pin "$ALL_CPUS" $(pids_chain) $(pids_player) $(pids_sdr) $(pids_desktop)
  exit 0
fi

echo "chain -> $CHAIN_CPUS";  pin "$CHAIN_CPUS"  $(pids_chain)
echo "sdr   -> $SDR_CPUS";    pin "$SDR_CPUS"    $(pids_sdr)
echo "other -> $OTHER_CPUS";  pin "$OTHER_CPUS"  $(pids_player) $(pids_desktop)
echo "done. undo: $0 undo"
