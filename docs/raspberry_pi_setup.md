# Raspberry Pi setup + ATSC-chain feasibility test

Goal: stand up the STVT decode chain on a Raspberry Pi (4 or 5) and **measure
whether it can keep up with real-time** (a 19.4 Mbps ATSC 8-VSB stream). This is
a feasibility test, not a daily driver yet — see "Expectations" below.

This doc is written to be followed top-to-bottom by a fresh Claude Code instance
running **on the Pi** (or by hand). Each step has a verify check — don't proceed
past a failed check.

---

## MEASURED RESULT — Pi 4 (2026-06-08)

Ran end-to-end on a **Pi 4 Model B Rev 1.4 (8 GB), Debian 13 trixie, GR 3.10.12**,
RSPdx on USB, RF34, lean config (`STVT_RXF_FUSED=1`):

- **Real-time factor: ~30%** (live.ts wrote **0.727 MB/s** vs the 2.42 MB/s ATSC rate)
- **281 unique PIDs** at the live edge (healthy mux ≈ 25-35) = the chain locks but
  loses most data to constant SDR overflow
- **OsO: 76 in ~90 s** (Ryzen baseline ≈ 1 in 9 HOURS)
- `vcgencmd get_throttled` = `0x0` (no undervolt — the number is valid)
- Total chain CPU **296% of 400%** (≈3 of 4 cores); hottest thread `dtv_atsc_*`
  (equalizer/viterbi) at **70%**, an `atscplus` block (fpll/sync) at 50%, the fused
  matched filter (`rational_resampler`) at 30%.

**Verdict: a Pi 4 cannot decode ATSC in real time — 30% is unwatchable.** The wall
is the single hottest *sequential* thread (equalizer/viterbi at 70%), which can't be
parallelised, so the spare 4th core doesn't help. Extrapolating per-core clock, a
**Pi 5 ≈ 30% × ~1.8 ≈ 54%** — still short of 1.0. This confirms the earlier
hardware analysis: the real small-box answer is an **x86 N100/N305 mini-PC**, not a
Pi. Everything below is the build recipe that produced this number (kept for repro).

---

## Expectations (read first — so the result isn't a surprise)

The chain uses **~3.76 cores of a Ryzen 5 1600X** (measured: 376% CPU, hottest
threads the equalizer/viterbi/fpll at ~70% each; the matched filter is ~60% after
the fusion work). Per-core, a Pi 4 (Cortex-A72 @ 1.5-1.8 GHz) is roughly **0.3×**
that Ryzen core and a Pi 5 (Cortex-A76 @ 2.4 GHz) roughly **0.55×**.

- **Pi 4:** ~1.2 Ryzen-core-equivalents total → expect **~30-35% of real-time** →
  near-continuous SDR overflow ("OsO"). Almost certainly **not watchable**. The
  point is the *number*, which predicts the Pi 5.
- **Pi 5:** ~2.2 Ryzen-core-equivalents → expect **~55-60% of real-time** → still
  short of 1.0, still heavy overflow, unless the chain is slimmed (lower matched-
  filter taps, lighter equalizer) at some quality cost.
- ATSC video is **MPEG-2**, which the Pi 4/5 **cannot hardware-decode** (the HW
  decoder dropped MPEG-2). So local 1080p playback adds ~1 software-decode core on
  top. For this test we run **headless** (chain only) and stream/copy the TS off
  the Pi for playback — that isolates the DSP question from the video-decode one.

If the measured real-time factor is well under 1.0 even on a Pi 5, the honest
answer is an **x86 N100/N305 mini-PC**, not a Pi (this matches the earlier
hardware analysis). Measure first, decide after.

---

## 0. Prerequisites (you, physically)

- **64-bit Raspberry Pi OS** (Bookworm). 32-bit will NOT work (Claude Code needs
  ARM64; GNU Radio 3.10 wants 64-bit). Verify: `uname -m` → must say `aarch64`.
- **4 GB+ RAM** (Pi 4 8 GB strongly preferred — the gr-atscplus C++ build is
  memory-hungry).
- The **SDRplay RSPdx** moved from the Ryzen box to a Pi USB **3.0** port (blue).
  Use a good power supply — the RSPdx + Pi under load is power-hungry; undervolt
  warnings (`vcgencmd get_throttled` ≠ 0x0) will wreck the measurement.
- Network access (for Claude Code auth + the API, and to copy the TS off-box).

---

## 1. Install Claude Code on the Pi

```bash
sudo apt update
curl -fsSL https://claude.ai/install.sh | bash
claude --version    # verify
```
Then `cd` into the cloned repo (step 3) and run `claude`. Authenticate in-browser
on first run.

---

## 2. Install the build/runtime stack (apt)

GNU Radio 3.10 + gr-dtv + SoapySDR dev + build tools. Pi OS Bookworm ships GR
3.10, which matches what gr-atscplus is built against (`find_package(Gnuradio
"3.10")`).

```bash
sudo apt update
sudo apt install -y \
  gnuradio gnuradio-dev gr-osmosdr \
  cmake build-essential git pkg-config \
  libsoapysdr-dev soapysdr-tools \
  libvolk-dev pybind11-dev python3-numpy python3-packaging \
  ffmpeg mpv
```
> NOTE (Debian 13 trixie / current Pi OS): the volk dev package is **`libvolk-dev`**
> (the old `libvolk2-dev` no longer exists). trixie ships GNU Radio **3.10.12** —
> 3.10.x, so the ABI matches. Install everything in ONE `apt install`; if any single
> package name is wrong, apt aborts the whole batch and installs nothing.
Verify GNU Radio:
```bash
gnuradio-config-info --version    # expect 3.10.x  (major.minor MUST be 3.10)
python3 -c "from gnuradio import gr, dtv, filter, analog, blocks, fft; print('GR python OK')"
```
If the version is not 3.10.x, stop — gr-atscplus won't load against a different
ABI. (Bookworm = 3.10; older Bullseye = 3.8, too old.)

---

## 3. Clone the repo (this branch)

```bash
cd ~
git clone -b linux-port-stvt-v3 https://github.com/Felbs/Software-TV-Tuner.git
cd Software-TV-Tuner
```

---

## 4. SDRplay API (ARM64) + SoapySDRPlay3

### 4a. SDRplay API
Download the **official Linux ARM64** API installer from
<https://www.sdrplay.com/api/> (a `SDRplay_RSP_API-Linux-3.x.xx.run` — pick the
ARM64/aarch64 build). Then:
The current direct URL is
`https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-3.15.2.run` (one unified
installer covers x64 + ARM32/ARM64; it auto-picks `arm64` via `dpkg --print-architecture`).
```bash
chmod +x SDRplay_RSP_API-Linux-*.run
sudo ./SDRplay_RSP_API-Linux-*.run      # accept the licence; installs /opt/sdrplay_api + a systemd service
sudo systemctl enable --now sdrplay
systemctl status sdrplay                # verify the apiService is active
```
> NON-INTERACTIVE install (driving over SSH with no TTY): the `.run` is a Makeself
> archive whose inner `install_lib.sh` pages the licence with `more`, which will
> EAT your piped answers and hang. Extract first, neutralise the pager, then feed
> the two `y` prompts:
> ```bash
> ./SDRplay_RSP_API-Linux-*.run --noexec --keep --target ~/sdrplay_extract
> cd ~/sdrplay_extract
> sed -i -e '/read -p "Press RETURN to view the license agreement"/d' \
>        -e 's/^more -d sdrplay_license.txt/cat sdrplay_license.txt >\/dev\/null/' install_lib.sh
> printf 'y\ny\n' | bash ./install_lib.sh      # self-sudos; needs passwordless sudo
> ```
> Installs `libsdrplay_api.so.3.15` → `/usr/local/lib`, the service to
> `/opt/sdrplay_api`, and enables+starts the `sdrplay` systemd unit.
Verify the device is seen:
```bash
SoapySDRUtil --probe="driver=sdrplay" 2>&1 | head -40   # should list the RSPdx
```
(If "No devices found": replug the RSPdx, check `dmesg | tail`, confirm USB3.)

### 4b. SoapySDRPlay3 (build from source against the API)
```bash
cd ~
git clone https://github.com/pothosware/SoapySDRPlay3
cd SoapySDRPlay3
```
**Apply the ring-buffer patch** (the single biggest OsO-resilience win on the
Ryzen box — even more important on a slow Pi). Find the ring-buffer sizing in the
source (look for `numPackets` / a buffer-count near `262144` or the default like
`numBuffers`/`bufferLength`) and bump it up; grep to locate:
```bash
grep -rniE 'ring|numBuffers|bufferLength|256\*1024|65536|numPackets' Source/ | head
```
Then build:
```bash
mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
make -j$(nproc)
sudo make install
sudo ldconfig
SoapySDRUtil --info | grep -i sdrplay     # module should be listed
```

---

## 5. Build gr-atscplus for ARM

This compiles the custom DSP blocks (fpll, sync, fs_checker, equalizer, viterbi,
RS, etc.). VOLK auto-selects NEON on aarch64. **On a Pi 4 this can take 20-40 min**
— `-march=native` is fine (it picks the Pi's core).

```bash
cd ~/Software-TV-Tuner/gr-atscplus
rm -rf build && mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
make -j$(nproc)              # if RAM-starved, drop to -j2; the linker is the hog
sudo make install
sudo ldconfig
```
Verify the module imports (it installs as `gnuradio.atscplus`, the same way
`tools/tv_live.py` imports it — NOT a top-level `atscplus`):
```bash
python3 -c "from gnuradio import atscplus; print('atscplus OK')"
```
On trixie it installs to `/usr/local/lib/python3.13/dist-packages/gnuradio/atscplus`
and `/usr/local/lib/aarch64-linux-gnu/libgnuradio-atscplus.so*` — both already on
the default Python/loader paths, so the import works straight after `make install`.
If it fails, check `ldconfig -p | grep atscplus` and that `/usr/local/lib` (and the
`aarch64-linux-gnu` subdir) are on the loader path.

---

## 6. First light — does the chain lock?

```bash
cd ~/Software-TV-Tuner/tools
# Lean production config (same as the Ryzen box). RF34 = the test mux; change --rf
# to a strong local ATSC RF channel if 34 isn't yours.
export STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long
export STVT_SPS=1.1 STVT_RRC_SYMS=4 STVT_TEISCRUB=0
export STVT_RXF_FUSED=1                      # the cheaper fused matched filter — important on a Pi
export STVT_ROTATE_GB=4                       # keep the file small on a Pi SD card
mkdir -p data/tv_live
python3 tv_live.py --rf 34 2>&1 | tee /tmp/pi_chain.log
```
Let it run ~60 s, then in another shell check the output is real TS (not noise):
```bash
tail -c 2000000 ~/Software-TV-Tuner/tools/data/tv_live/live.ts | python3 -c '
import sys,collections
d=sys.stdin.buffer.read(); c=collections.Counter(); i=d.find(b"\x47")
while i>=0 and i+188<=len(d):
    if d[i]==0x47: c[((d[i+1]&0x1f)<<8)|d[i+2]]+=1; i+=188
    else: i=d.find(b"\x47",i)
print("unique PIDs:",len(c)," (healthy mux ~25-35; hundreds = noise/overflow)")'
```

---

## 7. THE MEASUREMENT — real-time factor + OsO rate

Two numbers decide everything.

**(a) Throughput vs real-time.** A chain that keeps up writes `live.ts` at the
ATSC rate, **~2.42 MB/s** (19.4 Mbps). If the Pi falls behind, the SDR overflows
and the *effective* write rate drops. Measure the sustained rate over 60 s:
```bash
cd ~/Software-TV-Tuner/tools
F=data/tv_live/live.ts
s1=$(stat -c%s "$F"); sleep 60; s2=$(stat -c%s "$F")
rate=$(python3 -c "print(($s2-$s1)/60/1e6)")
echo "write rate: ${rate} MB/s   (real-time = 2.42)"
python3 -c "print('real-time factor: %.0f%%' % (($s2-$s1)/60/2.42e6*100))"
```
> NOTE: `live.ts` write rate is a *proxy*. A truer pure-DSP number is the
> file-replay benchmark (step 8) — it removes the SDR/USB from the equation.

**(b) OsO (sample-overflow) frequency.** Each overflow burst prints an `OsO`
line. Count them over the run:
```bash
grep -c '^OsO' /tmp/pi_chain.log     # Ryzen baseline: ~1 in 9 HOURS. Many/min = can't keep up.
```
Also watch per-thread load to see which block is the wall on ARM:
```bash
CPID=$(pgrep -f 'tv_live.py'); top -H -p $CPID    # press q to quit
```

**Interpretation:**
- real-time factor ≈ 100% AND OsO rare → the Pi can do it (surprising — celebrate).
- real-time factor < ~90% OR OsO every few seconds → it's behind. Record the %.
  A Pi 4 at e.g. 33% predicts a Pi 5 at ~33%×1.7 ≈ 56% (still short).

---

## 8. (Optional, cleanest) Pure-DSP benchmark via file replay

This removes the live SDR/RF entirely so you measure *only* the Pi's DSP speed —
deterministic and repeatable. `tools/tv_replay.py` swaps the SDR for a
`file_source` reading a captured **CF32 IQ** file.

1. On the **Ryzen box**, capture a short IQ clip (~20 s) at the chain's native
   8 MS/s and copy it to the Pi (ask the Ryzen-side assistant to produce
   `iq_rf34.cf32` — it can capture via SoapySDRUtil/a tiny flowgraph while the
   chain is briefly paused). CF32 = 8 bytes/sample, so 8 MS/s ⇒ 64 MB per second
   of signal; a 20 s clip ≈ 1.3 GB.
2. On the **Pi**, time the replay (uses bash `SECONDS` — no fragile parsing):
```bash
cd ~/Software-TV-Tuner
IQ=~/iq_rf34.cf32
dur=$(python3 -c "import os;print(os.path.getsize('$IQ')/64e6)")   # seconds of signal (8 MS/s CF32)
SECONDS=0
python3 tools/tv_replay.py --iq "$IQ" --out /tmp/replay.ts --log /tmp/replay.log
wall=$SECONDS
echo "signal=${dur}s  wall=${wall}s"
python3 -c "print('real-time factor: %.2fx  (>1.0 keeps up, <1.0 cannot)' % ($dur/$wall))"
```
**real-time factor = signal_seconds / wall_seconds.** >1.0 = faster than
real-time (the Pi could keep up); <1.0 = it cannot, and the value scales roughly
linearly with clock (Pi-4 0.35x ⇒ Pi-5 ≈ 0.6x). Trust this over the step-7 proxy.
(If `--repeat` is needed to lengthen a short clip, divide by the repeat count.)

---

## 9. Report back

Capture and report: Pi model + RAM, `uname -m`, GNU Radio version, the **real-time
factor %**, **OsO/min**, the `top -H` hot thread on ARM, and `vcgencmd
get_throttled` (must be 0x0 — any throttling invalidates the number). That's
everything needed to decide Pi 5 vs N100.
