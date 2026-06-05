"""Toggle / set the audio language of the currently-playing STVT chain.

VLC's audio-track switching is unreliable on stdin mpegts streams (the
B key falls back to whatever VLC thinks is the "default" language).
Workaround: have ffmpeg deliver ONLY the language we want, then we get
a single-track stream VLC can't second-guess.

This tool does the switch by:
  1. Finding the currently-running tv_tuner streaming subprocess
  2. Killing it cleanly (taskkill /T)
  3. Re-spawning with the new STVT_AUDIO_LANGUAGE value

Usage:
    python tools/stvt_audio_lang.py spa     # switch to Spanish
    python tools/stvt_audio_lang.py eng     # switch to English
    python tools/stvt_audio_lang.py all     # all audio tracks (VLC picks)
    python tools/stvt_audio_lang.py toggle  # flip between eng and spa
    python tools/stvt_audio_lang.py status  # show current language
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
TV_TUNER_PY = TOOLS / "tv_tuner.py"
STATE_FILE = REPO / "tools" / "data" / "tv_live" / "audio_lang_state.json"


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def find_tv_tuner_procs() -> list[tuple[int, str]]:
    """Find python.exe processes running tv_tuner.py. Returns [(pid, cmdline)]."""
    if sys.platform != "win32":
        out = subprocess.run(
            ["pgrep", "-af", "tv_tuner.py"],
            capture_output=True, text=True,
        ).stdout
        results = []
        for line in out.splitlines():
            if "tv_tuner.py" not in line:
                continue
            parts = line.split(maxsplit=1)
            try:
                results.append((int(parts[0]), parts[1] if len(parts) > 1 else ""))
            except (ValueError, IndexError):
                pass
        return results
    out = subprocess.run(
        ["wmic", "process", "where", "name='python.exe'",
         "get", "ProcessId,CommandLine", "/format:csv"],
        capture_output=True, text=True,
    ).stdout
    results = []
    for line in out.splitlines():
        if "tv_tuner.py" not in line:
            continue
        try:
            parts = line.strip().rsplit(",", 1)
            pid = int(parts[-1])
            cmdline = parts[0] if len(parts) > 1 else ""
            results.append((pid, cmdline))
        except (ValueError, IndexError):
            pass
    return results


def find_tv_tuner_pids() -> list[int]:
    return [pid for pid, _ in find_tv_tuner_procs()]


def detect_current_rf_program() -> tuple[int | None, int | None]:
    """Inspect the running tv_tuner.py command line for --rf and --program-id.
    Used so the toggle can respawn the exact channel the user is watching
    without reading state files. Returns (rf, program) — None on either if
    not detectable."""
    rf = program = None
    for _, cmdline in find_tv_tuner_procs():
        toks = cmdline.split()
        for i, t in enumerate(toks):
            if t in ("--rf",) and i + 1 < len(toks):
                try:
                    rf = int(toks[i + 1])
                except ValueError:
                    pass
            if t in ("--program-id", "--program") and i + 1 < len(toks):
                try:
                    program = int(toks[i + 1])
                except ValueError:
                    pass
    return rf, program


def kill_running():
    """Stop the current chain + player so we can respawn."""
    pids = find_tv_tuner_pids()
    if not pids:
        print("[audio-lang] no tv_tuner.py running — nothing to kill",
              file=sys.stderr)
        return
    for pid in pids:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
            )
        else:
            os.kill(pid, 15)
    # Backstop: kill any leftover ffmpeg/vlc
    if sys.platform == "win32":
        for img in ("ffmpeg.exe", "vlc.exe"):
            subprocess.run(["taskkill", "/F", "/IM", img], capture_output=True)
    else:
        subprocess.run(["pkill", "-f", "ffmpeg"], capture_output=True)
        subprocess.run(["pkill", "-x", "vlc"], capture_output=True)
    time.sleep(2)


# Winning chain config the tuner discovered. Set these whenever we
# spawn a fresh chain so the user doesn't have to remember them.
CHAIN_DEFAULTS = {
    "STVT_EQ":           "long",
    "STVT_RS":           "stock",
    "STVT_VITERBI":      "soft",
    "STVT_SPS":          "1.1",
    "STVT_RRC_SYMS":     "8",
    "STVT_TEISCRUB":     "1",
    "STVT_EQ_LKG":       "1",
    "STVT_EQ_LKG_RMS":   "1.0",
    "STVT_IFGR":         "45",
    "STVT_RFGAIN_SEL":   "3",
    "STVT_ANTENNA":      "Antenna A",
    "STVT_SUB_MARGIN":   "10",
}


def respawn(rf: int, program: int, language: str):
    """Spawn a fresh tv_tuner.py with the requested audio language.
    Always set the winning chain env vars so the user can launch
    from any shell (even a desktop shortcut) and get clean decode."""
    env = os.environ.copy()
    # Don't override if user already exported a value, but seed any missing.
    for k, v in CHAIN_DEFAULTS.items():
        env.setdefault(k, v)
    env["STVT_AUDIO_LANGUAGE"] = language
    cmd = [sys.executable, "-u", str(TV_TUNER_PY),
           "--rf", str(rf), "--player", "vlc"]
    if program and program != 1:
        cmd += ["--program-id", str(program)]
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        # The NO_WINDOW flag is what stops the console pop-ups when called
        # from the hotkey listener or a desktop shortcut.
        DETACHED = 0x00000008 | 0x00000200 | 0x08000000
        proc = subprocess.Popen(
            cmd, env=env, creationflags=DETACHED,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        proc = subprocess.Popen(cmd, env=env, start_new_session=True)
    print(f"[audio-lang] respawned tv_tuner.py (PID {proc.pid}) "
          f"on RF {rf} program {program} with audio={language}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("action", choices=["eng", "spa", "all", "toggle", "status"],
                    help="audio language to set, or special action")
    ap.add_argument("--rf", type=int, default=None,
                    help="RF channel (default: last known)")
    ap.add_argument("--program", type=int, default=None,
                    help="Program ID (default: last known, else 1)")
    args = ap.parse_args()

    state = load_state()

    if args.action == "status":
        print(f"current language: {state.get('language', '(unknown)')}")
        print(f"current rf:       {state.get('rf', '(unknown)')}")
        print(f"current program:  {state.get('program', '(unknown)')}")
        return 0

    if args.action == "toggle":
        cur = state.get("language", "eng")
        new = "spa" if cur == "eng" else "eng"
    else:
        new = args.action

    # Priority: --rf arg -> currently running tv_tuner -> saved state -> default
    detected_rf, detected_program = detect_current_rf_program()
    rf = args.rf or detected_rf or state.get("rf") or 34
    program = args.program or detected_program or state.get("program") or 1
    print(f"[audio-lang] using rf={rf} program={program} (detected from "
          f"running picker: rf={detected_rf} program={detected_program})")

    kill_running()
    respawn(rf, program, new)

    state.update({"language": new, "rf": rf, "program": program})
    save_state(state)
    print(f"[audio-lang] saved state: rf={rf} program={program} lang={new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
