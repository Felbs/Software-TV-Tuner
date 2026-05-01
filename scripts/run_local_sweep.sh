#!/bin/bash
# Real-IQ regression sweep — run all combos against a captured ATSC IQ
# file and print a scoreboard with RS-clean %, SD frames, and HD frames.
#
# Usage:
#   bash scripts/run_local_sweep.sh <path-to-cs16-file>
#
# IQ file should be 8 MS/s CS16 captured per docs/proven_capture_recipe.md.
# Requires ffmpeg installed (sudo apt install ffmpeg).
#
# Metrics per combo:
#   clean%   RS-decoded packets with TEI=0 and PID in KEEP set
#   sd       Frames ffmpeg extracts from SD stream (-map 0:0, 720x480)
#   hd       Frames ffmpeg extracts from HD stream (-map 0:2, 1920x1080)
#
# SD subchannels render at ~60% RS-clean; HD primary needs ~85%+.

set -e

if [ -z "$1" ] || [ ! -f "$1" ]; then
    echo "Usage: $0 <path-to-rf-capture.cs16>"
    exit 2
fi

IQ="$1"
SWEEP_DIR=$(mktemp -d)
ROOT="$(dirname "$(dirname "$(readlink -f "$0")")")"
# ts_tei_scrub.py lives in a companion SDR project — set TS_TEI_SCRUB env var
# to override, otherwise we'll skip the scrub step and show raw RS-clean only
SCRUB="${TS_TEI_SCRUB:-$(dirname "$ROOT")/ts_tei_scrub.py}"

echo "=== gr-atsc-plus combo sweep with SD+HD frame counts ==="
echo "Input: $IQ ($(du -h "$IQ" | cut -f1))"
echo "Sweep dir: $SWEEP_DIR"
echo

# Discover all combo names from combos.yaml
COMBOS=$(python3.12 -c "import yaml; print(' '.join(c['name'] for c in yaml.safe_load(open('$ROOT/combos.yaml'))['combos']))")
echo "Combos to test: $(echo $COMBOS | wc -w)"
echo

count_frames() {
    # Extract frames from a TS file at a given stream index. ffmpeg writes
    # "frame=N" to stderr; the LAST one is the final count.
    ffmpeg -nostdin -hide_banner -i "$1" -map "$2" -f null - 2>&1 \
        | grep -oP 'frame=\s*\K\d+' | tail -1 | tr -d '\n' || echo "0"
}

score_combo() {
    local combo=$1
    local ts="$SWEEP_DIR/${combo}.ts"
    local scrubbed="$SWEEP_DIR/${combo}_scr.ts"

    # RS-clean %
    local clean_line=$(python3.12 - "$ts" <<'PYEOF'
import sys
d = open(sys.argv[1], 'rb').read()
n = len(d) // 188
if n == 0:
    print("0.0 0")
    sys.exit()
KEEP = {0, 0x1FFB, 0x1FFF} | {p|s for p in range(0x30, 0x90, 0x10) for s in range(6)}
c = sum(1 for i in range(n)
        if d[i*188] == 0x47
        and not ((d[i*188+1] >> 7) & 1)
        and (((d[i*188+1] & 0x1F) << 8) | d[i*188+2]) in KEEP)
print(f"{100*c/n:.1f} {n}")
PYEOF
)
    local clean_pct=$(echo $clean_line | cut -d' ' -f1)
    local total=$(echo $clean_line | cut -d' ' -f2)

    # Scrub TEI then count frames
    if [ -f "$SCRUB" ]; then
        python3 "$SCRUB" "$ts" "$scrubbed" 2>/dev/null >/dev/null || cp "$ts" "$scrubbed"
    else
        cp "$ts" "$scrubbed"
    fi

    local sd=$(count_frames "$scrubbed" "0:0")
    local hd=$(count_frames "$scrubbed" "0:2")

    printf "  %-32s clean=%5s%%  sd=%4s  hd=%4s  pkts=%s\n" "$combo" "$clean_pct" "${sd:-0}" "${hd:-0}" "$total"
}

for combo in $COMBOS; do
    python3.12 "$ROOT/run_combo.py" "$IQ" "$SWEEP_DIR/${combo}.ts" "$combo" 2>/dev/null
    score_combo "$combo"
done

echo
echo "Sorted leaderboard (HD frames desc, then SD, then clean%):"
echo

for combo in $COMBOS; do
    python3.12 - "$SWEEP_DIR/${combo}.ts" "$combo" "$SWEEP_DIR/${combo}_scr.ts" <<'PYEOF'
import sys, subprocess
ts, name, scrubbed = sys.argv[1], sys.argv[2], sys.argv[3]
d = open(ts, 'rb').read()
n = len(d) // 188
if n == 0:
    print(f"0  0  0.0  {name}")
    sys.exit()
KEEP = {0, 0x1FFB, 0x1FFF} | {p|s for p in range(0x30, 0x90, 0x10) for s in range(6)}
c = sum(1 for i in range(n)
        if d[i*188] == 0x47
        and not ((d[i*188+1] >> 7) & 1)
        and (((d[i*188+1] & 0x1F) << 8) | d[i*188+2]) in KEEP)
def frames(idx):
    try:
        out = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-i', scrubbed,
                              '-map', idx, '-f', 'null', '-'],
                             capture_output=True, text=True, timeout=120)
        import re
        m = re.findall(r'frame=\s*(\d+)', out.stderr)
        return int(m[-1]) if m else 0
    except Exception:
        return 0
hd = frames('0:2'); sd = frames('0:0')
print(f"{hd:5d}  {sd:5d}  {100*c/n:5.1f}  {name}")
PYEOF
done | sort -rn

rm -rf "$SWEEP_DIR"
