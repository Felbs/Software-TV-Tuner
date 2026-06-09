#!/usr/bin/env bash
# Overnight Pi autobot. Phase A: lever sweep (~25 min, documents the best
# real-time factor — #3). Phase B: server soak for the rest of the night
# (reliability of the split's front-end — #2). Detached so it survives the
# Claude session ending; everything logged under ~/pi_autobot (NOT /tmp, which
# can be cleared — see memory autobot_cgroup_wedge).
#
# Launch:  setsid nohup tools/pi_overnight.sh >/dev/null 2>&1 &
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/pi_autobot"
mkdir -p "$DIR"
MAIN="$DIR/overnight.log"

log(){ echo "$(date '+%F %T') [autobot] $*" | tee -a "$MAIN"; }

log "=================== OVERNIGHT AUTOBOT START ==================="
log "host=$(hostname) temp=$(vcgencmd measure_temp) throttled=$(vcgencmd get_throttled)"

# Persist the IQ clip off /tmp so a reboot mid-night can't lose it.
IQ="$DIR/iq_rf15.cf32"
if [ ! -f "$IQ" ]; then
  if [ -f /tmp/iq_rf15.cf32 ]; then cp /tmp/iq_rf15.cf32 "$IQ"; log "copied IQ clip to $IQ"
  else log "WARN: no IQ clip found; capturing a fresh 10s clip from the SDR"
       # capture needs the SDR exclusively → stop the server briefly
       sudo systemctl stop soapyremote-server; sleep 2
       python3 "$HERE/record_iq.py" --rf 15 --seconds 10 --out "$IQ" >>"$MAIN" 2>&1
       sudo systemctl start soapyremote-server; sleep 4
  fi
fi

# ---- Phase A: lever sweep (#3) — needs clean CPU, runs while server idles ----
log "--- Phase A: lever sweep (replay; ~25 min) ---"
bash "$HERE/pi_lever_sweep.sh" "$IQ" "$DIR/sweep_results.csv" >>"$DIR/sweep.log" 2>&1
log "Phase A done -> $DIR/sweep_results.csv ; top configs:"
{ tail -n +2 "$DIR/sweep_results.csv" | sort -t, -k7,7nr -k5,5nr | head -5; } | tee -a "$MAIN"

# ---- Phase B: server soak (#2) — rest of the night ----
# Make sure the server is up before soaking.
sudo systemctl restart soapyremote-server; sleep 5
log "--- Phase B: server soak (rest of night) ---"
SOAK_HOURS="${SOAK_HOURS:-7}" SOAK_RECONNECT_MIN="${SOAK_RECONNECT_MIN:-20}" \
  SOAK_LOG="$DIR/soak.log" python3 "$HERE/pi_server_soak.py" >>"$MAIN" 2>&1

log "=================== OVERNIGHT AUTOBOT DONE ==================="
log "read in the morning:  $DIR/sweep_results.csv  +  $DIR/soak.log  +  $MAIN"
