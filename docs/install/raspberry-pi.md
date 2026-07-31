# Install on Raspberry Pi

The Pi runs the same software as Linux — it's Debian-based, so the
`bootstrap.sh` installer applies. What's different on a Pi is *how you
run it*, because 8-VSB decoding is CPU-heavy.

The Pi-tuned code lives on the **`pi-port-radiopi2`** branch (surfer /
scanner / supervisor experiments tuned for the Pi's core count).

## Base install

```bash
git clone -b pi-port-radiopi2 https://github.com/Felbs/Software-TV-Tuner.git
cd Software-TV-Tuner
./bootstrap.sh
python3 tools/doctor.py     # every dependency checked, fixes printed
```

Use a **64-bit** Raspberry Pi OS. If the build runs out of memory on a
Pi 4 (GCC "internal compiler error"), re-run with `MAKE_JOBS=2
./bootstrap.sh`.

A **Pi 5** is strongly recommended — it has the headroom to decode a
channel in real time. A **Pi 4** works too, but is happiest in the
record-then-decode mode below.

Everything here is stock **Raspberry Pi OS** — no Windows, no WSL, no
second computer required.

## Installing over SSH (headless)

The whole install and all scanning/recording work fine over a plain
`ssh pi@raspberrypi.local` session — clone, `./bootstrap.sh`,
`python3 tools/doctor.py`, scan, record, schedule. The only thing that
needs a screen is *watching* live video, and you have three options:

- sit at the Pi's own desktop (HDMI) for playback,
- record now over SSH and copy the files to any machine, or
- run the split-decode mode below and watch on the other computer.

## Two ways to run on a Pi

**1. All-on-Pi DVR (works on Pi 4 and 5)** — record the raw IQ now,
decode it offline, watch afterward. No second machine needed; sidesteps
the real-time CPU wall:

```bash
python3 tools/tv_tuner.py        # scan + record
# decode the saved IQ offline, then play it back
```

**2. SoapyRemote split-decode** — let the Pi be a networked IQ server and
do the heavy decoding on a faster desktop over the LAN. Install the
SoapyRemote server on the Pi:

```bash
sudo apt-get install -y soapysdr-module-remote
SoapySDRServer --bind
```
then point the desktop's chain at the Pi's address as the SDR source.

## Notes

- Same SDR setup as [Linux](linux.md) — RTL-SDR works out of the box;
  SDRplay needs the vendor API + `SoapySDRPlay3`.
- Keep the SDR on a short, direct USB link; extend on the antenna side
  with coax, not a long USB cable.
- Stuck? See [Troubleshooting](../../README.md#troubleshooting).

Next: [what to run once it's installed →](../../README.md#run)
