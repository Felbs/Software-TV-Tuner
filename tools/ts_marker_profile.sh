#!/bin/bash
# ts_marker_profile.sh — analyze live.ts to characterize the "we get
# picture markers but no seq_headers" mystery. Splits the TS into
# 5 chronological windows, counts each marker per window, and looks
# for patterns that might explain the systematic absence of seq_header.
#
# Usage: ~/ts_marker_profile.sh [path-to-ts]

set -u
TS="${1:-/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts}"
SZ=$(stat -c%s "$TS" 2>/dev/null || echo 0)
if [ "$SZ" -lt 50000000 ]; then
    echo "TS too small ($SZ bytes) — need at least 50MB"
    exit 1
fi

echo "=== TS marker profile ==="
echo "file: $TS  total=$((SZ/1024/1024)) MB"
echo ""
echo "[window]  seq_header  GOP   picture  slice0      slice1      slice2"
echo "          0x000001B3  ..B8  ..0100   0x00000101  0x00000102  0x000001A0"
echo "          ----------  ----  -------  ----------  ----------  ----------"

WIN_SZ=$((SZ / 5))
for i in 0 1 2 3 4; do
    OFFSET=$((i * WIN_SZ))
    seq=$(dd if="$TS" bs=1024 skip=$((OFFSET/1024)) count=$((WIN_SZ/1024)) status=none 2>/dev/null \
        | grep -aoP '\x00\x00\x01\xb3' | wc -l)
    gop=$(dd if="$TS" bs=1024 skip=$((OFFSET/1024)) count=$((WIN_SZ/1024)) status=none 2>/dev/null \
        | grep -aoP '\x00\x00\x01\xb8' | wc -l)
    pic=$(dd if="$TS" bs=1024 skip=$((OFFSET/1024)) count=$((WIN_SZ/1024)) status=none 2>/dev/null \
        | grep -aoP '\x00\x00\x01\x00' | wc -l)
    sl1=$(dd if="$TS" bs=1024 skip=$((OFFSET/1024)) count=$((WIN_SZ/1024)) status=none 2>/dev/null \
        | grep -aoP '\x00\x00\x01\x01' | wc -l)
    sl2=$(dd if="$TS" bs=1024 skip=$((OFFSET/1024)) count=$((WIN_SZ/1024)) status=none 2>/dev/null \
        | grep -aoP '\x00\x00\x01\x02' | wc -l)
    sla=$(dd if="$TS" bs=1024 skip=$((OFFSET/1024)) count=$((WIN_SZ/1024)) status=none 2>/dev/null \
        | grep -aoP '\x00\x00\x01\xa0' | wc -l)
    printf "  [%d]    %10d  %4d  %7d  %10d  %10d  %10d\n" \
        "$i" "$seq" "$gop" "$pic" "$sl1" "$sl2" "$sla"
done

echo ""
echo "=== overall (full file) ==="
TOT_SEQ=$(grep -aoP '\x00\x00\x01\xb3' "$TS" | wc -l)
TOT_GOP=$(grep -aoP '\x00\x00\x01\xb8' "$TS" | wc -l)
TOT_PIC=$(grep -aoP '\x00\x00\x01\x00' "$TS" | wc -l)
echo "  total seq=$TOT_SEQ  GOP=$TOT_GOP  pic=$TOT_PIC"

echo ""
echo "=== interpretation ==="
if [ "$TOT_SEQ" -eq 0 ] && [ "$TOT_PIC" -gt 100 ]; then
    echo "  pic > 0 but seq_header = 0 across ENTIRE file."
    echo "  Either:"
    echo "    (a) chain produces no seq_headers (alignment / derandomizer bug?)"
    echo "    (b) bit error rate selectively destroys this byte pattern"
    echo "    (c) signal is below the threshold where structural markers survive"
elif [ "$TOT_SEQ" -gt 0 ] && [ "$TOT_PIC" -gt 0 ]; then
    echo "  Both seq_header and pic present — chain CAN decode."
    echo "  Ratio pic/seq = $((TOT_PIC / TOT_SEQ)) (should be 30-60 for healthy)"
fi
