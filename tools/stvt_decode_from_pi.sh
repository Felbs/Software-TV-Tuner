#!/usr/bin/env bash
# Run ON A FAST BOX (x86 N100/N305, or the Ryzen). Decode the Pi's RSPdx
# remotely over the network and write/play the live TS here, where there's CPU
# to spare. The Pi (radiopi) must be running tools/pi_iq_server.sh.
#
# This is the decode half of the split plan (the Pi 4 can't keep up locally:
# ~0.33x real-time — see memory pi4_arm_lever_sweep). It carries the proven
# sustained-live config; gain/antenna/freq are forwarded to the Pi's SDR over
# the SoapyRemote transport.
#
# Usage:  tools/stvt_decode_from_pi.sh [rf] [pi_ip]
#         tools/stvt_decode_from_pi.sh 15 192.168.4.27
#
# Then watch the resulting tools/data/tv_live/live.ts with the usual player
# (e.g. tools/stvt_run.sh adopts a running chain, or stvt_play_hd / mpv).
set -u
RF="${1:-15}"
PI_IP="${2:-192.168.4.27}"
PORT="${STVT_REMOTE_PORT:-55132}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Bigger receive socket buffers on this box too (best-effort; needs sudo).
sudo sysctl -w net.core.rmem_max=67108864 net.core.wmem_max=67108864 >/dev/null 2>&1 || true

export STVT_SOAPY_ARGS="driver=remote,remote=${PI_IP}:${PORT},remote:driver=sdrplay"
export STVT_STREAM_ARGS="remote:prot=tcp"   # 'remote:prot' — gr-soapy rejects bare 'prot'

# Proven sustained-live config (lean, ~0.73x real-time on a 2017 Ryzen core, so
# huge headroom on a modern x86). On a fast box you can raise quality with
# STVT_SPS=1.5 STVT_RRC_SYMS=8 for full stock.
export STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long
export STVT_SPS="${STVT_SPS:-1.1}" STVT_RRC_SYMS="${STVT_RRC_SYMS:-4}" STVT_TEISCRUB="${STVT_TEISCRUB:-1}"
export STVT_RXF_FUSED="${STVT_RXF_FUSED:-1}"
export STVT_IFGR="${STVT_IFGR:-59}" STVT_RFGAIN_SEL="${STVT_RFGAIN_SEL:-5}" STVT_ANTENNA="${STVT_ANTENNA:-Antenna A}"

cd "$HERE" || exit 1
echo "[decode] RF $RF via Pi SDR at ${PI_IP}:${PORT}  (SPS=$STVT_SPS RRC=$STVT_RRC_SYMS fused=$STVT_RXF_FUSED)"
exec python3 tv_live.py --rf "$RF"
