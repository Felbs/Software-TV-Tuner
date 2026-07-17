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
chmod +x bootstrap.sh && ./bootstrap.sh
python3 tools/tv_tuner.py
```

## SDRplay on Linux

SDRplay radios need the vendor API plus `SoapySDRPlay3` built from source
(RTL-SDR and others work out of the box via `soapysdr-module-all`):

```bash
wget https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-3.15.2.run
chmod +x SDRplay_RSP_API-Linux-3.15.2.run && sudo ./SDRplay_RSP_API-Linux-3.15.2.run
sudo systemctl enable --now sdrplay
sudo apt-get install -y libsoapysdr-dev
git clone https://github.com/pothosware/SoapySDRPlay3.git
cd SoapySDRPlay3 && mkdir build && cd build
cmake .. && make -j"$(nproc)" && sudo make install && sudo ldconfig
SoapySDRUtil --probe   # should list your RSP device
```

## Manual gr-atscplus build

If you'd rather build the decoder module by hand (bootstrap does this for
you):

```bash
sudo apt-get install -y libsoapysdr-dev
cd gr-atscplus && mkdir -p build && cd build
cmake .. && make -j"$(nproc)" && sudo make install && sudo ldconfig
```

## Notes

- **WSL2 is build-only.** The chain builds and locks under WSL2, but its
  USB/NAT passthrough drops ~1.8% of samples — more than Reed-Solomon can
  repair. Run on native Linux for real decoding.
- On a Raspberry Pi, follow the [Raspberry Pi guide](raspberry-pi.md)
  instead — same base, with Pi-specific tips.

Next: [what to run once it's installed →](../../README.md#run)
