# Split decode: Pi as SDR front-end, fast box as decoder

A Raspberry Pi 4 (Cortex-A72) decodes ATSC 8-VSB at only **~0.33× real-time**
(measured pure-DSP; see memory `pi4_arm_lever_sweep` and
`docs/raspberry_pi_setup.md`) — about **3× too slow** to watch live. But it
streams 8 MS/s IQ fine (~92% over USB 2.0 + TCP). So the working architecture is
to **split the work**: the Pi is the antenna/SDR front-end, and a faster machine
does the heavy demod. This is the same pattern that made the WSL build work
(there: Windows served the SDR, WSL decoded) — roles just reversed.

```
   [ RSPdx ] --USB--> [ Pi: SoapySDRServer ] --IQ over TCP--> [ fast box: tv_live.py decode + play ]
                         tools/pi_iq_server.sh                   tools/stvt_decode_from_pi.sh
```

The IQ goes over the network as **CS16** (16-bit, ~32 MB/s at 8 MS/s), well
within gigabit. All tuning (frequency, gain, antenna) is set by the decoder and
forwarded to the Pi's SDR by the SoapyRemote transport.

## On the Pi (once)

```bash
sudo apt install -y soapyremote-server      # installs + enables the systemd service on :55132
tools/pi_iq_server.sh                        # sets socket buffers, (re)starts, prints the address
```

The `soapyremote-server` service is enabled on boot and `/etc/sysctl.d/99-stvt-soapyremote.conf`
persists the socket-buffer sizes, so after this the Pi serves the RSPdx
automatically on every boot. Verify the Pi is serving:

```bash
systemctl is-active soapyremote-server          # active
SoapySDRUtil --find="driver=remote,remote=<pi-ip>:55132"   # from another box: lists the RSPdx
```

## On the fast box (x86 N100/N305, or the Ryzen)

The box needs the same stack as the Pi build (GNU Radio 3.10 + gr-atscplus +
`soapysdr-module-remote`). Clone this branch, build gr-atscplus, then:

```bash
tools/stvt_decode_from_pi.sh 15 192.168.4.27     # RF 15, Pi at 192.168.4.27
# writes tools/data/tv_live/live.ts on THIS box — watch it with the usual player:
tools/stvt_run.sh 15          # adopts the running chain + supervises a player
```

Raise quality on a fast box: `STVT_SPS=1.5 STVT_RRC_SYMS=8 tools/stvt_decode_from_pi.sh ...`

## The other options (hardware)

- **One box, no Pi:** run the whole chain on an x86 **N100/N305** mini-PC with the
  RSPdx plugged straight in — full real-time, no split. The simplest path if you
  buy hardware.
- **Skip the SDR demod entirely:** a hardware-demod ATSC tuner (USB, or a
  networked **HDHomeRun**) hands any machine — including the Pi — a finished
  transport stream to play. Watches live TV on a Pi, but doesn't use this
  software demodulator.
