#!/usr/bin/env bash
# Launch the STVT live chain via the SoapyRemote transport (WSL -> Windows host).
#
# Prereqs (see HANDOFF.md / memory wsl_sdr_use_soapyremote_not_usbip):
#   * RSPdx attached to WINDOWS (not usbip — usbip starves it to ~1 MS/s).
#   * SoapySDRServer running on Windows, bound to IPv4:
#       "C:\Program Files\PothosSDR\bin\SoapySDRServer.exe" --bind=0.0.0.0:55132
#   * (once) sudo sysctl -w net.core.rmem_max=67108864 net.core.wmem_max=67108864
#
# Usage: tools/stvt_live_remote.sh [rf]   (default RF34)
set -u
RF="${1:-34}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export STVT_SOAPY_ARGS="driver=remote,remote=127.0.0.1:55132,remote:driver=sdrplay"
export STVT_STREAM_ARGS="remote:prot=tcp"   # key is remote:prot (gr-soapy rejects bare 'prot')

# LEAN real-time config: SPS=1.1 + RRC_SYMS=4 run ~0.73x real-time so the
# single-threaded matched filter sustains live 8 MS/s without OsO buildup.
# (Full quality SPS=1.5/RRC=8 is ~1.28x real-time -> overflows creep in over
# a long run — fine for short offline bursts, NOT for sustained live.)
# TEISCRUB=1 kept ON so the player sees NULL packets instead of corrupt PIDs.
export STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long
export STVT_SPS="${STVT_SPS:-1.1}" STVT_RRC_SYMS="${STVT_RRC_SYMS:-4}" STVT_TEISCRUB=1
export STVT_IFGR=59 STVT_RFGAIN_SEL=5 STVT_ANTENNA="Antenna A"

cd "$HERE" || exit 1
exec python3 tv_live.py --rf "$RF"
