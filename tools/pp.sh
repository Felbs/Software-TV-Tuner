#!/bin/bash
# pp.sh — list STVT-related processes WITHOUT pgrep false positives.
#
# `pgrep -f mpv_watchdog` matches any process containing that string in
# its command line — INCLUDING bash subshells that are themselves
# searching for "mpv_watchdog". That's been wasting time. This script
# matches by ACTUAL exec name + script path from /proc/PID/cmdline,
# excluding shell subshells running `pgrep` / `grep` / `sh -c`.
#
# Usage:
#   ~/pp.sh watchdog          # mpv_watchdog.sh script PIDs
#   ~/pp.sh chain             # tv_live.py / tv_tuner.py / run_stvt_winner
#   ~/pp.sh mpv               # mpv playing "Software TV Tuner ..."
#   ~/pp.sh ffmpeg            # the chain's ffmpeg (passthrough)
#   ~/pp.sh auto              # auto_play_forever / auto_acquire
#   ~/pp.sh sweep             # iq_sweep / iq_hunter / tv_live_filesrc
#   ~/pp.sh all               # everything in the chain ecosystem
#
# Output: one PID per line, or empty if none. Empty exit = no process.
#
# Use directly:
# kill-ok: prose/usage text, not an executed kill
#   for p in $(~/pp.sh chain); do kill -9 $p; done
#
# Or just check:
#   if [ -n "$(~/pp.sh watchdog)" ]; then echo running; fi

set -u
SELF_PID=$$
PPID_SELF=$PPID
MY_SHELL_PID=$(ps -o ppid= -p $SELF_PID | tr -d ' ')

emit() {
    # $1 = regex matching the script/binary
    # Iterate /proc, check exact cmdline match, skip my own ancestors.
    for pidfile in /proc/[0-9]*/cmdline; do
        pid=${pidfile#/proc/}
        pid=${pid%/cmdline}
        # Skip self + immediate ancestors (avoid matching my own bash -c).
        [ "$pid" = "$SELF_PID" ] && continue
        [ "$pid" = "$PPID_SELF" ] && continue
        [ "$pid" = "$MY_SHELL_PID" ] && continue
        cmd=$(tr '\0' ' ' < "$pidfile" 2>/dev/null)
        # Skip shell subshells running `pgrep`, `grep`, `bash -c`, etc.
        case "$cmd" in
            *"bash -c "*) continue ;;
            *"grep "*"$1"*) continue ;;
            *"pgrep "*"$1"*) continue ;;
            *"sed "*) continue ;;
            *"awk "*) continue ;;
        esac
        echo "$cmd" | grep -qE "$1" && echo "$pid"
    done 2>/dev/null
}

case "${1:-help}" in
    watchdog)
        emit '^(/bin/)?bash.*mpv_watchdog\.sh'
        ;;
    chain)
        # tv_live.py + tv_tuner.py + run_stvt_winner.sh (the playback chain)
        emit '(python3|/bin/python3) .*tools/(tv_live\.py|tv_tuner\.py)'
        emit '/bin/bash .*/run_stvt_winner\.sh'
        ;;
    mpv)
        emit '^mpv .*Software TV Tuner'
        ;;
    ffmpeg)
        emit 'ffmpeg .*-f mpegts -i pipe:0'
        ;;
    auto)
        emit '/bin/bash .*/auto_play_forever\.sh'
        emit 'python3 .*tools/auto_acquire\.py'
        ;;
    sweep)
        emit 'python3 .*tools/iq_sweep\.py'
        emit '/bin/bash .*/iq_hunter\.sh'
        emit 'python3 .*tools/tv_live_filesrc\.py'
        emit 'python3 .*tools/record_iq\.py'
        ;;
    all)
        "$0" watchdog
        "$0" chain
        "$0" mpv
        "$0" ffmpeg
        "$0" auto
        "$0" sweep
        ;;
    *)
        cat <<EOF
usage: $0 {watchdog|chain|mpv|ffmpeg|auto|sweep|all}

Emits PIDs (one per line) matching the category, excluding shell
subshells running pgrep/grep/awk that would false-positive with -f.

Examples:
  # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
  for p in \$(~/pp.sh chain); do kill -9 \$p; done
  if [ -z "\$(~/pp.sh watchdog)" ]; then ~/mpv_watchdog.sh >/dev/null 2>&1 & disown; fi
EOF
        exit 1
        ;;
esac
