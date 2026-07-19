#!/bin/bash
# Bootstrap: install GNU Radio + build & install the gr-atscplus OOT module
# + install the runtime deps tv_tuner.py needs to play / record / stream.
#
# Tested on Ubuntu 22.04 / 24.04 (apt-based). Idempotent — safe to re-run.

set -e

export DEBIAN_FRONTEND=noninteractive
APT="sudo DEBIAN_FRONTEND=noninteractive apt-get"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[bootstrap] === Software TV Tuner — Linux installer ==="

# ── 1. System packages ────────────────────────────────────────────
if ! command -v gnuradio-config-info >/dev/null 2>&1 \
   || ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[bootstrap] installing GNU Radio + ffmpeg + build tools..."
    $APT update -qq
    $APT install -y -qq \
        build-essential cmake git pkg-config \
        python3 python3-pip python3-numpy python3-yaml python3-scipy \
        python3-soapysdr \
        gnuradio gnuradio-dev gr-osmosdr libvolk-dev pybind11-dev \
        libfftw3-dev \
        soapysdr-tools soapysdr-module-all \
        ffmpeg mpv \
        usbutils
fi

# ── 2. Build & install gr-atscplus ────────────────────────────────
# Fast path: when invoked just to add the SDRplay stack and the decoder
# is already installed, don't rebuild it (a full rebuild takes minutes).
if [[ " $* " == *" --sdrplay"* ]] && [[ " $* " != *" --rebuild "* ]] \
   && python3 -c "from gnuradio import atscplus" 2>/dev/null; then
    echo "[bootstrap] gr-atscplus already installed — skipping rebuild"
    echo "[bootstrap]   (pass --rebuild to force after a git pull)"
    JOBS="${MAKE_JOBS:-$(nproc)}"; [ "$JOBS" -gt 8 ] && JOBS=8
else
echo "[bootstrap] building gr-atscplus OOT module..."
mkdir -p "$HERE/gr-atscplus/build"
cd "$HERE/gr-atscplus/build"
# Clean stale CMake cache so a re-run picks up renames / new files in
# the source tree (e.g. the cmake/Modules/*.cmake config files).
rm -rf CMakeCache.txt CMakeFiles
cmake .. 2>&1 | tee cmake.log
# Use PIPESTATUS to surface the build's exit code through tee.
# Cap parallelism: -j$(nproc) on a big CPU inside a memory-capped VM
# (WSL especially) can OOM the compiler (GCC internal compiler error).
JOBS="${MAKE_JOBS:-$(nproc)}"
[ "$JOBS" -gt 8 ] && JOBS=8
make -j"$JOBS" 2>&1 | tee build.log | tail -20
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
    echo "[bootstrap] parallel build failed (often out-of-memory) — retrying -j2..."
    make -j2 2>&1 | tee -a build.log | tail -5
    test "${PIPESTATUS[0]}" -eq 0 || \
        { echo "[bootstrap] make failed — see gr-atscplus/build/build.log"; exit 1; }
fi
sudo make install || \
    { echo "[bootstrap] make install failed"; exit 1; }
sudo ldconfig
cd "$HERE"
fi

# ── 2b. Optional: SDRplay driver stack (`./bootstrap.sh --sdrplay`) ──
# RTL-SDR etc. work out of the box via soapysdr-module-all; SDRplay
# needs the vendor API (interactive EULA) + the SoapySDRPlay3 plugin.
if [[ " $* " == *" --sdrplay "* ]] || [[ " $* " == *" --sdrplay-rebuild "* ]]; then
    if [[ " $* " != *" --sdrplay-rebuild "* ]] \
       && SoapySDRUtil --info 2>/dev/null | grep -qi sdrplay; then
        echo "[bootstrap] SoapySDR already has an sdrplay module — skipping"
        echo "[bootstrap]   (if it was built without the ring-buffer patch, re-run"
        echo "[bootstrap]    with --sdrplay-rebuild to rebuild it patched)"
    elif ldconfig -p 2>/dev/null | grep -q libsdrplay_api \
         || [ -e /usr/local/lib/libsdrplay_api.so ]; then
        echo "[bootstrap] vendor API found — building SoapySDRPlay3..."
        $APT install -y -qq libsoapysdr-dev
        SPTMP=$(mktemp -d)
        git clone -q https://github.com/pothosware/SoapySDRPlay3.git "$SPTMP/sp3"
        # The ring-buffer patch is THE big Linux quality fix: the stock
        # 2 MiB ring overflows several times a second at 8 MS/s (OsO).
        "$HERE/tools/patch_soapy_ringbuffer.sh" "$SPTMP/sp3"
        mkdir -p "$SPTMP/sp3/build"
        cd "$SPTMP/sp3/build"
        cmake .. && make -j"$JOBS" && sudo make install && sudo ldconfig
        cd "$HERE"
        echo "[bootstrap] probe:"
        SoapySDRUtil --probe 2>/dev/null | head -6 || true
    else
        echo "[bootstrap] SDRplay vendor API not installed. Do this first"
        echo "  (the installer's EULA needs an interactive run AT A REAL"
        echo "   TERMINAL — piping input to it makes it hang at 100% CPU):"
        echo "    wget https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-3.15.2.run"
        echo "    chmod +x SDRplay_RSP_API-Linux-3.15.2.run && sudo ./SDRplay_RSP_API-Linux-3.15.2.run"
        echo "    sudo systemctl enable --now sdrplay"
        echo "  then re-run:  ./bootstrap.sh --sdrplay"
        SDRPLAY_PENDING=1
    fi
fi

# ── 2c. USB power & buffer setup (Linux needs this done by hand) ──
# On Windows the SDR vendor driver disables USB selective-suspend for
# the radio; on Linux nothing does, and an autosuspending SDR drops
# samples or "vanishes" mid-stream. Also raise the kernel's usbfs URB
# buffer cap — the 16 MB default under-buffers an 8 MS/s SDR stream.
# Both persist across reboots (udev rule + tmpfiles.d).
if [ -d /etc/udev/rules.d ]; then
    sudo tee /etc/udev/rules.d/66-stvt-sdr.rules >/dev/null <<'RULES'
# Software-TV-Tuner: keep SDRs fully powered (no USB autosuspend).
# SDRplay (RSP1A / RSPdx / RSPduo / RSP2...)
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="1df7", TEST=="power/control", ATTR{power/control}="on"
# RTL-SDR (RTL2832U)
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="0bda", ATTR{idProduct}=="2838", TEST=="power/control", ATTR{power/control}="on"
RULES
    sudo udevadm control --reload-rules 2>/dev/null || true
    sudo udevadm trigger --subsystem-match=usb 2>/dev/null || true
    echo 'w! /sys/module/usbcore/parameters/usbfs_memory_mb - - - - 1000' \
        | sudo tee /etc/tmpfiles.d/stvt-usbfs.conf >/dev/null
    echo 1000 | sudo tee /sys/module/usbcore/parameters/usbfs_memory_mb >/dev/null 2>&1 || true
    echo "[bootstrap] USB set up: autosuspend off for SDRs, usbfs buffer raised."
    echo "[bootstrap]   If your SDR was already plugged in, unplug/replug it once."
else
    echo "[bootstrap] (no /etc/udev — container/WSL? skipping USB power setup)"
fi

# ── 3. Verify the new blocks are importable ───────────────────────
python3 -c "from gnuradio import atscplus; \
print('[bootstrap] atscplus blocks:', \
sorted(b for b in dir(atscplus) if b.startswith('atsc_')))"

# ── 4. Optional: extras for tv_player.py (decoupled A/V player) ──
# Only needed for `--player magic`; the default player path works
# without them. Use apt (PEP 668 makes bare `pip install` fail on
# Ubuntu 23.04+/Mint 21+ with "externally-managed-environment").
echo "[bootstrap] installing tv_player.py runtime deps (optional)..."
$APT install -y -qq python3-opencv python3-sounddevice 2>/dev/null \
    || echo "[bootstrap] (skipped — only needed for --player magic; install python3-opencv python3-sounddevice yourself if you want it)"

# ── 5. Friendly next step ────────────────────────────────────────
echo
if [ "${SDRPLAY_PENDING:-0}" = "1" ]; then
    echo "[bootstrap] === Done, EXCEPT the SDRplay driver ==="
    echo "[bootstrap] !! Your SDRplay is NOT usable yet: install the vendor"
    echo "[bootstrap] !! API (instructions above), then re-run:"
    echo "[bootstrap] !!     ./bootstrap.sh --sdrplay"
else
    echo "[bootstrap] === Done ==="
fi
echo "[bootstrap] Check it:  python3 $HERE/tools/doctor.py"
echo "[bootstrap] Try it:    python3 $HERE/tools/tv_tuner.py"
echo "[bootstrap] First run will scan your local channels (~3 min)."
