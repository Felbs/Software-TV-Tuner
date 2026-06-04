#!/bin/bash
# chain_cycler.sh — kill+restart the chain every N seconds to keep it
# in its "first 50s after lock" window where the marker profile shows
# bits actually decode. The chain's quality decays after ~50s due to
# SDR sample clipping (max|x|>1.0 observed in fpll log), which the
# RS decoder can't fully recover from.
#
# Each cycle: chain runs for CYCLE_SEC, gets killed, restarts. While
# auto_play_forever handles the actual restart, we trigger it by
# killing tv_live which exits run_stvt_winner.sh, which auto_play_forever
# detects and restarts.
#
# Usage: nohup ~/chain_cycler.sh > /tmp/chain_cycler.log 2>&1 & disown
#
# Tune via CYCLE_SEC env (default 45). Set CYCLE_SEC=0 to disable.

set -u
CYCLE_SEC="${CYCLE_SEC:-45}"
PP=/home/user/pp.sh

log() { echo "[$(date '+%H:%M:%S')] [chain_cycler] $*"; }

if [ "$CYCLE_SEC" -eq 0 ]; then
    log "CYCLE_SEC=0 — disabled, exiting"
    exit 0
fi

log "starting; will restart chain every ${CYCLE_SEC}s"
log "purpose: keep chain in its 'fresh acquisition' decode window"

while true; do
    sleep "$CYCLE_SEC"
    CHAIN=$($PP chain | head)
    if [ -z "$CHAIN" ]; then
        log "no chain alive — skipping (auto_play_forever should resurrect)"
        continue
    fi
    log "cycling chain (PIDs: $(~/pp.sh chain | tr '\n' ' '))"
    # Kill tv_live; tv_tuner.py + run_stvt_winner.sh will exit; auto_play_forever
    # will respawn. Don't kill the whole pipeline at once — let normal shutdown happen.
    TVLIVE=$(pgrep -f 'tools/tv_live.py' | head -1)
    if [ -n "$TVLIVE" ]; then
        kill "$TVLIVE" 2>/dev/null
    else
        # tv_live not found — kill the whole chain so it can restart
        for p in $($PP chain); do kill $p 2>/dev/null; done
    fi
done
