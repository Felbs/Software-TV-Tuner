# Install on WSL (Windows Subsystem for Linux)

Yes, the tuner decodes live TV inside WSL. The trick is **where the SDR
lives**: WSL's USB passthrough drops samples, so don't pass the radio
through — **serve it from Windows over the network** with SoapyRemote,
and let the Linux side do all the decoding. Loopback is lossless; USB
passthrough is not.

You'll use **two windows** the whole time:
- **Window 1 (PowerShell):** runs the SDR server. Start it, then don't
  touch it.
- **Window 2 (Ubuntu/WSL):** everything else.

## What you need

1. Windows 10/11 with **WSL2 + Ubuntu 22.04/24.04** (`wsl --install -d Ubuntu`)
2. [`radioconda`](https://github.com/ryanvolz/radioconda) on the
   **Windows** side (bundles SoapySDR + the SoapySDRServer)
3. Your SDR's Windows driver (SDRplay: the **API v3** from
   [sdrplay.com](https://www.sdrplay.com/))
4. An antenna

## Window 1 — serve the radio (PowerShell)

```powershell
$env:PATH = "$HOME\radioconda\Library\bin;C:\Program Files\SDRplay\API\x64;" + $env:PATH
& "$HOME\radioconda\Library\bin\SoapySDRServer.exe" --bind="0.0.0.0:55132"
```

> **The PATH line is not optional.** Launched bare, the server can't
> find the vendor driver DLLs and will cheerfully serve you nothing but
> your microphone. If a later probe shows `driver = audio` instead of
> your SDR, this is why. (Adjust the radioconda path if you installed it
> elsewhere.)

Leave this window running.

## Window 2 — install and run (Ubuntu)

```bash
git clone -b wsl-port https://github.com/Felbs/Software-TV-Tuner.git ~/stvt
cd ~/stvt
./bootstrap.sh
```

`bootstrap.sh` installs GNU Radio + dependencies and builds the decoder
module (it caps build parallelism so WSL's memory limit doesn't OOM the
compiler). Then check the server sees your radio — from **inside WSL**:

```bash
SoapySDRUtil --find="driver=remote,remote=127.0.0.1:55132"
```

You should see your SDR's label (e.g. `SDRplay Dev0 RSPdx ...`). If
`127.0.0.1` doesn't connect, your WSL is in NAT mode — use the Windows
host IP instead: `ip route show default | awk '{print $3}'`.

## Run it

```bash
cd ~/stvt/adaptive-tv
export WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 STVT_MPV_VO=wlshm
export STVT_SOAPY_ARGS="driver=remote,remote=127.0.0.1:55132,remote:driver=sdrplay"
export STVT_ANTENNA="Antenna B"        # your antenna port
export STVT_EQ=long STVT_RS=erasure STVT_RS_ERASURES=20 ATSC_SYNC_SOFT_LOCK=6.0
export STVT_SPS=1.3 STVT_MIN_BUF_BYTES=16777216
export STVT_IFGR=20 STVT_RFGAIN_SEL=7  # gain: decent UHF start, tune for your antenna
python3 -u tv_tuna_panel.py
```

Open **http://localhost:8642** in your Windows browser, hit **SCAN**,
click a channel. Video plays in an mpv window via WSLg.

The `STVT_SOAPY_ARGS` line is the only WSL-specific part of the launch —
it says "the radio lives across the network." On native Linux with the
SDR plugged in directly, you'd simply omit it.

## Troubleshooting

| Symptom | Cause |
|---|---|
| probe finds only `driver = audio` (a microphone) | Window 1 launched without the PATH line — the server can't load the vendor driver |
| `no available RSP devices found` | something else holds the radio (close other SDR apps), or restart the Windows `SDRplayAPIService` |
| probe can't connect to `127.0.0.1:55132` | NAT-mode WSL — use the host IP from `ip route show default` |
| build dies with a GCC internal compiler error | out-of-memory: re-run `./bootstrap.sh` (it retries `-j2`), or `MAKE_JOBS=2 ./bootstrap.sh` |
| anything else | `python3 tools/doctor.py` — checks every dependency and prints the fix |

Next: [what to run once it's installed →](../../README.md#run)
