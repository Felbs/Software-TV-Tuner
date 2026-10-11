# Install on Arch Linux / Omarchy

Verified 2026-09-26 on Omarchy (Arch, kernel 7.2, GNU Radio 3.10.12,
Python 3.14) with an SDRplay RSPdx. Everything below is what actually
worked on that box; nothing is copied from the Ubuntu guide untested.

Arch differs from Ubuntu in four ways that matter here:

1. Packages come from `pacman` and the AUR (`yay`), not `apt`.
2. Python is "externally managed" — there is no system `pip`; use the
   distro packages, or a venv with `--system-site-packages`.
3. CMake installs to `/usr/local` by default and **Arch does not look
   there** — install the decoder module with `-DCMAKE_INSTALL_PREFIX=/usr`.
4. VLC is split into plugin packages; the caption text renderer and the
   MPEG-2 decoders are optional installs.

## 1. Packages

```bash
sudo pacman -S --needed base-devel cmake git \
  gnuradio gnuradio-companion gnuradio-osmosdr volk boost pybind11 eigen \
  soapysdr soapyrtlsdr rtl-sdr libusb \
  python-numpy python-scipy python-yaml python-psutil \
  ffmpeg mpv vlc vlc-plugin-freetype vlc-plugin-ffmpeg vlc-plugin-mpeg2
```

SDRplay owners also need the vendor API service and the SoapySDR plugin
from the AUR (both build from source, a few minutes):

```bash
yay -S --needed libsdrplay soapysdrplay3-git
sudo systemctl enable --now sdrplay
SoapySDRUtil --find          # should list your RSP
```

## 2. The ring-buffer patch (SDRplay only — the big Linux quality fix)

The stock SoapySDRPlay3 plugin under-runs the live chain at 8 MS/s and
shows up as rising `OsO` overflow counts. Rebuild the AUR package with
`tools/patch_soapy_ringbuffer.sh` applied: copy the PKGBUILD directory
out of `~/.cache/yay/soapysdrplay3-git`, add the two `sed` lines from that
script to `prepare()`, bump `pkgrel`, then `makepkg -sf` and
`sudo pacman -U` the result. (An AUR update will overwrite it — re-apply.)

## 3. USB power and buffers

Same rules as Ubuntu, applied by hand (bootstrap.sh is apt-only):

```bash
sudo tee /etc/udev/rules.d/66-stvt-sdr.rules >/dev/null <<'RULES'
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="1df7", TEST=="power/control", ATTR{power/control}="on"
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="0bda", ATTR{idProduct}=="2838", TEST=="power/control", ATTR{power/control}="on"
RULES
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb
echo 'w! /sys/module/usbcore/parameters/usbfs_memory_mb - - - - 1000' | sudo tee /etc/tmpfiles.d/stvt-usbfs.conf
echo 1000 | sudo tee /sys/module/usbcore/parameters/usbfs_memory_mb
sudo pacman -S cpupower && sudo cpupower frequency-set -g performance   # resets at reboot
```

`python3 tools/doctor.py` verifies all of it ("USB link sustains full rate").

## 4. Build and install the decoder module

```bash
cmake -S gr-atscplus -B gr-atscplus/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr
make -C gr-atscplus/build -j"$(nproc)"
sudo make -C gr-atscplus/build install && sudo ldconfig
python3 -c "from gnuradio import atscplus; print('ok')"
```

## 5. Run

```bash
STVT_ANTENNA="Antenna B" STVT_BIAST=1 python3 tools/tv_tuner.py --rf 31 --player vlc
```

`--player vlc` is the one to use on Linux: it passes the broadcast through
untouched (VLC decodes MPEG-2 + AC-3 and renders captions in sync). With
`--player mpv` the tuner re-encodes video with x264, which costs several
cores. VLC shows a "Privacy and Network Access Policy" dialog the first
time; answer it once (or set `qt-privacy-ask=0` in `~/.config/vlc/vlcrc`).

## What to expect from the CPU

Software 8-VSB is ~5 cores of sequential DSP. A 2-core 2012 laptop
(i5-3210M) locks and reads a healthy MER but cannot keep real time:
expect sample overflows and a glitchy picture no matter which levers are
set. A 6-core desktop reaches ~95 %. The genuine fix below that is a
hardware-demod tuner (HDHomeRun).
