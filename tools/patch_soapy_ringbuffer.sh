#!/usr/bin/env bash
# patch_soapy_ringbuffer.sh — enlarge the SoapySDRPlay3 USB ring buffer.
#
# THE single biggest quality fix on Linux. The stock SoapySDRPlay3 driver uses a
# 2 MiB / ~83 ms ring buffer that overflows several times a second at 6+ MS/s,
# causing "OsO" sample-overrun events that corrupt the decode. Bumping it to
# 32 MiB / ~1.3 s of headroom removes the great majority of them.
#
# Run this against a CLONED SoapySDRPlay3 source tree, THEN rebuild & install it.
# Re-apply whenever you rebuild SoapySDRPlay3 from upstream.
#
# Usage:  tools/patch_soapy_ringbuffer.sh /path/to/SoapySDRPlay3
set -eu
SRC="${1:?usage: $0 /path/to/SoapySDRPlay3}"
HPP="$SRC/SoapySDRPlay.hpp"
[ -f "$HPP" ] || { echo "Not found: $HPP"; exit 1; }

cp -n "$HPP" "$HPP.orig" 2>/dev/null || true
sed -i -E \
  -e 's/#define[[:space:]]+DEFAULT_BUFFER_LENGTH[[:space:]]+\([0-9]+\)/#define DEFAULT_BUFFER_LENGTH     (262144)/' \
  -e 's/#define[[:space:]]+DEFAULT_NUM_BUFFERS[[:space:]]+\([0-9]+\)/#define DEFAULT_NUM_BUFFERS       (32)/' \
  "$HPP"

echo "Patched $HPP :"
grep -E 'DEFAULT_BUFFER_LENGTH|DEFAULT_NUM_BUFFERS' "$HPP" | sed 's/^/  /'
echo ""
echo "Now rebuild & install:"
echo "  cd $SRC && mkdir -p build && cd build && cmake .. && make -j\$(nproc) && sudo make install && sudo ldconfig"
