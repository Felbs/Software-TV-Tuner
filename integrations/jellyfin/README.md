# Jellyfin (and Plex / Emby / Channels) integration

STVT exposes the tuner as a **network HDHomeRun** (`adaptive-tv/stvt_hdhr.py`),
so any media server can pull live antenna TV from it. The media server does the
video decode + transcode — which is the whole point: the Raspberry Pi has no
hardware MPEG-2 decoder, so it should only ever *tune*, never *decode*. A
capable client (a PC/GPU) does the heavy lifting and streams to phones, tablets,
browsers, TVs.

```
antenna → SDR → Pi (stvt_hdhr :5004, HDHomeRun) → Jellyfin server (transcode) → apps
```

## 1. Run the tuner server (on the Pi)

`stvt_hdhr.py` runs as the `stvt-hdhr` user service (see the Raspberry Pi
install section of the top-level README). Verify it:

```bash
curl http://<pi-ip>:5004/discover.json     # HDHomeRun descriptor
curl http://<pi-ip>:5004/lineup.json        # channel list
```

Put the Pi in **tuner-only (headless)** mode so it doesn't waste CPU decoding a
local screen nobody's watching:

```bash
# in the Pi's ~/start_panel.sh
export STVT_HEADLESS=1        # 1 = stream-only (no local player); 0 = watch on the Pi
```

## 2. Install a media server

Any of Jellyfin / Plex / Emby / Channels DVR works. Jellyfin (free, no account):

```bash
# on the machine that will do the decoding (a PC/GPU box, NOT the Pi)
curl https://repo.jellyfin.org/install-debuntu.sh | sudo bash
```

## 3. Point the server at the tuner

**Automatic (Jellyfin):** on a *fresh* Jellyfin, this completes the wizard and
adds the tuner + guide in one shot:

```bash
JF_PASS='pick-a-password' STVT_TUNER='http://<pi-ip>:5004' \
    python3 jf_setup.py
```

**Manual (any server), via the web UI** — Dashboard → Live TV:
- **Tuner Devices → +** → *HDHomeRun* → `<pi-ip>:5004`
  (or *M3U Tuner* → `http://<pi-ip>:5004/lineup.m3u`)
- **Guide Data Providers → +** → *XMLTV* → `http://<pi-ip>:5004/guide.xml`
- Refresh the guide; the 5.1/4.1-style channels populate.

Then open the server address (e.g. `http://<server-ip>:8096`) in a browser or
the Jellyfin app on your phone/TV.

## Notes / gotchas

- **Single tuner:** the SDR is one 6 MHz channel at a time. All sub-channels of
  the tuned channel and multiple clients on it are fine; switching to a channel
  on a different RF re-tunes (~20–30 s).
- **WSL host + mirrored networking:** if the server runs in WSL, other LAN
  devices (your phone) are blocked by the Windows firewall until you allow the
  port, e.g. in an **Administrator** PowerShell:
  `New-NetFirewallRule -DisplayName "Jellyfin" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8096`
- **Guide** is currently a minimal placeholder XMLTV (channel names + rolling
  blocks). Real PSIP program data is a future enrichment.
