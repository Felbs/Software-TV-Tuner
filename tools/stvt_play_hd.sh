#!/usr/bin/env bash
# stvt_play_hd.sh — supervised HD player for the live STVT stream.
#
# An ATSC broadcast is a MULTIPLEX of several programs (often 1-2 HD 1080 +
# several SD subchannels). If you point mpv at the raw multi-program live.ts it
# picks a track at random and may show "Invalid frame dimensions 0x0" garbage —
# even though the decode is perfect. This script stream-copies ONE program with
# ffmpeg and feeds it to mpv with a buffer cushion, so playback is clean and
# rides through the chain's occasional sample-overflow (OsO) hiccups.
#
# It also self-heals: if the cushion drains or mpv dies, it relaunches the
# player (NEVER the decoder chain) with a hard cap on restarts so it can never
# runaway-respawn.
#
# Usage:  tools/stvt_play_hd.sh [program] [tailMB]
#   program : MPEG-TS program number to play (default 3). Run
#             `ffprobe -show_programs tools/data/tv_live/live.ts` to list them;
#             pick one whose video stream is 1920x1080.
#   tailMB  : how many MB from the live edge to start (default 25 ≈ ~20s cushion).
set -u
PROG="${1:-3}"
BACKMB="${2:-25}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
F="$HERE/data/tv_live/live.ts"
MPVLOG="/tmp/stvt_mpv.log"
SUPLOG="/tmp/stvt_play_hd.sup.log"
MAX_RESTARTS=40
COOLDOWN=3
LOWCACHE_LIMIT=4          # ~60s of sustained low cache before a refresh

# Auto-detect the graphical session (works under a normal desktop login).
export DISPLAY="${DISPLAY:-:0}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

log(){ echo "$(printf '%(%H:%M:%S)T' -1) $*" >> "$SUPLOG"; }


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
case "${STVT_DEINT:-auto}" in
  # auto (DEFAULT): resolved per-program in launch() from the video field order —
  # progressive channels (Fox 720p60) get NO deint filter at all (clean, and it
  # avoids the yadif cc_fifo caption fps-doubling); interlaced (1080i) gets full
  # 60fps send_field deint. This is the robust fix for "glitchy progressive".
  auto)      DEINT_FLAG="AUTO";;
  # low: decode 1080i at HALF resolution (960x540). ~1/4 the decode cost and
  # interlace combing collapses into sub-pixel blur — no filter needed at
  # all. The soft trade is minor on a sub-1080 panel. The only mode measured
  # to keep up on the Pi 5 alongside the live chain.
  low)       DEINT_FLAG="--vd-lavc-o=lowres=1";;
  # low+yadif: deinterlace AT the halved resolution (~1/4 the filter cost
  # that failed at 1080) — removes residual half-res combing on motion.
  lowdeint)  DEINT_FLAG="--vd-lavc-o=lowres=1 --vf=lavfi=[bwdif=mode=send_frame:deint=interlaced]";;
  # field: full 60fps deint, but INTERLACED-ONLY via lavfi yadif (send_field =
  # one frame per field = 60fps from 1080i). deint=interlaced means yadif skips
  # frames flagged progressive, so 720p60 (Fox) passes through untouched. The old
  # `--deinterlace=yes` was mpv's UNCONDITIONAL deinterlacer: it doubled
  # progressive 59.94fps -> 119.88fps (the fps=120000/1001 in the cc_fifo error),
  # which made 720p channels glitchy and garbled their captions.
  field|yes) DEINT_FLAG="--vf=lavfi=[yadif=mode=send_field:deint=interlaced]";;
  # lavfi wrapper, NOT mpv's own yadif: mpv runs its filter on the single
  # video thread (measured: 2944 drops + video 61s behind audio), while the
  # lavfi graph slice-threads yadif across all cores.
  frame)     DEINT_FLAG="--vf=lavfi=[yadif=mode=send_frame:deint=interlaced]";;
  *)         DEINT_FLAG="--deinterlace=no";;
esac

launch(){
  # last-N-bytes form (tail -c N) — NOT absolute offset (tail -c +OFF), which
  # stalls seeking into a multi-GB growing file.
  local bytes=$(( BACKMB*1000000 ))
  : > "$MPVLOG"

  # Resolution-aware deint + window fit (the SD-aware logic ported from the Pi).
  # On an SD subchannel (e.g. 704x480 4:3 TeleXitos) a 4:3 stream opens as a
  # tiny window; probe THIS program's height and, for SD (<720), force the
  # bwdif full-res deint and enlarge the small window to fill the screen at its
  # true aspect. HD opens at the panel's natural size (x86 default: no autofit
  # cap — press f for fullscreen; set STVT_FIT to window it).
  local deint="$DEINT_FLAG" fit=""
  local vh
  vh=$(timeout 8 ffprobe -v error -show_entries program=program_id:stream=height \
        -of compact -i "$F" 2>/dev/null | grep -F "program_id=$PROG|" \
        | grep -oE 'height=[0-9]+' | head -1 | cut -d= -f2)
  # AUTO deint: probe THIS program's field order and deinterlace only genuinely
  # interlaced content. progressive -> no filter (no cc_fifo fps-doubling);
  # interlaced -> 60fps send_field; unknown/empty -> safe send_frame conditional.
  if [ "$deint" = "AUTO" ]; then
    local fo
    fo=$(timeout 8 ffprobe -v error -probesize 5M -analyzeduration 5M \
          -show_entries program=program_id:stream=field_order -of compact \
          -i "$F" 2>/dev/null | grep -F "program_id=$PROG|" \
          | grep -oE 'field_order=[a-z]+' | head -1 | cut -d= -f2)
    case "$fo" in
      progressive) deint="--deinterlace=no"; log "prog $PROG PROGRESSIVE ($fo) — no deint" ;;
      tt|bb|tb|bt) deint="--vf=lavfi=[yadif=mode=send_field:deint=interlaced]"; log "prog $PROG INTERLACED ($fo) — 60fps deint" ;;
      *)           deint="--vf=lavfi=[yadif=mode=send_frame:deint=interlaced]"; log "prog $PROG field order unknown ('$fo') — safe conditional deint" ;;
    esac
  fi
  if [ -n "$vh" ] && [ "$vh" -lt 720 ]; then
    deint="--vf=lavfi=[bwdif=mode=send_frame:deint=interlaced]"
    fit="--autofit-larger='${STVT_FIT:-90%x90%}' --autofit-smaller='${STVT_FIT:-90%x90%}'"
    log "prog $PROG is SD (${vh}p) — full-res decode + enlarge-to-fill"
  fi
  # -f mpegts on the INPUT is essential: tail -c starts mid-packet, so ffmpeg's
  # format auto-probe reads a partial packet and dies ("Invalid data found"),
  # which looks like a rough-patch hang and triggers an endless relaunch storm
  # (observed: 2300+ relaunches, mpv mostly down, while the chain was perfect).
  # Forcing mpegts skips the probe and lets the demuxer resync to the 188 grid.
  #
  # Audio: many ATSC programs carry a Spanish SAP track alongside English. A bare
  # -map 0:p:N lets ffmpeg emit the audio in absolute-index order, which can put
  # Spanish first and varies between relaunches. Mapping by PROGRAM-RELATIVE
  # position (0,1,2,3...) instead forces the broadcaster's PMT order — English is
  # listed first, Spanish second — so mpv reliably shows 1/2=English, 2/2=Spanish.
  # The trailing ? makes the higher slots optional (programs with fewer tracks
  # don't error). --alang is the belt-and-suspenders default; press # to switch
  # live; STVT_ALANG=spa to start on Spanish.
  # x86 player: no nice (this box runs the chain at several x real-time, the
  # player never starves it), mpv's default high-quality VO/scalers (the Pi's
  # --profile=fast + cheap scalers were a GPU speed trade x86 doesn't need),
  # and the session's own audio (pulse/pipewire — no Pi ALSA-direct HDMI).
  # Keep the ported knobs: --video-sync (STVT_MPV_SYNC), --mute (STVT_MPV_MUTE),
  # the SD-aware $deint/$fit, and the program-relative audio map.
  # Audio-pop protection lives HERE in the player (which has CPU headroom under
  # GPU decode), NOT in the chain (TEISCRUB stalls the CPU-bound DSP pipeline ->
  # drops below real-time -> player starves). Two cheap layers:
  #   ffmpeg +discardcorrupt — drop transport packets flagged corrupt before the
  #     AC-3/MPEG decoders ever see them (the corrupt frames = the loud pops).
  #   mpv  --af=alimiter      — hard brick-wall limiter so any pop that still
  #     slips through is clamped, never full-scale. STVT_AUDIO_LIMIT=0 disables.
  local aflimit="--af=alimiter=limit=0.9:level=disabled"
  [ "${STVT_AUDIO_LIMIT:-1}" = "0" ] && aflimit=""
  # Captions OFF by default (STVT_CC=1 to show). ATSC programs carry embedded
  # EIA-608/708 closed captions; mpv can surface them as a sub track, and when
  # the framerate was being doubled they rendered as gibberish. Keep them hidden
  # and don't auto-select a sub track unless the user opts in.
  # --sid=no = select NO subtitle track at all (definitive off — mpv was still
  # auto-selecting the embedded eia_608 CC track, which rendered as gibberish;
  # --sub-visibility=no alone only HID a still-selected track). STVT_CC=1 shows it.
  # STVT_CC=1: --sub-create-cc-track=yes makes mpv's ffmpeg CC decoder extract the
  # video's embedded A/53 EIA-608 into a selectable sub track (--sid=1). Verified
  # readable at 59.94fps; the old gibberish was deint field-doubling (fixed by the
  # auto field-order patch). Note atsc_cc.py, the OSD bridge, is silent on 60p.
  local subflags="--sid=no --sub-visibility=no --no-sub-auto"
  [ "${STVT_CC:-0}" = "1" ] && subflags="--sub-create-cc-track=yes --sid=1 --sub-visibility=yes"
  # Video error CONCEALMENT — what a TV does that we didn't: when a macroblock
  # arrives corrupt, interpolate it (guess motion vectors + deblock) from the
  # previous frame / neighbours instead of rendering garbage blocks. The signal
  # fades ~6% sometimes; this hides that loss the way a TV hides it. Applies to
  # the software decoder; harmless (ignored) under GPU hwdec. STVT_MPV_EC=0 off.
  local ec="--vd-lavc-o=error_concealment=3 --vd-lavc-framedrop=none"
  [ "${STVT_MPV_EC:-1}" = "0" ] && ec=""
  # SMOOTH profile (STVT_MPV_SMOOTH=1, default on): the chain decodes ~95% of a
  # fading signal, so packets drop intermittently. Low-latency flags make the
  # player SKIP at every gap. Instead: regenerate continuous timestamps
  # (+genpts+igndts) so discarded/lost packets don't leave timestamp holes,
  # drop the low-latency flags, and let video play smoothly over audio gaps
  # (video-sync=desync) — trading a few seconds of latency for no skipping.
  # +discardcorrupt is OPT-IN (STVT_DISCARD_CORRUPT=1). Default off to match
  # main and pi-port: it drops corrupt packets before the audio decoder
  # (fewer pops) but visibly mangles video -- the 2026-07-10 "datamosh"
  # regression, re-reported by the user on 2026-07-31. The alimiter and
  # error_concealment=3 below cover the pops without the video cost.
  local _dc=""; [ "${STVT_DISCARD_CORRUPT:-0}" = "1" ] && _dc="+discardcorrupt"
  local ff_fflags="nobuffer+flush_packets${_dc}" ff_lowdelay="-flags low_delay"
  local vsync="${STVT_MPV_SYNC:-audio}"
  if [ "${STVT_MPV_SMOOTH:-1}" != "0" ]; then
    ff_fflags="+genpts+igndts${_dc}+flush_packets"; ff_lowdelay=""
    vsync="desync"
  fi
  setsid bash -c "tail -c $bytes -F '$F' | \
    ffmpeg -hide_banner -loglevel warning -fflags $ff_fflags \
      $ff_lowdelay -probesize 5M -analyzeduration 5M -err_detect ignore_err \
      -f mpegts -i - -map 0:p:$PROG:0 -map 0:p:$PROG:1? -map 0:p:$PROG:2? -map 0:p:$PROG:3? \
      -c copy -flush_packets 1 -f mpegts - | \
    mpv - --vo=${STVT_MPV_VO:-gpu} --hwdec=${STVT_MPV_HWDEC:-no} --cache=yes --cache-secs=${STVT_CACHE_SECS:-8} --demuxer-max-bytes=128MiB \
      --demuxer-readahead-secs=${STVT_CACHE_SECS:-8} --cache-pause=no --cache-pause-initial=no \
      $deint $aflimit $ec $subflags \
      --video-sync=$vsync \
      --alang=${STVT_ALANG:-eng,en} \
      $fit \
      --mute=${STVT_MPV_MUTE:-no} \
      --title='STVT Live (prog $PROG)' --force-seekable=no \
      --msg-level=all=status" >> "$MPVLOG" 2>&1 < /dev/null &
  log "launched player prog=$PROG tail=${BACKMB}MB"
}

kill_player(){
  for p in $(pgrep -x mpv); do kill "$p" 2>/dev/null; done
  for p in $(pgrep -x ffmpeg); do kill "$p" 2>/dev/null; done
  for p in $(pgrep -f "tail -c .*live.ts"); do kill "$p" 2>/dev/null; done
  sleep 1
  # kill-ok: player/pipeline consumer (mpv/ffmpeg/tail), not the SDR holder
  for p in $(pgrep -x mpv) $(pgrep -x ffmpeg); do kill -9 "$p" 2>/dev/null; done
}

# Current playback position (1st AV number) in whole seconds, "" if none yet.
av_pos(){ grep -oE '^AV: [0-9:]+' "$MPVLOG" | tail -1 | grep -oE '[0-9:]+$' \
          | awk -F: '{n=NF; print (n==3?$1*3600+$2*60+$3:$1*60+$2)}'; }

# Returns 0 if mpv started AND its position advances (real playback, not a
# rough-patch hang that stalls at "Reading from stdin..." or freezes on the
# first frame). Waits up to ~20s.
started_ok(){
  local n=0 p0 p1
  while [ "$n" -lt 12 ]; do
    p0=$(av_pos); [ -n "$p0" ] && break
    pgrep -x mpv >/dev/null || return 1
    sleep 1; n=$((n+1))
  done
  [ -n "$p0" ] || return 1
  sleep 4; p1=$(av_pos)
  [ -n "$p1" ] && [ "$p1" -gt "$p0" ] && return 0
  return 1
}

[ -f "$F" ] || { echo "live.ts not found at $F — start the chain first (tools/tv_live.py)"; exit 1; }

# Kill + relaunch + verify start, retrying rough-patch hangs up to the cap.
# Returns non-zero only when the restart cap is exhausted.
relaunch(){
  while :; do
    restarts=$((restarts+1))
    [ "$restarts" -gt "$MAX_RESTARTS" ] && { log "MAX_RESTARTS hit — giving up"; return 1; }
    log "relaunch #$restarts ($1)"
    kill_player; sleep "$COOLDOWN"; launch
    started_ok && { log "playing"; return 0; }
    log "  start hung (rough patch) — retrying"
  done
}

restarts=0
relaunch "initial" || exit 1
last=$(av_pos); stuck=0
fsz_prev=$(stat -c %s "$F" 2>/dev/null || echo 0)
while true; do
  sleep 10
  if ! pgrep -f '^python3 [^ ]*tv_live\.py' >/dev/null; then log "chain DOWN — supervisor exiting"; exit 0; fi
  if ! pgrep -x mpv >/dev/null; then relaunch "mpv died" || exit 1; last=$(av_pos); stuck=0; continue; fi
  # Proactive rotation relaunch (STVT_ROTATE_RELAUNCH=0 disables): when the
  # chain recycles live.ts the size shrinks; tail -F follows the truncation
  # but drags an MPEG-TS timestamp discontinuity through ffmpeg/mpv — it
  # usually rides through, but occasionally costs ~1min of dropped frames +
  # A-V offset and leaves mpv's clock bookkeeping skewed (measured overnight
  # 2026-06-13). A deterministic relaunch at the rotation instant costs a
  # ~5s blip and restores a clean clock + full cushion. The 100MB margin
  # keeps sampling jitter from false-tripping.
  fsz=$(stat -c %s "$F" 2>/dev/null || echo 0)
  if [ "${STVT_ROTATE_RELAUNCH:-1}" = 1 ] && [ "$fsz" -lt $((fsz_prev - 100000000)) ]; then
    log "rotation detected ($((fsz_prev/1000000))MB -> $((fsz/1000000))MB) — proactive relaunch"
    sleep 3   # let the fresh file accumulate a few MB for ffmpeg's probe
    relaunch "rotation" || exit 1
    last=$(av_pos); stuck=0; fsz_prev=$(stat -c %s "$F" 2>/dev/null || echo 0)
    continue
  fi
  fsz_prev=$fsz
  # True freeze = playback POSITION stops advancing (cache=0 while still
  # advancing at the live edge is fine — not a freeze).
  pos=$(av_pos)
  if [ -n "$pos" ] && [ -n "$last" ] && [ "$pos" -le "$last" ]; then
    stuck=$((stuck+1))
  else
    stuck=0
  fi
  last=$pos
  if [ "$stuck" -ge 3 ]; then   # ~30s with no progress
    relaunch "frozen at ${pos}s" || exit 1; last=$(av_pos); stuck=0
  fi
done
