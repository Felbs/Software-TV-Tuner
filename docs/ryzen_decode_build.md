# Fast-box (x86 / Ryzen) decoder build — for the Pi split

Build steps for the machine that does the **decoding** in the split setup
(`docs/pi_split_decode.md`): the Pi streams IQ, this box runs the ATSC DSP.

Key simplification: **this box does NOT need the SDRplay API or SoapySDRPlay3.**
The RSPdx lives on the Pi; this box only needs the SoapyRemote *client* module
(`soapysdr-module-remote`). The `remote:driver=sdrplay` part is resolved on the
Pi. So the decoder box never touches SDRplay drivers.

Assumes x86-64 Debian/Ubuntu (Bookworm/24.04 or similar) with GNU Radio 3.10
(matches gr-atscplus's `find_package(Gnuradio 3.10)`).

---

## A. If this box already builds STVT (e.g. the Ryzen dev box)

You only need the remote client module + this branch:

```bash
sudo apt install -y soapysdr-module-remote      # the only new dependency
cd ~/Software-TV-Tuner
git fetch origin && git checkout pi-port-stvt
# gr-atscplus is unchanged in behaviour (the eq knob is inert by default); a
# rebuild is optional but harmless:
#   cd gr-atscplus/build && cmake .. && make -j"$(nproc)" && sudo make install && sudo ldconfig
```

Verify the box can see the Pi's SDR over the network:
```bash
SoapySDRUtil --find="driver=remote,remote=192.168.4.27:55132"   # lists the RSPdx
```

Then jump to **§C Run**.

---

## B. Fresh x86 box — full stack

```bash
# 1. Toolchain + GNU Radio 3.10 + SoapySDR + the REMOTE client + player
sudo apt update
sudo apt install -y \
  gnuradio gnuradio-dev gr-osmosdr \
  cmake build-essential git pkg-config \
  libsoapysdr-dev soapysdr-tools soapysdr-module-remote \
  libvolk-dev pybind11-dev python3-numpy python3-packaging python3-soapysdr \
  ffmpeg mpv

# 2. Verify GNU Radio is 3.10.x (major.minor MUST match)
gnuradio-config-info --version
python3 -c "from gnuradio import gr, dtv, filter, analog, blocks, fft, soapy; print('GR OK')"

# 3. Tune VOLK for this CPU (AVX2/FMA on the Ryzen — big win on x86, unlike ARM)
volk_profile        # writes ~/.volk/volk_config; a few minutes

# 4. Clone + build gr-atscplus (the custom ATSC blocks)
git clone -b pi-port-stvt https://github.com/Felbs/Software-TV-Tuner.git
cd Software-TV-Tuner/gr-atscplus
rm -rf build && mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
make -j"$(nproc)"
sudo make install && sudo ldconfig
python3 -c "from gnuradio import atscplus; print('atscplus OK')"
```

---

## C. Run — decode the Pi's SDR here

The Pi (radiopi) must be serving: on the Pi, `tools/pi_iq_server.sh` (its
`soapyremote-server` service already auto-starts on boot at `:55132`).

```bash
cd ~/Software-TV-Tuner
# RF 15 (Univision, strong); second arg = the Pi's IP
tools/stvt_decode_from_pi.sh 15 192.168.4.27
```

This writes `tools/data/tv_live/live.ts` **on this box**. Watch it with the usual
supervised player (it adopts a running chain):
```bash
tools/stvt_run.sh 15
```

Quality: the script defaults to the lean sustained-live config (SPS=1.1,
RRC_SYMS=4). A modern x86 has tons of headroom — raise it with:
```bash
STVT_SPS=1.5 STVT_RRC_SYMS=8 tools/stvt_decode_from_pi.sh 15 192.168.4.27
```

## Health checks
- `grep -c OsO tools/data/tv_live/tv_tuner.tv_live.log` — should stay LOW (OsO now
  reflects the network/USB path on the Pi, not this box's CPU).
- live.ts should grow at ~2.42 MB/s (real-time). If it lags, the bottleneck is the
  Pi's USB 2.0 link (move the RSPdx to a Pi USB 3.0 port) or the network, NOT the
  decoder CPU.
