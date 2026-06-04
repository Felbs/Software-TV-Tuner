#!/bin/bash
# auto_recover.sh — watch /tmp/playback_state.json and react when the
# bot's-eye-view says something is wrong, without user prompting.
#
# Reactions (current policy):
#   PLAYING                  → noop
#   STUTTER                  → noop (mpv cache catching up, common)
#   FROZEN ≥3 consecutive    → kill mpv (let tv_tuner respawn it)
#   DEAD ≥2 consecutive      → kick the chain (kill chain, let
#                              auto_play_forever resurrect it)
#   BAD_BITS ≥5 consecutive  → write /tmp/replug_requested.flag
#                              with timestamp; chain restart won't
#                              help, document for user. ONE-SHOT — we
#                              don't keep restarting on this state.
#   NO_CHAIN                 → noop (auto_play_forever's job)
#
# Run:  nohup ~/auto_recover.sh > /tmp/auto_recover.log 2>&1 & disown
#
# IMPORTANT: this is intentionally conservative. It does NOT loop
# replug requests, it does NOT cycle equalizers, it does not run sweeps.
# Each reaction needs to be explicit and bounded so we don't burn the
# SDR with restart loops the way we did earlier today.

set -u
STATE=/tmp/playback_state.json
LOG_TAG='[auto_recover]'
REPLUG_FLAG=/tmp/replug_requested.flag
PP=/home/user/pp.sh

# Cooldown so we don't react every 6s when the monitor flickers
COOLDOWN_SEC=60
last_reaction=0

# Streak counters
streak_frozen=0
streak_dead=0
streak_bad_bits=0

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "$LOG_TAG starting"

while true; do
    sleep 8
    [ -f "$STATE" ] || continue

    state=$(awk -F'"' '/"state":/{print $4}' "$STATE")
    consec=$(awk -F'[: ,]' '/"consecutive_frozen":/{for(i=1;i<=NF;i++)if($i~/^[0-9]+$/){print $i; exit}}' "$STATE")
    ts_kbps=$(awk -F'[: ,]' '/"ts_growth_kbps":/{for(i=1;i<=NF;i++)if($i~/^-?[0-9]+$/){print $i; exit}}' "$STATE")

    now=$(date +%s)
    cooldown_left=$((last_reaction + COOLDOWN_SEC - now))
    if [ $cooldown_left -gt 0 ]; then
        continue
    fi

    case "$state" in
        PLAYING|STUTTER|NO_CHAIN)
            streak_frozen=0; streak_dead=0; streak_bad_bits=0
            ;;
        FROZEN)
            streak_frozen=$((streak_frozen + 1))
            streak_dead=0; streak_bad_bits=0
            if [ $streak_frozen -ge 3 ]; then
                MPV=$($PP mpv | head -1)
                if [ -n "$MPV" ]; then
                    log "$LOG_TAG FROZEN×${streak_frozen} → killing mpv (PID $MPV), tv_tuner will respawn"
                    kill -9 "$MPV" 2>/dev/null
                    last_reaction=$now
                    streak_frozen=0
                fi
            fi
            ;;
        DEAD)
            streak_dead=$((streak_dead + 1))
            streak_frozen=0; streak_bad_bits=0
            if [ $streak_dead -ge 2 ]; then
                log "$LOG_TAG DEAD×${streak_dead} → killing chain (auto_play_forever will resurrect)"
                for p in $($PP chain); do kill -9 $p 2>/dev/null; done
                last_reaction=$now
                streak_dead=0
            fi
            ;;
        BAD_BITS)
            streak_bad_bits=$((streak_bad_bits + 1))
            streak_frozen=0; streak_dead=0
            if [ $streak_bad_bits -ge 5 ]; then
                if [ ! -f "$REPLUG_FLAG" ]; then
                    log "$LOG_TAG BAD_BITS×${streak_bad_bits} (ts=${ts_kbps}KB/s) → flagging for replug"
                    echo "$(date '+%Y-%m-%d %H:%M:%S') BAD_BITS sustained — SDR needs replug" > "$REPLUG_FLAG"
                fi
                # Don't keep restarting chain; it won't help per memory.
                last_reaction=$now
            fi
            ;;
    esac
done
