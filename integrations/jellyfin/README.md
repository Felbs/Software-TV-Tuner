# STVT → Jellyfin (native Linux)

Turn this box (SDR + antenna) into a network TV tuner that Jellyfin auto-detects,
then watch live TV on any Jellyfin client (phone, TV, browser). This box only
**tunes** — Jellyfin does the decode/transcode — which is why streaming plays
smoother than decoding locally.

## 1. Start the tuner server

```bash
cd ~/Software-TV-Tuner
python3 tools/stvt_hdhr.py
```

It serves the HDHomeRun API on `:5004` (`http://<this-box>:5004/discover.json`)
and tunes the SDR on demand. Gain defaults to the passive-antenna config
(`IFGR=20/RFGAIN_SEL=7/Antenna B`); override any `STVT_*` knob from the env.

Run it as a persistent user service:

```bash
systemd-run --user --unit=stvt-hdhr \
  --working-directory=$HOME/Software-TV-Tuner \
  python3 tools/stvt_hdhr.py
# stop:  systemctl --user stop stvt-hdhr
```

## 2. Point Jellyfin at it

**Automatic** (fresh Jellyfin — creates the admin user, adds the tuner + guide):

```bash
JF_PASS='your-password' STVT_TUNER='http://localhost:5004' \
  python3 integrations/jellyfin/jf_setup.py
```

Use `STVT_TUNER='http://<this-box-ip>:5004'` if Jellyfin runs on another
machine. `JF_URL` defaults to `http://localhost:8096`.

**Manual** (existing Jellyfin): Dashboard → Live TV → Tuner Devices → **+** →
HDHomeRun → `http://<this-box>:5004`. Then TV Guide Data → **+** → XMLTV →
`http://<this-box>:5004/guide.xml`. Refresh the guide.

## 3. Watch

Jellyfin → **Live TV** → pick a channel. First tune takes a few seconds while
the SDR locks; switching to a different RF re-tunes (single tuner).

## Endpoints

| path | purpose |
|------|---------|
| `/discover.json` | device descriptor (auto-detect) |
| `/lineup.json` | channel list |
| `/lineup.m3u` | generic M3U (Plex/VLC) |
| `/guide.xml` | minimal XMLTV so Live-TV setup completes |
| `/auto/v5.1` | HDHomeRun-standard stream path |
| `/stream/<rf>/<minor>` | tune + stream one program |

The channel list comes from `tools/default_stations.py` (edit it for your
market); the MPEG program for each subchannel is resolved live from the tuned
mux's PAT.
