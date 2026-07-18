#!/bin/bash
# Bootstrap: install GNU Radio + build & install the gr-atscplus OOT module
# + install the runtime deps tv_tuner.py needs to play / record / stream.
#
# Tested on Ubuntu 22.04 / 24.04 (apt-based). Idempotent — safe to re-run.

set -e

DEBIAN_FRONTEND=noninteractive
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[bootstrap] === Software TV Tuner — Linux installer ==="

# ── 1. System packages ────────────────────────────────────────────
if ! command -v gnuradio-config-info >/dev/null 2>&1 \
   || ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[bootstrap] installing GNU Radio + ffmpeg + build tools..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq \
        build-essential cmake git pkg-config \
        python3 python3-pip python3-numpy python3-yaml python3-scipy \
        python3-soapysdr \
        gnuradio gnuradio-dev gr-osmosdr libvolk-dev pybind11-dev \
        libfftw3-dev \
        soapysdr-tools soapysdr-module-all \
        ffmpeg \
        usbutils
fi

# ── 2. Build & install gr-atscplus ────────────────────────────────
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

# ── 2b. Optional: SDRplay driver stack (`./bootstrap.sh --sdrplay`) ──
# RTL-SDR etc. work out of the box via soapysdr-module-all; SDRplay
# needs the vendor API (interactive EULA) + the SoapySDRPlay3 plugin.
if [[ " $* " == *" --sdrplay "* ]]; then
    if SoapySDRUtil --info 2>/dev/null | grep -qi sdrplay; then
        echo "[bootstrap] SoapySDR already has an sdrplay module — skipping"
    elif ldconfig -p 2>/dev/null | grep -q libsdrplay_api \
         || [ -e /usr/local/lib/libsdrplay_api.so ]; then
        echo "[bootstrap] vendor API found — building SoapySDRPlay3..."
        sudo apt-get install -y -qq libsoapysdr-dev
        SPTMP=$(mktemp -d)
        git clone -q https://github.com/pothosware/SoapySDRPlay3.git "$SPTMP/sp3"
        mkdir -p "$SPTMP/sp3/build"
        cd "$SPTMP/sp3/build"
        cmake .. && make -j"$JOBS" && sudo make install && sudo ldconfig
        cd "$HERE"
        echo "[bootstrap] probe:"
        SoapySDRUtil --probe 2>/dev/null | head -6 || true
    else
        echo "[bootstrap] SDRplay vendor API not installed. Do this first"
        echo "  (the installer's EULA needs an interactive run):"
        echo "    wget https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-3.15.2.run"
        echo "    chmod +x SDRplay_RSP_API-Linux-3.15.2.run && sudo ./SDRplay_RSP_API-Linux-3.15.2.run"
        echo "    sudo systemctl enable --now sdrplay"
        echo "  then re-run:  ./bootstrap.sh --sdrplay"
    fi
fi

# ── 3. Verify the new blocks are importable ───────────────────────
python3 -c "from gnuradio import atscplus; \
print('[bootstrap] atscplus blocks:', \
sorted(b for b in dir(atscplus) if b.startswith('atsc_')))"

# ── 4. Optional: extras for tv_player.py (decoupled A/V player) ──
# These are only needed if you launch with `--player magic`. Skip
# the install if pip isn't writable; the default ffplay path works
# without them.
echo "[bootstrap] installing tv_player.py runtime deps (optional)..."
python3 -m pip install --user opencv-python sounddevice 2>/dev/null \
    || echo "[bootstrap] (skipped — install opencv-python sounddevice yourself if you want --player magic)"

# ── 5. Friendly next step ────────────────────────────────────────
echo
echo "[bootstrap] === Done ==="
echo "[bootstrap] Try it:  python3 $HERE/tools/tv_tuner.py"
echo "[bootstrap] First run will scan your local channels (~3 min)."
