#!/usr/bin/env bash
# stvt_dvr.sh — all-on-the-Pi record-then-watch DVR.
#
# The Pi 4 CANNOT decode ATSC live (~0.33-0.46x real-time, proven core-count
# floor). So instead of decoding live, the Pi RECORDS raw IQ (the SDR does that
# at ~0 CPU), DECODES it offline on the Pi (slower than real-time but it
# finishes), and PLAYS the result. Everything on the Pi, no second machine.
#
#   stvt_dvr.sh record <rf> <minutes> [name]   capture IQ from the SDR
#   stvt_dvr.sh decode <name>                  offline-decode IQ -> playable .ts
#   stvt_dvr.sh watch  <name>                  play the decoded .ts
#   stvt_dvr.sh auto   <rf> <minutes> [name]   record, then decode (then watch)
#   stvt_dvr.sh verify [rf] [secs]             health check: is the channel giving stable video?
#   stvt_dvr.sh scan   [rf...]                 rank candidate channels by decode quality, pick the best
#   stvt_dvr.sh list                           show recordings + disk
#
# DISK (the binding constraint): raw IQ is CF32 = ~3.84 GB/min (~230 GB/hr). The
# decoded .ts is only ~65 MB/min, so the huge IQ is DELETED after a good decode
# (keep it with STVT_DVR_KEEP_IQ=1). For long shows put STVT_DVR_DIR on a USB SSD.
#
# Config (env): STVT_DVR_DIR (~/stvt_dvr), STVT_DVR_EQ (long=quality | stock=~30%
# faster), STVT_DVR_KEEP_IQ (0).
#
# NOTE 2026-06-09: written overnight, NOT yet tested end-to-end (the SDR was busy
# with the soak). Verify record+decode+play against the SDR before trusting it.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${STVT_DVR_DIR:-$HOME/stvt_dvr}"
mkdir -p "$DIR"
# IQ format: cf32 = complex float32 (8 B/sample, default, fully tested) or
# cs16 = interleaved int16 (4 B/sample, HALF the disk -> ~2x record time).
# cs16 scaling is verified exact; SDR-record test still pending (see docs/pi_dvr.md).
FMT="${STVT_DVR_FORMAT:-cs16}"   # cs16 default: validated, and required for the 29.6MB/s SD card
case "$FMT" in
  cs16) EXT=cs16; GB_PER_MIN=1.92;;
  *)    FMT=cf32; EXT=cf32; GB_PER_MIN=3.84;;
esac

die(){ echo "[dvr] ERROR: $*" >&2; exit 1; }
free_gb(){ df -BG --output=avail "${1:-$DIR}" 2>/dev/null | tail -1 | tr -dc '0-9'; }

# STVT_DVR_RAM=1 writes the (transient) IQ to RAM (/dev/shm) instead of the SD
# card, so the SD's ~30 MB/s write can't drop samples — guaranteed-clean capture
# for SHORT clips (tmpfs is ~half RAM, ~2 min of cs16). The kept .ts still lands
# in $DIR. Measured: SD capture ~99.7% aligned vs RAM ~99.99%.
IQDIR="$DIR"
if [ "${STVT_DVR_RAM:-0}" = 1 ]; then IQDIR=/dev/shm/stvt_dvr_iq; mkdir -p "$IQDIR"; fi

ensure_sdr_free(){
  # all-on-Pi DVR talks to the SDR directly; release anything else holding it
  if systemctl is-active --quiet soapyremote-server 2>/dev/null; then
    echo "[dvr] stopping soapyremote-server to free the SDR"; sudo systemctl stop soapyremote-server
  fi
  for p in $(pgrep -f 'pi_server_soak|tv_live.py' 2>/dev/null); do
    [ "$(cat /proc/$p/comm 2>/dev/null)" = python3 ] && kill -INT "$p" 2>/dev/null
  done
  sleep 2
}

cmd_record(){
  local RF="${1:-34}"   # default RF34 (clean 99.99%-aligned channel)
  local MIN="${2:?usage: record <rf> <minutes> [name]}"
  local NAME="${3:-rec_$(date +%Y%m%d_%H%M%S)_rf${RF}}"
  local IQ="$IQDIR/$NAME.$EXT"
  local need avail maxmin
  need=$(awk "BEGIN{printf \"%d\", $MIN*$GB_PER_MIN+3}")     # +3 GB margin
  avail=$(free_gb "$IQDIR")
  maxmin=$(awk "BEGIN{printf \"%d\", ($avail-3)/$GB_PER_MIN}")
  echo "[dvr] ${MIN}min $FMT IQ needs ~${need}GB; ${avail}GB free in $IQDIR (max ~${maxmin}min)"
  [ "${avail:-0}" -lt "$need" ] && die "not enough disk. Shorter recording, STVT_DVR_FORMAT=cs16 (half size), or STVT_DVR_DIR=<usb drive>."
  ensure_sdr_free
  echo "[dvr] recording RF$RF for ${MIN}min ($FMT) -> $IQ  (Ctrl-C stops early but keeps the file)"
  python3 "$HERE/record_iq.py" --rf "$RF" --seconds $((MIN*60)) --out "$IQ" --format "$FMT" \
      --ifgr "${STVT_IFGR:-50}" --rfgain-sel "${STVT_RFGAIN_SEL:-5}" --antenna "${STVT_ANTENNA:-Antenna A}" \
      || die "record_iq.py failed"
  echo "[dvr] recorded $(du -h "$IQ" 2>/dev/null|cut -f1).  Next: $0 decode $NAME"
}

cmd_decode(){
  local NAME="${1:?usage: decode <name>}"
  local TS="$DIR/$NAME.ts" IQ=""
  # find the IQ by either extension, in the RAM dir (if STVT_DVR_RAM) or $DIR;
  # tv_replay auto-detects format from the file. The .ts always lands in $DIR.
  for d in "$IQDIR" "$DIR"; do for e in cs16 cf32; do
    [ -f "$d/$NAME.$e" ] && IQ="$d/$NAME.$e" && break 2; done; done
  [ -n "$IQ" ] || die "no IQ file for '$NAME' in $IQDIR or $DIR  (run: $0 list)"
  local dur eta eq bpers
  case "$IQ" in *.cs16) bpers=32e6;; *) bpers=64e6;; esac   # bytes per signal-second
  dur=$(python3 -c "import os;print(os.path.getsize('$IQ')/$bpers)")
  eq="${STVT_DVR_EQ:-long}"     # long=best quality; stock=~30% faster
  local rate; [ "$eq" = stock ] && rate=0.43 || rate=0.33
  eta=$(awk "BEGIN{printf \"%.0f\", $dur/$rate/60}")
  echo "[dvr] decoding '$NAME' (${dur}s signal, EQ=$eq) at ~${rate}x -> ETA ~${eta}min"
  # STVT_EQ_S16=1 default: int16 NEON equalizer data path (commit b0eadbc).
  # Same-IQ A/B: -13% decode wall, segs_aligned bit-identical. Opt out with 0.
  STVT_RS=stock STVT_VITERBI=hard STVT_EQ="$eq" STVT_SPS="${STVT_SPS:-1.1}" \
    STVT_RRC_SYMS="${STVT_RRC_SYMS:-4}" STVT_TEISCRUB=1 STVT_RXF_FUSED=1 \
    STVT_EQ_S16="${STVT_EQ_S16:-1}" \
    python3 "$HERE/tv_replay.py" --iq "$IQ" --out "$TS" --log "$DIR/$NAME.decode.log" \
    || die "tv_replay.py failed (see $DIR/$NAME.decode.log)"
  echo "[dvr] decoded -> $TS ($(du -h "$TS" 2>/dev/null|cut -f1))"
  if [ "${STVT_DVR_KEEP_IQ:-0}" != "1" ]; then
    rm -f "$IQ"; echo "[dvr] removed $(basename "$IQ") to reclaim disk (keep next time with STVT_DVR_KEEP_IQ=1)"
  fi
  echo "[dvr] watch:  $0 watch $NAME"
}

cmd_watch(){
  local NAME="${1:?usage: watch <name>}"
  local TS="$DIR/$NAME.ts"
  [ -f "$TS" ] || die "no decoded TS: $TS  (run: $0 decode $NAME)"
  # ATSC video is MPEG-2 (no Pi HW decode) -> software decode (fast, ~5x) but the
  # Pi's GPU video-OUTPUT can't present 1080 at full rate with mpv's default heavy
  # quality, so it drops frames. Fixes (measured on a Pi 4): --profile=fast + cheap
  # scalers + NO deinterlace (1080i sw-deint is too heavy) => full-rate, in sync.
  # Multi-program mux: extract the HD program (first 1920/1280-wide) to a seekable
  # single-program file so mpv doesn't pick a bad SD track AND --start can skip the
  # ~1-2s startup glitch (chain lock-in + decoder warmup).
  local hd hdfile="$DIR/$NAME.hd.ts"
  hd=$(ffprobe -v error -show_entries program=program_id:stream=width -of compact "$TS" 2>/dev/null \
       | grep -oE "program_id=[0-9]+\|stream\|width=(1920|1280)" | grep -oE "program_id=[0-9]+" | grep -oE "[0-9]+" | head -1)
  if [ -n "$hd" ]; then
    [ -f "$hdfile" ] || { echo "[dvr] extracting HD program $hd..."; \
      ffmpeg -loglevel error -y -i "$TS" -map 0:p:$hd -c copy "$hdfile" 2>/dev/null; }
    TS="$hdfile"
  fi
  local audio="--ao=pipewire"; [ -n "${STVT_DVR_AUDIO:-}" ] && audio="--ao=pipewire --audio-device=$STVT_DVR_AUDIO"
  echo "[dvr] playing $NAME (Pi-tuned: fast profile, no deinterlace, skip ${STVT_DVR_START:-1.5}s startup)"
  exec mpv --hwdec=no --vo=gpu --profile=fast \
    --scale=bilinear --cscale=bilinear --dither=no --deinterlace="${STVT_DVR_DEINT:-no}" \
    --cache=yes --start="${STVT_DVR_START:-1.5}" --force-window=yes --volume=100 $audio \
    "${@:2}" "$TS"
}

cmd_auto(){
  local RF="${1:-34}" MIN="${2:?usage: auto [rf] <minutes> [name]}" NAME="${3:-rec_$(date +%Y%m%d_%H%M%S)_rf${RF}}"
  cmd_record "$RF" "$MIN" "$NAME"
  cmd_decode "$NAME"
  echo "[dvr] ready. watch with:  $0 watch $NAME"
}

cmd_verify(){
  # quick "is this channel/setup giving stable video?" check via the acceptance
  # gate (records a clean clip to RAM, decodes, reports segs_aligned + PASS/FAIL).
  local RF="${1:-34}" SECS="${2:-20}"
  echo "[dvr] verify: ${SECS}s RF$RF -> acceptance gate (want segs_aligned >= 98%)"
  bash "$HERE/stvt_dvr_accept.sh" "$RF" "$SECS" "${STVT_DVR_EQ:-long}"
}

cmd_scan(){
  # Auto best-channel: test-decode a SHORT clip (to RAM) from each candidate RF,
  # rank by segs_aligned, so you don't need to know which channel decodes clean.
  # Usage: stvt_dvr.sh scan [rf...]   (default: a set of common strong locals)
  local chans="${*:-34 36 31 35 15 7}"
  local secs="${STVT_DVR_SCAN_SECS:-6}"
  local D=/dev/shm/dvr_scan; mkdir -p "$D"
  ensure_sdr_free
  echo "[dvr] scanning [$chans] @ ${secs}s each to RAM — want segs_aligned >= 98%"
  printf "  %-5s %-9s %s\n" "RF" "aligned" ""
  local best="" bestal=0
  for rf in $chans; do
    timeout $((secs+30)) python3 "$HERE/record_iq.py" --rf "$rf" --seconds "$secs" \
      --out "$D/s.cs16" --format cs16 --ifgr "${STVT_IFGR:-50}" --rfgain-sel "${STVT_RFGAIN_SEL:-5}" >/dev/null 2>&1
    STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long STVT_SPS=1.1 STVT_RRC_SYMS=4 STVT_TEISCRUB=1 \
      STVT_RXF_FUSED=1 STVT_IQ_FORMAT=cs16 \
      timeout $((secs*5+60)) python3 "$HERE/tv_replay.py" --iq "$D/s.cs16" --out "$D/s.ts" --log "$D/s.log" >/dev/null 2>&1
    local al; al=$(grep -oE "segs_aligned=[0-9]+ \([0-9.]+%\)" "$D/s.log" 2>/dev/null | tail -1 | grep -oE "\([0-9.]+%\)" | tr -dc '0-9.')
    al=${al:-0}
    local mark="  weak"; awk "BEGIN{exit !($al>=98)}" && mark="  GOOD"
    printf "  %-5s %-9s %s\n" "$rf" "${al}%" "$mark"
    awk "BEGIN{exit !($al>$bestal)}" && { best=$rf; bestal=$al; }
    rm -f "$D/s.cs16" "$D/s.ts" "$D/s.log"
  done
  rm -rf "$D"
  [ -n "$best" ] && echo "[dvr] best: RF$best (${bestal}% aligned) -> record it:  $0 auto $best <minutes>"
}

cmd_list(){
  echo "[dvr] $DIR  ($(free_gb)GB free)"
  ls -lh "$DIR"/*.ts "$DIR"/*.cf32 "$DIR"/*.cs16 2>/dev/null | awk '{print "  "$5"  "$9}' || echo "  (no recordings yet)"
}

case "${1:-help}" in
  record) shift; cmd_record "$@";;
  decode) shift; cmd_decode "$@";;
  watch)  shift; cmd_watch "$@";;
  auto)   shift; cmd_auto "$@";;
  verify) shift; cmd_verify "$@";;
  scan)   shift; cmd_scan "$@";;
  list)   cmd_list;;
  *) grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -28;;
esac
