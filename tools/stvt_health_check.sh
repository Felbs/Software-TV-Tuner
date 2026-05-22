#!/bin/bash
# stvt_health_check.sh — diagnose the STVT chain stack BEFORE touching code.
#
# Symptoms that point to systemic issues (run this FIRST when stuck):
#   - every chain config produces 0 frames decoded
#   - auto_acquire shows ts=0.0MB on multiple RFs
#   - chain runs but live.ts doesn't grow
#   - mpv probes fail repeatedly
#
# Each check reports PASS / WARN / FAIL with the specific remediation.
# Exit 0 = healthy, exit 1 = problems found.

set -u
PASS=0
WARN=0
FAIL=0

ok()   { echo -e "  \033[32mPASS\033[0m: $1"; PASS=$((PASS+1)); }
warn() { echo -e "  \033[33mWARN\033[0m: $1"; WARN=$((WARN+1)); }
err()  { echo -e "  \033[31mFAIL\033[0m: $1"; FAIL=$((FAIL+1)); }
fix()  { echo -e "    → fix: $1"; }
hdr()  { echo ""; echo "=== $1 ==="; }

# -----------------------------------------------------------------------
hdr "1. SDR daemon"
DAEMONS=$(ps -eo pid,cmd | awk '$2=="/opt/sdrplay_api/sdrplay_apiService"' | wc -l)
case "$DAEMONS" in
    0) err "sdrplay_apiService is DEAD (0 processes)"
       fix "sudo killall sdrplay_apiService ; sleep 3 ; sudo nohup /opt/sdrplay_api/sdrplay_apiService > /tmp/sdrplay_daemon.log 2>&1 & disown"
       ;;
    1) ok  "sdrplay_apiService running (1 process)"
       ;;
    *) err "MULTIPLE sdrplay_apiService daemons ($DAEMONS) — they fight"
       fix "sudo killall sdrplay_apiService ; sleep 3 ; sudo nohup /opt/sdrplay_api/sdrplay_apiService > /tmp/sdrplay_daemon.log 2>&1 & disown"
       ;;
esac

# -----------------------------------------------------------------------
hdr "2. SDR USB device"
if lsusb | grep -q 'SDRplay RSPdx'; then
    ok "SDRplay RSPdx detected on USB"
    SDR_SYS=$(for d in /sys/bus/usb/devices/*/idVendor; do
        if [ -f "$d" ] && grep -q "1df7" "$d" 2>/dev/null; then
            dirname "$d"; break
        fi
    done)
    if [ -n "$SDR_SYS" ]; then
        SPEED=$(cat "$SDR_SYS/speed" 2>/dev/null)
        STATE=$(cat "$SDR_SYS/power/runtime_status" 2>/dev/null)
        echo "    sysfs: $SDR_SYS  speed=${SPEED}M  state=$STATE"
        # The SDR ITSELF is USB 2.0 spec, will always show 480M. What matters
        # is which BUS — bus 5 is the small USB 2 controller, bus 1 is the
        # main 14-port controller paired with USB 3 Bus 2.
        BUS=$(echo "$SDR_SYS" | grep -oE '/[0-9]+-' | tr -d '/-' | head -1)
        case "$BUS" in
            1) ok "On Bus 1 (main xHCI controller — preferred)" ;;
            5) warn "On Bus 5 (smaller USB 2 controller). Type-C port is better"
               fix "physically move USB hub to a Type-C / blue USB 3 port"
               ;;
            *) warn "On Bus $BUS (uncommon)" ;;
        esac
    fi
else
    err "SDRplay RSPdx NOT detected on USB"
    fix "check USB cable to hub + check antenna F-connector seating"
fi

# -----------------------------------------------------------------------
hdr "3. Chain orchestration"
APF=$(pgrep -fc auto_play_forever)
if [ "$APF" -eq 0 ]; then
    warn "auto_play_forever NOT running"
    fix "nohup /home/user/auto_play_forever.sh > /tmp/auto_play_forever.log 2>&1 & disown"
elif [ "$APF" -eq 1 ]; then
    ok "auto_play_forever running (1 instance)"
else
    err "MULTIPLE auto_play_forever ($APF) — duplicates fight over SDR"
    fix "kill all auto_play_forever then start one fresh"
fi

TVL=$(pgrep -fc 'tools/tv_live.py --rf')
if [ "$TVL" -gt 1 ]; then
    err "MULTIPLE tv_live.py ($TVL) — sweep + chain colliding"
fi

MPV=$(pgrep -c mpv)
if [ "$MPV" -gt 1 ]; then
    warn "MULTIPLE mpv ($MPV) — orphans from prior runs"
    fix "kill orphans: keep newest, kill older: pgrep mpv | sort -n | head -n -1 | xargs -r kill -9"
fi

# -----------------------------------------------------------------------
hdr "4. live.ts is being written"
TS=/home/user/Software-TV-Tuner/tools/data/tv_live/live.ts
if [ ! -f "$TS" ]; then
    warn "live.ts does not exist"
elif [ "$(stat -c%s $TS)" -lt 10000000 ]; then
    warn "live.ts is small ($(stat -c%s $TS) bytes)"
else
    S1=$(stat -c%s "$TS")
    sleep 2
    S2=$(stat -c%s "$TS")
    GROWTH=$((S2 - S1))
    KBPS=$((GROWTH / 2 / 1024))
    if [ "$GROWTH" -eq 0 ]; then
        err "live.ts not growing — chain producing NO data"
        fix "this means SDR isn't feeding samples; check daemon (step 1)"
    elif [ "$KBPS" -lt 800 ]; then
        warn "live.ts growth low: ${KBPS} KB/s (expected ~1500)"
    else
        ok "live.ts growing at ${KBPS} KB/s"
    fi
fi

# -----------------------------------------------------------------------
hdr "5. Linux tuning"
GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)
if [ "$GOV" = "performance" ]; then
    ok "CPU governor = performance"
else
    warn "CPU governor = $GOV (should be performance)"
    fix "sudo ~/fix_linux_tuning.sh"
fi

# USB autosuspend on SDR
if [ -n "${SDR_SYS:-}" ]; then
    AS=$(cat "$SDR_SYS/power/autosuspend" 2>/dev/null)
    if [ "$AS" = "0" ] || [ "$AS" = "-1" ]; then
        ok "SDR USB autosuspend disabled ($AS)"
    else
        warn "SDR USB autosuspend = $AS (may cause hiccups)"
        fix "sudo ~/fix_linux_tuning.sh"
    fi
fi

# -----------------------------------------------------------------------
hdr "6. mpv config sanity (NVIDIA host)"
if [ -f /etc/mpv/mpv.conf ]; then
    SYSCONF=$(cat /etc/mpv/mpv.conf | grep -v '^#' | head -10)
    if echo "$SYSCONF" | grep -q "hwdec=vaapi" && nvidia-smi >/dev/null 2>&1; then
        # vaapi on NVIDIA — was the May 22 mistake
        if [ -f /home/user/.config/mpv/mpv.conf ]; then
            ok "user mpv.conf overrides system vaapi setting"
        else
            warn "/etc/mpv/mpv.conf has hwdec=vaapi but this is NVIDIA — may break decode"
            fix "leave /etc alone; DO NOT add ~/.config/mpv/mpv.conf hwdec=no — that broke playback 2026-05-22"
        fi
    fi
fi

# -----------------------------------------------------------------------
hdr "7. Decode sanity (does TS actually decode?)"
if [ -f "$TS" ] && [ "$(stat -c%s $TS)" -gt 30000000 ]; then
    tail -c 30000000 "$TS" > /tmp/health_sample.ts
    # Try to decode 3s of program 3 video
    N=$(timeout 6 ffmpeg -hide_banner -loglevel error \
        -fflags +genpts+igndts+discardcorrupt -err_detect ignore_err \
        -f mpegts -i /tmp/health_sample.ts -map 0:p:3:v -t 3 -f null - 2>&1 \
        | grep -c "frame=")
    if [ "$N" -gt 0 ]; then
        ok "ffmpeg decoded $N frame batches from program 3"
    else
        # Try other programs before giving up
        for p in 4 5; do
            M=$(timeout 6 ffmpeg -hide_banner -loglevel error \
                -fflags +genpts+igndts+discardcorrupt -err_detect ignore_err \
                -f mpegts -i /tmp/health_sample.ts -map 0:p:$p:v -t 3 -f null - 2>&1 \
                | grep -c "frame=")
            if [ "$M" -gt 0 ]; then
                ok "ffmpeg decoded $M frame batches from program $p (program 3 dead)"
                break
            fi
        done
        if [ -z "${M:-}" ] || [ "$M" -eq 0 ]; then
            err "ffmpeg decoded 0 frames from ALL programs — signal below decode floor"
            fix "wait for RF conditions to improve OR move antenna OR check antenna F-connector"
        fi
    fi
fi

# -----------------------------------------------------------------------
hdr "Summary"
echo "PASS=$PASS  WARN=$WARN  FAIL=$FAIL"
if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "Run the FIX commands in order, top-to-bottom. The most common"
    echo "single cause of a stuck STVT session is a dead/duplicate SDR daemon"
    echo "(step 1 above). 2026-05-22 lesson: don't chain-debug until step 1 PASSES."
    exit 1
fi
exit 0
