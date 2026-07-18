# Install on Linux

Roughly 5 minutes on Ubuntu 22.04 / 24.04 (bare metal). The included
`bootstrap.sh` does the whole setup for you.

## The easy way

`bootstrap.sh` apt-installs GNU Radio + ffmpeg + SoapySDR, builds and
installs the `gr-atscplus` module, and pip-installs the player extras.
It's idempotent — safe to re-run.

```bash
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
# 1. vendor API (interactive EULA - can't be scripted)
wget https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-3.15.2.run
chmod +x SDRplay_RSP_API-Linux-3.15.2.run && sudo ./SDRplay_RSP_API-Linux-3.15.2.run
sudo systemctl enable --now sdrplay
# 2. the Soapy plugin - bootstrap automates this part:
./bootstrap.sh --sdrplay
SoapySDRUtil --probe   # should list your RSP device
```

Tip for sustained 8 MS/s on slower machines: if long runs show rising
`OsO` (overflow) counts, enlarge SoapySDRPlay3's ring buffer before
building (`SoapySDRPlay.hpp`: bump the buffer to `262144` × `32`
elements) — the stock size under-buffers the live TV chain.

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
