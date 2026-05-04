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
        gnuradio gnuradio-dev gr-osmosdr libvolk-dev pybind11-dev \
        libfftw3-dev \
        soapysdr-tools soapysdr-module-all \
        ffmpeg
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
make -j"$(nproc)" 2>&1 | tee build.log | tail -20
test "${PIPESTATUS[0]}" -eq 0 || \
    { echo "[bootstrap] make failed — see gr-atscplus/build/build.log"; exit 1; }
sudo make install || \
    { echo "[bootstrap] make install failed"; exit 1; }
sudo ldconfig

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
