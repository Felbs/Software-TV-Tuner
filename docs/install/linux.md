# Install on Linux

5–20 minutes depending on the machine, on Ubuntu 22.04 / 24.04 or
Linux Mint 21 / 22 (bare metal). The included `bootstrap.sh` does the
whole setup for you.

## The easy way

`bootstrap.sh` apt-installs GNU Radio + ffmpeg + mpv + SoapySDR, builds
and installs the `gr-atscplus` module, sets up the USB power rules, and
apt-installs the optional player extras. It's idempotent — safe to
re-run.

```bash
sudo apt-get update && sudo apt-get install -y git   # fresh Ubuntu/Mint doesn't ship git
git clone https://github.com/Felbs/Software-TV-Tuner.git
cd Software-TV-Tuner
./bootstrap.sh              # add --sdrplay if your SDR is an SDRplay (see below)
python3 tools/doctor.py     # every dependency checked, fixes printed
python3 tools/tv_tuner.py
```

## SDRplay on Linux

SDRplay radios need the vendor API plus `SoapySDRPlay3` built from source
(RTL-SDR and others work out of the box via `soapysdr-module-all`):

```bash
# 1. vendor API (interactive EULA - can't be scripted).
#    Run it at a REAL terminal: piping input to it (yes|, ssh without -t,
#    CI) makes the vendor installer busy-loop forever at 100% CPU.
wget https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-3.15.2.run
chmod +x SDRplay_RSP_API-Linux-3.15.2.run && sudo ./SDRplay_RSP_API-Linux-3.15.2.run
sudo systemctl enable --now sdrplay
# 2. the Soapy plugin - bootstrap automates this part:
./bootstrap.sh --sdrplay
SoapySDRUtil --probe   # should list your RSP device
```

If the probe says `no available RSP devices found` right after
installing the API, restart the service and give it a few seconds —
the install re-triggers the USB device, and a service started before
that is stuck on the old device node:

```bash
sudo systemctl restart sdrplay && sleep 4 && SoapySDRUtil --probe
```

(No systemd — WSL, containers? Start the service by hand instead:
`sudo /opt/sdrplay_api/sdrplay_apiService &`)

`bootstrap.sh --sdrplay` automatically applies our ring-buffer patch
(`tools/patch_soapy_ringbuffer.sh`) before building — the stock
SoapySDRPlay3 buffer under-runs the live TV chain at 8 MS/s and shows
up as rising `OsO` (overflow) counts. If you built SoapySDRPlay3
yourself *without* the patch, re-run `./bootstrap.sh --sdrplay-rebuild`.

## USB on Linux (bootstrap does this for you)

On Windows the vendor driver takes care of USB power management; on
Linux the kernel will happily **autosuspend** the SDR mid-stream and
caps usbfs transfer memory at 16 MB — both cause dropped samples or a
radio that "vanishes". `bootstrap.sh` installs a udev rule
(`/etc/udev/rules.d/66-stvt-sdr.rules`) that keeps SDRplay/RTL-SDR
radios fully powered, and raises the usbfs cap persistently. **Unplug
and replug the SDR once after the first bootstrap run** so the rule
applies. `python3 tools/doctor.py` verifies all of it.

## Manual gr-atscplus build

If you'd rather build the decoder module by hand (bootstrap does this for
you):

```bash
sudo apt-get install -y libsoapysdr-dev
cd gr-atscplus && mkdir -p build && cd build
cmake .. && make -j"$(nproc)" && sudo make install && sudo ldconfig
```

## Notes

- **WSL2 works — but not over USB passthrough** (that path drops ~1.8%
  of samples, more than Reed-Solomon can repair). The working pattern is
  serving the SDR from Windows over SoapyRemote and decoding in WSL —
  full recipe in the [WSL guide](wsl.md).
- On a Raspberry Pi, follow the [Raspberry Pi guide](raspberry-pi.md)
  instead — same base, with Pi-specific tips.

Next: [what to run once it's installed →](../../README.md#run)
