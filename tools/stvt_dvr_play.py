"""DVR-mode player for the STVT live chain.

What this gives you vs. `stvt_play_hd.py`:
  * Pause / rewind / forward / seek-back-to-live, all via VLC's native
    hotkeys. We point VLC at the growing `live.ts` file directly (file
    URL, not stdin pipe), so VLC sees it as a seekable container — the
    way it would see a recorded MKV.
  * As long as the chain keeps writing, `live.ts` keeps growing, and
    you can scrub back hours.
  * The picker's program selection works the same — we filter to one
    program at the ffmpeg level so the player sees a single video +
    single audio track per language.

What this does NOT do:
  * Cross-channel time-shift. When you change channels the picker
    wipes `live.ts` and starts fresh.
  * Long-term recording. For that, use `--record` on tv_tuner.py or
    the upcoming multi-program recorder.

VLC's DVR hotkeys (work when the VLC window has focus):
    Space            pause / play
    Left  / Right    seek back / forward 10 sec
    Shift+Arrow      30 sec
    Ctrl+Arrow       1 minute
    Ctrl+Shift+Arrow 5 minutes
    Home             jump to start (earliest in buffer)
    End              jump to live edge
    F                fullscreen
    M                mute
    +/-              volume

Usage:
    python tools/stvt_dvr_play.py [program]
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIVE_TS = REPO / "tools" / "data" / "tv_live" / "live.ts"
FFMPEG  = r"C:\ffmpeg\bin\ffmpeg.exe"
VLC     = r"C:\Program Files\VideoLAN\VLC\vlc.exe"


def main() -> int:
    prog = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    lang = os.environ.get("STVT_AUDIO_LANGUAGE", "all")

    if not LIVE_TS.exists():
        print(f"live.ts not found at {LIVE_TS}", file=sys.stderr)
        print("Start the chain first: python tools/tv_tuner.py --rf <N> --player vlc",
              file=sys.stderr)
        return 1
    if not Path(VLC).exists():
        print(f"VLC not installed at {VLC}", file=sys.stderr)
        return 1
    if not Path(FFMPEG).exists():
        print(f"ffmpeg not installed at {FFMPEG}", file=sys.stderr)
        return 1

    # VLC reads live.ts directly with built-in program selection. Native
    # support for growing mpegts files — no ffmpeg copy needed. Avoids
    # the issue where a separate ffmpeg demuxer freezes when its input
    # file outpaces what it has buffered.
    #
    # --programs=N picks the specific ATSC program (e.g. 3 = NBC HD,
    # 5 = WZDC Telemundo on RF 34). The audio-language env knob still
    # works via VLC's own track selection.
    vlc_cmd = [
        VLC,
        "--no-video-title-show",
        "--meta-title", f"STVT DVR (prog {prog})",
        "--file-caching", "3000",
        "--input-fast-seek",
        "--ts-seek-percent",
        f"--programs={prog}",
        "--play-and-exit",
    ]
    # Preferred audio language hint (VLC picks the matching track from
    # the program if present). Empty / "all" lets VLC decide.
    if lang and lang not in ("all",):
        vlc_cmd += ["--audio-language", lang]
    vlc_cmd.append(str(LIVE_TS))
    try:
        vlc_proc = subprocess.Popen(vlc_cmd)
        print(f"[dvr] VLC started (PID {vlc_proc.pid}) on {LIVE_TS}")
        print("[dvr] DVR hotkeys (VLC must be focused):")
        print("        Space        pause / play")
        print("        Left / Right seek back / forward 10 sec")
        print("        Shift+Arrow  30 sec")
        print("        Ctrl+Arrow   1 min")
        print("        End          jump to live edge")
        vlc_proc.wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
