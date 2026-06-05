#!/usr/bin/env python3
"""stvt_cc_osd.py — bridge live CEA-608 captions into mpv's on-screen text.

Reads caption lines from atsc_cc.py (decoding a single-program feed) and
pushes each into a running mpv via its JSON IPC socket as an on-screen
message ("show-text"). Captions are delayed by --delay seconds to line up
with mpv's playback, which runs a few seconds behind the live edge.

This is how we get VLC-style overlaid captions on the player that actually
works on WSLg (VLC's live audio is broken here; see stvt_audio_autotune.py).

Usage:
  stvt_cc_osd.py --feed /tmp/stvt_cc_feed.ts --channel 1 \
                 --sock /tmp/mpv-cc.sock --delay 5
Normally launched by stvt_watch_cc_osd.sh.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


def mpv_send(sock_path: str, cmd: dict) -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(sock_path)
        s.sendall((json.dumps(cmd) + "\n").encode())
        s.close()
        return True
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed", required=True, help="single-program TS for the 608 decoder")
    ap.add_argument("--channel", type=int, default=1, help="1=CC1 (English), 2=CC2 (Spanish)")
    ap.add_argument("--sock", default="/tmp/mpv-cc.sock")
    ap.add_argument("--delay", type=float,
                    default=float(os.environ.get("STVT_CC_DELAY", "5.0")),
                    help="seconds to delay captions to match mpv's playback lag")
    ap.add_argument("--hold", type=float, default=5.0, help="seconds each caption stays up")
    a = ap.parse_args()
    here = Path(__file__).resolve().parent

    cc = subprocess.Popen(
        [sys.executable, "-u", str(here / "atsc_cc.py"),
         "--channel", str(a.channel), a.feed],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    # Exit cleanly when mpv's socket disappears (player closed).
    def watchdog() -> None:
        seen = False
        while True:
            if mpv_send(a.sock, {"command": ["ignore"]}):
                seen = True
            elif seen:
                try:
                    cc.terminate()
                except ProcessLookupError:
                    pass
                os._exit(0)
            time.sleep(2)
    threading.Thread(target=watchdog, daemon=True).start()

    hold_ms = int(a.hold * 1000)
    if cc.stdout is None:
        return 1
    for line in cc.stdout:
        line = line.strip()
        if not line or line.startswith("[atsc_cc]"):
            continue
        text = line.replace("\\", " ").replace("\n", " ")
        # show this caption in mpv `delay` seconds from now (matches mpv lag)
        threading.Timer(a.delay, mpv_send,
                        args=(a.sock, {"command": ["show-text", text, hold_ms]})).start()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
