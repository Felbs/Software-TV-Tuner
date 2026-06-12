#!/usr/bin/env bash
# stvt_surf.sh — channel surfer. Change channels up/down right in the mpv
# window like a TV remote: PageUp / PageDown, or scroll the mouse wheel.
# Smooth mpv playback + overlaid CC carry across channels. Switching to a
# subchannel in the SAME mux is fast (no retune); changing mux retunes the
# SDR (a few seconds, like any OTA tuner).
#
# Channel list comes from ~/.tv_tuner/scan.json (run a scan first), sorted
# by virtual channel.
#
# Usage:  tools/stvt_surf.sh
#   STVT_SURF_START=<index>  start on a given channel (default 0)
#   STVT_CC_DELAY=<sec>      caption delay (default 5)
# Stop: Ctrl-C in this terminal (tears everything down), or press q in mpv
#       then Ctrl-C.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
F="$HERE/data/tv_live/live.ts"
SOCK=/tmp/mpv-cc.sock
CCFEED=/tmp/stvt_cc_feed.ts
FIFO=/tmp/stvt_surf.fifo
ICONF=/tmp/stvt_surf_input.conf
CCDELAY="${STVT_CC_DELAY:-5}"

export DISPLAY="${DISPLAY:-:0}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
# Native Linux: local USB SDR (driver=sdrplay, tv_live.py's default) + the
# session's own audio + GPU video. WSLg users override these before running:
#   export STVT_SOAPY_ARGS=driver=remote,remote=127.0.0.1:55132,remote:driver=sdrplay
#   export STVT_STREAM_ARGS=remote:prot=tcp PULSE_SERVER=unix:/mnt/wslg/PulseServer
#   export STVT_MPV_VO=wlshm LIBGL_ALWAYS_SOFTWARE=1
# Pi 5 chain config (2026-06-13) — same trio as stvt_run.sh: fused front-end +
# int16 NEON eq + 8MB GR buffers + FPLL fold = 1.21x real-time. IFGR=50 is the
# DVR-validated gain (59 was the Ryzen-era value).
export STVT_IFGR="${STVT_IFGR:-50}" STVT_RFGAIN_SEL="${STVT_RFGAIN_SEL:-5}" STVT_ANTENNA="${STVT_ANTENNA:-Antenna A}"
export STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long STVT_SPS=1.1 STVT_RRC_SYMS=4
export STVT_RXF_FUSED="${STVT_RXF_FUSED:-1}" STVT_EQ_S16="${STVT_EQ_S16:-1}"
export STVT_MIN_BUF_BYTES="${STVT_MIN_BUF_BYTES:-8388608}" STVT_FPLL_FOLD="${STVT_FPLL_FOLD:-1}"
pactl unload-module module-suspend-on-idle 2>/dev/null || true   # keep audio alive

# --- channel list (rf|program|virtual|callsign), sorted by virtual channel ---
mapfile -t CHANS < <(cd "$HERE" && python3 - <<'PY'
import json, pathlib, tv_tuner as t
scan = json.loads((pathlib.Path.home()/'.tv_tuner/scan.json').read_text())
rows = [r for r in t.expand_channels_from_scan(scan) if not r.get("not_detected")]
def key(r):
    try:
        a, _, b = str(r.get("virtual", "")).partition(".")
        return (int(a), int(b or 0))
    except ValueError:
        return (9999, 0)
for r in sorted(rows, key=key):
    print(f"{r['rf']}|{r['program']}|{r.get('virtual','?')}|{r.get('callsign','?')}")
PY
)
N=${#CHANS[@]}
[ "$N" -gt 0 ] || { echo "no channels in scan.json — run a scan first"; exit 1; }
echo "loaded $N channels"

# --- mpv keybindings -> write a direction into the control fifo ---
cat > "$ICONF" <<EOF
PGUP       run /bin/sh -c "echo up > $FIFO"
PGDWN      run /bin/sh -c "echo down > $FIFO"
WHEEL_UP   run /bin/sh -c "echo up > $FIFO"
WHEEL_DOWN run /bin/sh -c "echo down > $FIFO"
q          run /bin/sh -c "echo quit > $FIFO"
EOF
rm -f "$FIFO"; mkfifo "$FIFO"

CHAIN_PG=""; FEED_PG=""; MPV_PG=""; BR_PG=""; CUR_RF=""

ensure_chain() {  # $1 = rf ; (re)tune only if the RF actually changed
  if [ "$CUR_RF" = "$1" ] && [ -n "$CHAIN_PG" ] && kill -0 "$CHAIN_PG" 2>/dev/null; then
    return
  fi
  [ -n "$CHAIN_PG" ] && kill -- -"$CHAIN_PG" 2>/dev/null
  sleep 3; rm -f "$F"
  setsid bash -c "exec python3 '$HERE/tv_live.py' --rf $1 --rotate-gb ${STVT_ROTATE_GB:-16} > '$HERE/data/tv_live/tv_tuner.tv_live.log' 2>&1" </dev/null >/dev/null 2>&1 &
  CHAIN_PG=$!; CUR_RF="$1"
  # 10MB (~4s of TS) is enough cushion for the player's tail/probe — the
  # old 40MB threshold added ~12s of dead air to every cross-mux change.
  for i in $(seq 1 25); do [ "$(stat -c%s "$F" 2>/dev/null || echo 0)" -gt 10000000 ] && break; sleep 1; done
}

stop_player() {
  for pg in "$MPV_PG" "$FEED_PG" "$BR_PG"; do [ -n "$pg" ] && kill -- -"$pg" 2>/dev/null; done
  MPV_PG=""; FEED_PG=""; BR_PG=""
}

start_player() {  # $1 = program
  local p="$1"
  setsid bash -c "tail -s 0.1 -c 20000000 -F '$F' | ffmpeg -hide_banner -loglevel error -i pipe:0 -map 0:p:$p -c copy -f mpegts '$CCFEED'" </dev/null >/dev/null 2>&1 &
  FEED_PG=$!
  rm -f "$SOCK"
  # mpv gets the Pi tune (fast profile, cheap scalers, no deint, ALSA-direct
  # HDMI audio, windowed autofit) + nice +10 so the chain wins the CPU.
  setsid nice -n "${STVT_PLAYER_NICE:-10}" bash -c "tail -c 20000000 -F '$F' | ffmpeg -hide_banner -loglevel warning -err_detect ignore_err -f mpegts -i - -map 0:p:$p -c copy -flush_packets 1 -f mpegts - | mpv - --input-ipc-server='$SOCK' --input-conf='$ICONF' --vo=${STVT_MPV_VO:-gpu} --hwdec=no --cache=yes --cache-secs=30 --demuxer-readahead-secs=20 --cache-pause=no --cache-pause-initial=no --force-seekable=no --profile=fast --scale=bilinear --cscale=bilinear --dither=no --deinterlace=no --ao=alsa --audio-device='${STVT_AUDIO_DEV:-alsa/hdmi:CARD=vc4hdmi0,DEV=0}' --autofit-larger='${STVT_FIT:-85%x85%}' --geometry=50%:50% --osd-align-x=center --osd-align-y=bottom --osd-font-size=42 --osd-border-size=2 --title='STVT Surf'" </dev/null >/tmp/stvt_surf_mpv.log 2>&1 &
  MPV_PG=$!
  for i in $(seq 1 30); do [ -S "$SOCK" ] && break; sleep 0.3; done
  setsid python3 "$HERE/stvt_cc_osd.py" --feed "$CCFEED" --channel 1 --sock "$SOCK" --delay "$CCDELAY" </dev/null >/dev/null 2>&1 &
  BR_PG=$!
}

banner() {  # transient channel banner on the mpv OSD
  python3 - "$SOCK" "$1" <<'PY' 2>/dev/null || true
import socket, json, sys
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(2)
    s.connect(sys.argv[1])
    s.sendall((json.dumps({"command": ["show-text", sys.argv[2], 2500]}) + "\n").encode())
    s.close()
except OSError:
    pass
PY
}

tune() {  # $1 = index
  IFS='|' read -r rf prog virt call <<< "${CHANS[$1]}"
  echo "$(date +%T.%2N) -> [$(( $1 + 1 ))/$N]  $virt  $call  (RF$rf prog$prog)"
  ensure_chain "$rf"
  stop_player
  start_player "$prog"
  sleep 1; banner "  $virt   $call  "
  echo "$(date +%T.%2N) tuned [$(( $1 + 1 ))/$N]"
}

cleanup() { stop_player; [ -n "$CHAIN_PG" ] && kill -- -"$CHAIN_PG" 2>/dev/null; rm -f "$FIFO"; }
trap cleanup EXIT INT TERM

IDX="${STVT_SURF_START:-0}"
tune "$IDX"
TUNED_IDX=$IDX
echo "=== SURFING: PageUp/PageDown or mouse-wheel in the mpv window to change channel. q in the window (or Ctrl-C here) to stop. ==="

# ── Real-TV control loop (2026-06-13 rewrite) ────────────────────────────
# The old loop did one blocking read per tune, so presses made DURING a
# multi-second tune queued in the FIFO and replayed afterwards as full
# tunes ("stuck on a channel, then jumped 3 really fast" — measured user
# report). Real-TV model instead:
#   * every press updates the TARGET channel instantly and shows it in an
#     on-screen banner — feedback is immediate even while a tune runs;
#   * tuning fires once the press burst goes quiet for 0.45s, and only
#     for the NET target (3 quick ups = ONE tune, 3 channels away);
#   * presses that land during the tune coalesce the same way and cause
#     at most one follow-up tune.
# The FIFO is held open read+write on fd 3: a plain `read < fifo` blocks
# in OPEN (not read) until a writer appears, which is why `read -t` alone
# can't poll a FIFO — the <> open never blocks and keeps the pipe alive
# between writers.
# Idle ticks double as the player watchdog: a glitchy stream can kill mpv
# (and with it the keybindings), which used to leave the surfer dead with
# no recovery ("found a glitchy channel, pressed up, it crashed"). Now a
# dead player relaunches on the current channel within ~2s.
exec 3<>"$FIFO"
IDLE_TICKS=0
while :; do
  if IFS= read -r -t 0.45 -u 3 cmd; then
    case "$cmd" in
      up)   IDX=$(( (IDX + 1) % N )) ;;
      down) IDX=$(( (IDX - 1 + N) % N )) ;;
      quit) echo "$(date +%T.%2N) quit"; exit 0 ;;
      *)    continue ;;
    esac
    IDLE_TICKS=0
    IFS='|' read -r _rf _prog virt call <<< "${CHANS[$IDX]}"
    banner "  > $virt  $call"
    continue          # keep coalescing while presses are still arriving
  fi
  # input quiet for 0.45s — commit the pending change, if any
  if [ "$IDX" != "$TUNED_IDX" ]; then
    tune "$IDX"
    TUNED_IDX=$IDX
    IDLE_TICKS=0
    continue
  fi
  # truly idle — watch the player (cheap pgrep every ~2s)
  IDLE_TICKS=$(( IDLE_TICKS + 1 ))
  if [ $(( IDLE_TICKS % 4 )) -eq 0 ] && ! pgrep -x mpv >/dev/null; then
    echo "$(date +%T.%2N) player died — relaunching [$(( IDX + 1 ))/$N]"
    tune "$IDX"
    TUNED_IDX=$IDX
  fi
done
