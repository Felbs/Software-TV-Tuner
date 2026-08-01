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
# x86 chain config — keep the Ryzen gain/front-end. The Pi exported a
# fused + int16-NEON + 8MB-buffer + FPLL-fold speed trade and IFGR=50; x86
# runs the chain several x real-time and keeps its own measured gain (IFGR=59)
# and quality knobs. Anything you export still wins.
export STVT_IFGR="${STVT_IFGR:-59}" STVT_RFGAIN_SEL="${STVT_RFGAIN_SEL:-5}" STVT_ANTENNA="${STVT_ANTENNA:-Antenna A}"
export STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long STVT_SPS=1.1 STVT_RRC_SYMS=4
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
# Single-instance guard (same pattern as stvt_run.sh). Three stacked surfer
# instances were found fighting over the screen on 2026-06-13 — each
# restart's TERM failed to kill the old one (a surfer blocked opening its
# FIFO ignores TERM until a writer appears), and their watchdogs kept
# killing each other's players, which the user saw as random freezes.
LOCKF="/tmp/stvt_surf.lock"
exec 8>"$LOCKF" || { echo "cannot open lock $LOCKF" >&2; exit 3; }
# -w 15: see stvt_run.sh — tolerate a dying instance's slow lock release.
if ! flock -w 15 8; then
  echo "stvt_surf.sh is already running (lock $LOCKF held). Refusing a 2nd instance." >&2
  # kill-ok: prose/usage text, not an executed kill
  echo "Stop it first:  kill -9 \$(pgrep -f '^bash [^ ]*stvt_surf'); pkill -x mpv" >&2
  exit 3
fi

rm -f "$FIFO"; mkfifo "$FIFO"

CHAIN_PG=""; FEED_PG=""; MPV_PG=""; BR_PG=""; CUR_RF=""

ensure_chain() {  # $1 = rf ; (re)tune only if the RF actually changed
  if [ "$CUR_RF" = "$1" ] && [ -n "$CHAIN_PG" ] && kill -0 "$CHAIN_PG" 2>/dev/null; then
    return
  fi
  [ -n "$CHAIN_PG" ] && kill -- -"$CHAIN_PG" 2>/dev/null
  sleep 3; rm -f "$F"
  # exec 8>&- closes the inherited single-instance lock fd so the chain doesn't
  # hold the flock after THIS surfer exits (else a restart can't reacquire the
  # lock until the orphaned chain is found and killed — repeatedly hit
  # 2026-06-13).
  setsid bash -c "exec 8>&- 2>/dev/null; exec python3 '$HERE/tv_live.py' --rf $1 --rotate-gb ${STVT_ROTATE_GB:-16} > '$HERE/data/tv_live/tv_tuner.tv_live.log' 2>&1" </dev/null >/dev/null 2>&1 &
  CHAIN_PG=$!; CUR_RF="$1"
  # 10MB (~4s of TS) is enough cushion for the player's tail/probe — the
  # old 40MB threshold added ~12s of dead air to every cross-mux change.
  for i in $(seq 1 25); do [ "$(stat -c%s "$F" 2>/dev/null || echo 0)" -gt 10000000 ] && break; sleep 1; done
}

stop_player() {
  for pg in "$MPV_PG" "$FEED_PG" "$BR_PG"; do [ -n "$pg" ] && kill -- -"$pg" 2>/dev/null; done
  MPV_PG=""; FEED_PG=""; BR_PG=""
  # Belt-and-suspenders: a previous session's player can survive its cleanup
  # (observed 2026-06-13: an orphaned frozen mpv sat on screen over the live
  # one, both fighting for the IPC socket). The surfer owns the screen —
  # sweep ANY leftover player-pipeline parts, not just our own PGIDs.
  kill $(pgrep -x mpv) 2>/dev/null
  kill $(pgrep -x ffmpeg) 2>/dev/null
  kill $(pgrep -f '^tail .*live\.ts') 2>/dev/null
  sleep 0.5
  # TERM stays PENDING on a stopped/hard-hung process — KILL the survivors
  # (a SIGSTOPped mpv survived the sweep in testing).
  # kill-ok: player/pipeline consumer (mpv/ffmpeg/tail), not the SDR holder
  kill -9 $(pgrep -x mpv) $(pgrep -x ffmpeg) 2>/dev/null
  sleep 0.2
}

# Unique PIDs at the live edge. A healthy mux shows ~25-40; ~1-10 means the
# chain locked the carrier but is decoding NOISE (a "drought") and no amount
# of player relaunching will help — the CHAIN needs a fresh cold start.
edge_pids() {
  tail -c 1000000 "$F" 2>/dev/null | python3 -c '
import sys
d=sys.stdin.buffer.read(); s=set(); i=d.find(b"\x47")
while i>=0 and i+188<=len(d):
    if d[i]==0x47: s.add(((d[i+1]&0x1f)<<8)|d[i+2]); i+=188
    else: i+=1
print(len(s))' 2>/dev/null || echo 0
}

# Current playback clock via mpv's IPC socket; empty on any failure.
mpv_timepos() {
  python3 - "$SOCK" <<'PY' 2>/dev/null
import socket, json, sys
try:
    s = socket.socket(socket.AF_UNIX); s.settimeout(1); s.connect(sys.argv[1])
    s.sendall(b'{"command":["get_property","time-pos"]}\n')
    d = json.loads(s.recv(4096).decode().splitlines()[0]).get("data")
    print("" if d is None else f"{d:.2f}")
except Exception:
    pass
PY
}


# Deinterlace mode (STVT_DEINT) — NBC & friends broadcast 1080i; without
# deinterlacing, moving edges show combing ("wavy lines on outlines",
# user-reported). x86 default is `field` (full 60fps deint): this box has the
# headroom the Pi never did, so it gets the best picture instead of the Pi's
# half-res speed trades.
#   field : full 60fps deint (--deinterlace=yes). Smoothest motion. x86 DEFAULT.
#   frame : yadif half-rate -> 30fps progressive. Combing gone at ~half cost.
#   low   : decode 1080i at half resolution (Pi speed trade; combing blurs out).
#   lowdeint : low + half-res bwdif (the Pi 5 default — too soft for x86).
#   no    : combing visible on 1080i motion.
# deint=interlaced applies the filter ONLY to frames flagged interlaced, so
# progressive 720p channels (Fox) pass through untouched.
case "${STVT_DEINT:-field}" in
  # low: decode 1080i at HALF resolution (960x540). ~1/4 the decode cost and
  # interlace combing collapses into sub-pixel blur — no filter needed at
  # all. The soft trade is minor on a sub-1080 panel. The only mode measured
  # to keep up on the Pi 5 alongside the live chain.
  low)       DEINT_FLAG="--vd-lavc-o=lowres=1";;
  # low+yadif: deinterlace AT the halved resolution (~1/4 the filter cost
  # that failed at 1080) — removes residual half-res combing on motion.
  lowdeint)  DEINT_FLAG="--vd-lavc-o=lowres=1 --vf=lavfi=[bwdif=mode=send_frame:deint=interlaced]";;
  field|yes) DEINT_FLAG="--deinterlace=yes";;
  # lavfi wrapper, NOT mpv's own yadif: mpv runs its filter on the single
  # video thread (measured: 2944 drops + video 61s behind audio), while the
  # lavfi graph slice-threads yadif across all cores.
  frame)     DEINT_FLAG="--vf=lavfi=[yadif=mode=send_frame:deint=interlaced]";;
  *)         DEINT_FLAG="--deinterlace=no";;
esac

start_player() {  # $1 = program
  local p="$1"
  # -y is LOAD-BEARING: without it, ffmpeg's "overwrite?" prompt reads its
  # answer from stdin — which here is the TS pipe — and dies, so the caption
  # feed silently never regenerated after the first tune (captions dead from
  # the second channel on; found 2026-06-13 when the feed file was stale).
  rm -f "$CCFEED"
  setsid bash -c "exec 8>&- 2>/dev/null; tail -s 0.1 -c 20000000 -F '$F' | ffmpeg -hide_banner -loglevel error -y -i pipe:0 -map 0:p:$p -c copy -f mpegts '$CCFEED'" </dev/null >/dev/null 2>&1 &
  FEED_PG=$!
  rm -f "$SOCK"
  # Resolution-aware deint + fit (ported from stvt_play_hd.sh). The lowres
  # deint path is a 1080i tune; on an SD subchannel (e.g. 704x480 4:3) lowres
  # halves it to a tiny square window. Probe this program's height: SD (<720)
  # gets full-res decode (bwdif is cheap at SD) + enlarge-to-fill; HD keeps the
  # lowres path + size cap.
  local deint="$DEINT_FLAG" fit="--autofit-larger='${STVT_FIT:-85%x85%}'" vh
  vh=$(timeout 8 ffprobe -v error -show_entries program=program_id:stream=height \
        -of compact -i "$F" 2>/dev/null | grep -F "program_id=$p|" \
        | grep -oE 'height=[0-9]+' | head -1 | cut -d= -f2)
  if [ -n "$vh" ] && [ "$vh" -lt 720 ]; then
    deint="--vf=lavfi=[bwdif=mode=send_frame:deint=interlaced]"
    fit="--autofit-larger='${STVT_FIT:-90%x90%}' --autofit-smaller='${STVT_FIT:-90%x90%}'"
  fi
  # x86 player: GPU VO + the session's own audio (pulse/pipewire — no Pi
  # ALSA-direct HDMI), full-quality scalers (no Pi --profile=fast), no nice
  # (this box has CPU to spare), and the SD-aware deint/fit ported above.
  # Override the VO with STVT_MPV_VO if you need it (e.g. wlshm under WSLg).
  setsid bash -c "exec 8>&- 2>/dev/null; tail -c 20000000 -F '$F' | ffmpeg -hide_banner -loglevel warning -err_detect ignore_err -f mpegts -i - -map 0:p:$p -c copy -flush_packets 1 -f mpegts - | mpv - --input-ipc-server='$SOCK' --input-conf='$ICONF' --vo=${STVT_MPV_VO:-gpu} --hwdec=no --cache=yes --cache-secs=30 --demuxer-readahead-secs=20 --cache-pause=no --cache-pause-initial=no --force-seekable=no $deint $fit --osd-align-x=center --osd-align-y=bottom --osd-font-size=42 --osd-border-size=2 --title='STVT Surf'" </dev/null >/tmp/stvt_surf_mpv.log 2>&1 &
  MPV_PG=$!
  for i in $(seq 1 30); do [ -S "$SOCK" ] && break; sleep 0.3; done
  # 8>&- closes the inherited single-instance lock fd: stvt_cc_osd is long-lived
  # (watches the mpv socket) and otherwise holds the flock after the surfer
  # exits, blocking the next launch (the recurring 2026-06-13 lock holder).
  setsid python3 "$HERE/stvt_cc_osd.py" --feed "$CCFEED" --channel 1 --sock "$SOCK" --delay "$CCDELAY" 8>&- </dev/null >/dev/null 2>&1 &
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
  # Rich banner: network + now-playing (EIT) + signal/decode-health, pushed to
  # the mpv OSD once the socket is up. Falls back to the plain banner if the
  # helper or guide data isn't available. Backgrounded so a slow EIT lookup
  # doesn't delay the next channel change.
  ( sleep 1
    python3 "$HERE/stvt_surf_info.py" --rf "$rf" --program "$prog" \
      --virtual "$virt" --callsign "$call" --sock "$SOCK" 2>/dev/null \
      || banner "  $virt   $call  "
  ) &
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
DEAD_RETRIES=0
AUTO_SKIPS=0
LAST_DIR=1
while :; do
  if IFS= read -r -t 0.45 -u 3 cmd; then
    case "$cmd" in
      up)   IDX=$(( (IDX + 1) % N )); LAST_DIR=1 ;;
      down) IDX=$(( (IDX - 1 + N) % N )); LAST_DIR=-1 ;;
      quit) echo "$(date +%T.%2N) quit"; exit 0 ;;
      *)    continue ;;
    esac
    IDLE_TICKS=0
    AUTO_SKIPS=0   # user took the wheel — reset the dead-channel skip budget
    IFS='|' read -r _rf _prog virt call <<< "${CHANS[$IDX]}"
    banner "  > $virt  $call"
    continue          # keep coalescing while presses are still arriving
  fi
  # input quiet for 0.45s — commit the pending change, if any
  if [ "$IDX" != "$TUNED_IDX" ]; then
    tune "$IDX"
    TUNED_IDX=$IDX
    IDLE_TICKS=0
    DEAD_RETRIES=0   # fresh channel — give it a clean retry budget
    continue
  fi
  # truly idle — watch the player every ~2s: dead OR frozen both relaunch.
  # Frozen = mpv alive but its playback clock stuck (observed 2026-06-13:
  # a stalled player sat on the last frame indefinitely; death-only
  # supervision never fired). 5 consecutive identical clocks ≈ 9s frozen.
  IDLE_TICKS=$(( IDLE_TICKS + 1 ))
  if [ $(( IDLE_TICKS % 4 )) -eq 0 ]; then
    if ! pgrep -x mpv >/dev/null; then
      # Cap relaunches per channel: an undecodable program (empty/no-video
      # subchannel like a "no data" station) makes mpv exit instantly, which
      # otherwise loops forever (observed on 25.1 WDVM-SD, RF15 prog7 — looked
      # like a crash). After 3 tries, stop and wait for the user to surf away;
      # DEAD_RETRIES resets when they change channel.
      DEAD_RETRIES=$(( DEAD_RETRIES + 1 ))
      if [ "$DEAD_RETRIES" -le 3 ]; then
        echo "$(date +%T.%2N) player died — relaunching [$(( IDX + 1 ))/$N] (try $DEAD_RETRIES/3)"
        tune "$IDX"; TUNED_IDX=$IDX; FROZEN=0; LAST_TP=""
      else
        # Channel won't decode (dead/empty subchannel, or droughting RF). Don't
        # sit on a black screen — auto-skip to the next channel in the surf
        # direction, exactly as if the user kept pressing. AUTO_SKIPS caps the
        # cascade so an all-dead lineup can't loop forever; a keypress resets it.
        AUTO_SKIPS=$(( AUTO_SKIPS + 1 ))
        if [ "$AUTO_SKIPS" -ge "$N" ]; then
          echo "$(date +%T.%2N) no decodable channel after skipping all $N — waiting for input"
          banner "  no decodable channel — check antenna"
          DEAD_RETRIES=0   # one quiet pass done; a keypress will retry fresh
          AUTO_SKIPS=0
        else
          IDX=$(( (IDX + ${LAST_DIR:-1} + N) % N ))
          IFS='|' read -r _rf _prog _v _c <<< "${CHANS[$IDX]}"
          echo "$(date +%T.%2N) channel unavailable — auto-skip to [$(( IDX + 1 ))/$N] $_v"
          tune "$IDX"; TUNED_IDX=$IDX; DEAD_RETRIES=0; FROZEN=0; LAST_TP=""
        fi
      fi
    else
      tp=$(mpv_timepos)
      # Empty tp with mpv ALIVE = the player exists but its IPC won't answer
      # (hard hang / SIGSTOP) — that IS a freeze, count it. Only an
      # ADVANCING clock resets the counter.
      if [ -z "$tp" ] || [ "$tp" = "${LAST_TP:-}" ]; then
        FROZEN=$(( ${FROZEN:-0} + 1 ))
        if [ "$FROZEN" -ge 5 ]; then
          # Drought-aware recovery (2026-06-13): when the freeze is really
          # the CHAIN decoding noise (live edge shows ~1 PID instead of
          # ~25-40), relaunching the player forever can't help — observed
          # as a player relaunch loop every ~13s over a black window. Kill
          # the chain so tune()'s ensure_chain gives it a fresh cold start.
          ep=$(edge_pids)
          if [ "${ep:-0}" -lt 10 ]; then
            echo "$(date +%T.%2N) player FROZEN + chain DROUGHT (${ep} PIDs) — chain restart [$(( IDX + 1 ))/$N]"
            [ -n "$CHAIN_PG" ] && kill -- -"$CHAIN_PG" 2>/dev/null
            CHAIN_PG=""; CUR_RF=""
            sleep 3
          else
            echo "$(date +%T.%2N) player FROZEN (${tp:-ipc unresponsive}, mux healthy ${ep} PIDs) — relaunching [$(( IDX + 1 ))/$N]"
          fi
          tune "$IDX"; TUNED_IDX=$IDX; FROZEN=0; LAST_TP=""
          continue
        fi
      else
        FROZEN=0
      fi
      LAST_TP="$tp"
    fi
  fi
done
