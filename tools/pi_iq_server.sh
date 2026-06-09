#!/usr/bin/env bash
# Run ON THE PI (radiopi). Serve the RSPdx raw IQ over the network so a FASTER
# box does the heavy ATSC DSP. This is the Pi half of the "split" plan — the
# same pattern as the WSL breakthrough, but with the roles reversed:
#
#     WSL era:   Windows = SoapySDRServer (had the SDR)   ->  WSL decodes
#     Pi  plan:  Pi      = SoapySDRServer (has the RSPdx) ->  x86 box decodes
#
# Why: a Pi 4 (Cortex-A72) decodes ATSC at only ~0.33x real-time (measured) —
# 3x too slow. But it streams 8 MS/s IQ fine (~92% over USB2/TCP). So let it be
# the antenna front-end and let a real CPU do the demod. See memory
# pi4_arm_lever_sweep.
#
# Prereq (once):  sudo apt install -y soapyremote-server
#   (creates+enables the soapyremote-server systemd service on :55132, all ifaces)
#
# Usage:  tools/pi_iq_server.sh        # set buffers, (re)start, print the address
set -u
PORT="${STVT_REMOTE_PORT:-55132}"

# Bigger socket buffers for sustained 8 MS/s (~32 MB/s) IQ over TCP.
sudo sysctl -w net.core.rmem_max=67108864 net.core.wmem_max=67108864 >/dev/null

sudo systemctl restart soapyremote-server
sleep 2
IP=$(hostname -I | awk '{print $1}')
echo "[pi-iq-server] status: $(systemctl is-active soapyremote-server)"
echo "[pi-iq-server] serving RSPdx at  ${IP}:${PORT}  (driver=remote,remote=${IP}:${PORT},remote:driver=sdrplay)"
echo "[pi-iq-server] on the fast box:  tools/stvt_decode_from_pi.sh <rf> ${IP}"
