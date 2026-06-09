#!/usr/bin/env bash
# stvt_dvr_accept.sh — "stable video" acceptance test (the autobot's quality gate).
# Records a clean clip to RAM (no SD-throughput confound), decodes it, and reports
# PASS/FAIL on the chain's own segs_aligned% (the reliable metric — ffmpeg frame
# counts on raw multi-program TS are noisy) plus a frame-decode sanity check on the
# highest-resolution program. Prints one CSV line: ts,rf,secs,align,relocks,hd_dim,frames,verdict
#
# Usage: stvt_dvr_accept.sh [rf] [seconds] [eq]    (defaults: 34 30 long)
# PASS = segs_aligned >= ALIGN_MIN (default 98) and frames > 0.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RF="${1:-34}"; SECS="${2:-30}"; EQ="${3:-long}"
ALIGN_MIN="${ACCEPT_ALIGN_MIN:-98}"
D=/dev/shm/accept; mkdir -p "$D"
stamp(){ date '+%H:%M:%S'; }

# free the SDR (graceful — never kill -9, that wedges the SDRplay API)
systemctl is-active --quiet soapyremote-server 2>/dev/null && sudo systemctl stop soapyremote-server
for p in $(pgrep -f 'pi_server_soak|tv_live.py' 2>/dev/null); do
  [ "$(cat /proc/$p/comm 2>/dev/null)" = python3 ] && kill -INT "$p" 2>/dev/null
done
sleep 2

timeout $((SECS+40)) python3 "$HERE/record_iq.py" --rf "$RF" --seconds "$SECS" \
  --out "$D/a.cs16" --format cs16 --ifgr "${STVT_IFGR:-50}" --rfgain-sel 5 >"$D/rec.log" 2>&1
rec_oso=$(grep -ic OsO "$D/rec.log")

STVT_RS=stock STVT_VITERBI=hard STVT_EQ="$EQ" STVT_SPS="${STVT_SPS:-1.1}" \
  STVT_RRC_SYMS="${STVT_RRC_SYMS:-4}" STVT_TEISCRUB=1 STVT_RXF_FUSED=1 STVT_IQ_FORMAT=cs16 \
  timeout $((SECS*5+120)) python3 "$HERE/tv_replay.py" --iq "$D/a.cs16" --out "$D/a.ts" \
  --log "$D/dec.log" >/dev/null 2>&1

align=$(grep -oE "segs_aligned=[0-9]+ \([0-9.]+%\)" "$D/dec.log" | tail -1 | grep -oE "\([0-9.]+%\)" | tr -dc '0-9.')
relocks=$(grep -oE "relocks=[0-9]+" "$D/dec.log" | grep -oE "[0-9]+")
# highest-resolution program's frame yield (sanity only)
hd_prog=$(ffprobe -v error -show_entries program=program_id:stream=width,height -of compact "$D/a.ts" 2>/dev/null \
          | grep -oE "program_id=[0-9]+\|stream\|width=1920|program_id=[0-9]+\|stream\|width=1280" | grep -oE "program_id=[0-9]+" | grep -oE "[0-9]+" | head -1)
frames=0; hd_dim="?"
if [ -n "${hd_prog:-}" ]; then
  out=$(timeout 90 ffmpeg -hide_banner -i "$D/a.ts" -map 0:p:$hd_prog:v:0 -t 12 -f null - 2>&1)
  frames=$(echo "$out" | grep -oE "frame= *[0-9]+" | tail -1 | tr -dc 0-9)
  hd_dim=$(ffprobe -v error -select_streams v -show_entries stream=width,height -of csv=p=0 "$D/a.ts" 2>/dev/null | grep -E "1920|1280" | head -1 | tr ',' x)
fi
align=${align:-0}; frames=${frames:-0}
verdict=FAIL
awk "BEGIN{exit !($align >= $ALIGN_MIN)}" && [ "${frames:-0}" -gt 0 ] && verdict=PASS

echo "$(date '+%F %T'),$RF,$SECS,$align,${relocks:-?},${hd_dim:-?},${frames:-0},$verdict (rec_oso=$rec_oso, eq=$EQ)"
rm -f "$D/a.cs16" "$D/a.ts"
[ "$verdict" = PASS ]
