#!/bin/bash
# Bootstrap: install gnuradio + build & install the gr-atscplus OOT module.
# Designed to be idempotent so the weekly remote agent can re-run safely.
set -e

DEBIAN_FRONTEND=noninteractive
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v gnuradio-config-info >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq \
        build-essential cmake git pkg-config python3 python3-pip python3-numpy \
        python3-yaml python3-scipy gnuradio gr-osmosdr libvolk-dev \
        gnuradio-dev pybind11-dev libfftw3-dev
fi

mkdir -p "$HERE/gr-atscplus/build"
cd "$HERE/gr-atscplus/build"
cmake .. > cmake.log 2>&1
make -j"$(nproc)" 2>&1 | tail -10
sudo make install
sudo ldconfig

python3 -c "from gnuradio import atscplus; \
print('atscplus blocks:', sorted(b for b in dir(atscplus) if b.startswith('atsc_')))"
