#!/usr/bin/env python3
"""stvt_audio_autotune.py — autonomous audio-smoothness tuner.

The "agent that listens": for each candidate player config it launches the
player HEADLESS (no window pops), records what actually reaches the speakers
via stvt_audio_meter.sh, and counts dropouts (the stutter/skip). It ranks
the configs and prints the one that plays cleanly — no human listening
required.

Why this exists: VLC stutters on a live `tail -F` pipe because tail's
default 1s poll feeds data in 1s bursts. This sweeps tail poll interval,
player cache, codec, and player (VLC vs mpv) to find a config with 0
dropouts.

Prereqs: the decode chain (tv_live.py) running so live.ts grows, and the
WSLg PulseAudio sink monitor (RDPSink.monitor).

Usage: python3 tools/stvt_audio_autotune.py [--program 3] [--measure 12]
       [--warmup 7]
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIVE = HERE / "data" / "tv_live" / "live.ts"
METER = HERE / "stvt_audio_meter.sh"
STATE = Path("/tmp/stvt_audio_state.json")
PULSE = "unix:/mnt/wslg/PulseServer"
BYTES = 25_000_000  # ~10s of mux behind the live edge

# Each config builds a headless player pipeline reading the live edge.
# Varies: tail poll interval (the suspected culprit), player cache, audio
# codec, and player. label -> dict.
CONFIGS = [
    ("vlc_tail1.0_c8000", dict(player="vlc", tail_s="1.0", cache=8000, acodec="mp2")),
    ("vlc_tail0.2_c1500", dict(player="vlc", tail_s="0.2", cache=1500, acodec="mp2")),
    ("vlc_tail0.1_c3000", dict(player="vlc", tail_s="0.1", cache=3000, acodec="mp2")),
    ("vlc_tail0.1_aac",   dict(player="vlc", tail_s="0.1", cache=3000, acodec="aac")),
    ("vlc_tail0.05_c2000",dict(player="vlc", tail_s="0.05", cache=2000, acodec="mp2")),
    ("mpv_tail0.1",       dict(player="mpv", tail_s="0.1", cache=0, acodec="mp2")),
]


def log(m: str) -> None:
    print(f"[audio-tune {time.strftime('%H:%M:%S')}] {m}", flush=True)


def killall_players() -> None:
    for name in ("vlc", "mpv"):
        subprocess.run(["pkill", "-x", name], capture_output=True)
    # the transcoder ffmpeg + tail of a previous pipeline (NOT tv_live)
    subprocess.run(["pkill", "-x", "ffmpeg"], capture_output=True)
    subprocess.run(["pkill", "-f", r"[t]ail .* -F"], capture_output=True)
    time.sleep(2)


def pipeline_cmd(prog: int, c: dict) -> str:
    ff = (f"ffmpeg -hide_banner -loglevel error -i pipe:0 "
          f"-map 0:p:{prog}:v -map 0:p:{prog}:a:0 -c:v copy "
          f"-c:a {c['acodec']} -ac 2 -b:a 256k -ar 48000 -f mpegts pipe:1")
    tail = f"tail -s {c['tail_s']} -c {BYTES} -F '{LIVE}'"
    if c["player"] == "vlc":
        play = (f"vlc fd://0 --intf dummy --vout=dummy --no-fullscreen "
                f"--network-caching={c['cache']} --no-sub-autodetect-file "
                f"--play-and-exit")
    else:  # mpv, headless audio
        play = ("mpv - --vo=null --cache=yes --cache-secs=20 "
                "--demuxer-readahead-secs=20 --cache-pause=no "
                "--msg-level=all=no")
    return f"{tail} | {ff} | {play}"


def measure(seconds: int) -> dict:
    env = dict(os.environ, PULSE_SERVER=PULSE, STVT_AUDIO_STATE=str(STATE))
    subprocess.run(["bash", str(METER), str(seconds)],
                   env=env, capture_output=True, text=True,
                   timeout=seconds + 30)
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"dropouts": 99, "silence_pct": 100, "score": 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--program", type=int, default=3)
    ap.add_argument("--measure", type=int, default=12)
    ap.add_argument("--warmup", type=int, default=7)
    args = ap.parse_args()

    if not LIVE.exists():
        log(f"live.ts missing at {LIVE} — start the chain first"); return 1

    env = dict(os.environ, PULSE_SERVER=PULSE, DISPLAY=os.environ.get("DISPLAY", ":0"),
               XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
               LIBGL_ALWAYS_SOFTWARE="1")
    env.pop("WAYLAND_DISPLAY", None)

    log(f"sweeping {len(CONFIGS)} configs, prog {args.program}, "
        f"{args.measure}s measure each")
    results = []
    try:
        for label, c in CONFIGS:
            log(f"=== {label}: tail -s {c['tail_s']} cache {c['cache']} "
                f"{c['acodec']} [{c['player']}]")
            killall_players()
            proc = subprocess.Popen(["bash", "-c", pipeline_cmd(args.program, c)],
                                    env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL,
                                    start_new_session=True)
            time.sleep(args.warmup)
            m = measure(args.measure)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            log(f"  -> dropouts={m.get('dropouts')} "
                f"silence={m.get('silence_pct')}% score={m.get('score')}")
            results.append((label, c, m))
    finally:
        killall_players()

    results.sort(key=lambda r: (r[2].get("dropouts", 99), -r[2].get("score", 0)))
    print("\n" + "=" * 60)
    print("  AUDIO SMOOTHNESS LEADERBOARD (fewer dropouts = better)")
    print("=" * 60)
    print(f"  {'config':<20}{'drops':<7}{'silence%':<10}score")
    for label, _c, m in results:
        print(f"  {label:<20}{m.get('dropouts',99):<7}"
              f"{m.get('silence_pct',100):<10}{m.get('score',0)}")
    print("=" * 60)
    best = results[0]
    print(f"\n  WINNER: {best[0]}  (dropouts={best[2].get('dropouts')}, "
          f"score={best[2].get('score')})")
    print(f"  settings: tail -s {best[1]['tail_s']}, "
          f"cache={best[1]['cache']}, codec={best[1]['acodec']}, "
          f"player={best[1]['player']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        killall_players()
        sys.exit(130)
