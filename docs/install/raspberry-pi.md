# Install on Raspberry Pi

The Pi runs the same software as Linux — it's Debian-based, so the
`bootstrap.sh` installer applies. What's different on a Pi is *how you
run it*, because 8-VSB decoding is CPU-heavy.

The Pi-tuned code lives on the **`pi-port-stvt`** branch (surfer /
scanner / supervisor experiments tuned for the Pi's core count).

## Base install

```bash
git clone https://github.com/Felbs/Software-TV-Tuner.git
cd Software-TV-Tuner
git checkout pi-port-stvt
chmod +x bootstrap.sh && ./bootstrap.sh
```

A **Pi 5** is strongly recommended — it has the headroom to decode a
channel in real time. A **Pi 4** works too, but is happiest in the
record-then-decode mode below.

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
