"""Channel up / down stepper for the STVT chain.

The hotkey listener calls this with one of:
    next    -> tune the next detected virtual channel in scan.json
    prev    -> tune the previous detected virtual channel

The current channel is detected from the running tv_tuner.py command
line (--rf and --program-id). The next/prev channel is picked from
~/.tv_tuner/scan.json (the picker's scan output). 'not_detected'
rows are skipped so we never tune a known-dead carrier.

Usage:
    python tools/stvt_channel_step.py next
    python tools/stvt_channel_step.py prev
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
TV_TUNER_PY = REPO / "tools" / "tv_tuner.py"
PY = sys.executable
SCAN_PATH = Path(os.path.expanduser("~")) / ".tv_tuner" / "scan.json"

# kill-ok: prose/usage text, not an executed kill
# Hide console windows from subprocess.run calls (wmic, taskkill etc.)
# on Windows so the hotkey doesn't make black boxes flash on the screen.
SUBPROC_KW = {}
if sys.platform == "win32":
    SUBPROC_KW["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

CHAIN_DEFAULTS = {
    "STVT_EQ":          "long",
    "STVT_RS":          "stock",
    "STVT_VITERBI":     "hard",  # ported from Pi: bare/winning config on x86 too
    "STVT_SPS":         "1.1",
    "STVT_RRC_SYMS":    "8",
    "STVT_TEISCRUB":    "1",
    "STVT_EQ_LKG":      "1",
    "STVT_EQ_LKG_RMS":  "1.0",
    "STVT_IFGR":        "45",
    "STVT_RFGAIN_SEL":  "3",
    "STVT_ANTENNA":     "Antenna A",
    "STVT_SUB_MARGIN":  "10",
}


def find_tv_tuner_cmdline() -> str | None:
    """Return the command line of the running tv_tuner.py, or None."""
    if sys.platform != "win32":
        out = subprocess.run(["pgrep", "-af", "tv_tuner.py"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            if "tv_tuner.py" in line:
                return line
        return None
    out = subprocess.run(
        ["wmic", "process", "where", "name='python.exe'",
         "get", "ProcessId,CommandLine", "/format:csv"],
        capture_output=True, text=True, **SUBPROC_KW,
    ).stdout
    for line in out.splitlines():
        if "tv_tuner.py" in line:
            return line
    return None


def detect_current() -> tuple[int | None, int | None]:
    cmd = find_tv_tuner_cmdline()
    if not cmd:
        return None, None
    rf = program = None
    toks = cmd.split()
    for i, t in enumerate(toks):
        if t == "--rf" and i + 1 < len(toks):
            try:
                rf = int(toks[i + 1])
            except ValueError:
                pass
        elif t in ("--program-id", "--program") and i + 1 < len(toks):
            try:
                program = int(toks[i + 1])
            except ValueError:
                pass
    return rf, program


def load_channels() -> list[dict]:
    """Load detected channels from scan.json. Returns a list sorted by
    virtual channel number, with rows that don't actually decode
    excluded. Uses the channel's PSIP data to recover the virtual
    channel numbers (major.minor) per program, since those don't sit
    on the programs[] entries themselves.

    Schema we work with (from sdr_agent's tv_tuner scan):
      channels[i] = {
        rf, callsign, network_hint,
        programs: [{program_id, program_num, video_codec, ...}],
        psip: {channels: [{short_name, major, minor, program_number}], ...},
      }
    """
    if not SCAN_PATH.exists():
        return []
    try:
        data = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    seen = set()
    for c in data.get("channels", []):
        if c.get("not_detected"):
            continue
        if not c.get("rf"):
            continue
        callsign = c.get("callsign")
        if not callsign or callsign in ("?", "None"):
            continue
        network_hint = c.get("network_hint") or ""
        psip_channels = ((c.get("psip") or {}).get("channels") or [])
        # Map program_number -> (major.minor, short_name)
        psip_by_pnum = {p.get("program_number"): p for p in psip_channels
                        if p.get("program_number")}
        for prog in c.get("programs") or []:
            pnum = prog.get("program_num") or prog.get("program_id")
            if not pnum:
                continue
            psip_match = psip_by_pnum.get(pnum)
            if psip_match:
                major = psip_match.get("major")
                minor = psip_match.get("minor", 1)
                virtual = f"{major}.{minor}" if major else f"{c['rf']}.{pnum}"
                short = psip_match.get("short_name") or callsign
            else:
                virtual = f"{c['rf']}.{pnum}"
                short = callsign
            key = (c["rf"], virtual)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "rf":       c["rf"],
                "program":  pnum,
                "virtual":  str(virtual),
                "callsign": short,
                "network":  network_hint,
            })
    def sort_key(r):
        try:
            major, _, minor = r["virtual"].partition(".")
            return (int(major), int(minor or "1"))
        except ValueError:
            return (9999, 0)
    out.sort(key=sort_key)
    return out


def find_neighbor(channels: list[dict], rf: int | None,
                  program: int | None, direction: int) -> dict | None:
    if not channels:
        return None
    idx = None
    if rf is not None:
        for i, c in enumerate(channels):
            if c["rf"] == rf and (program is None or c["program"] == program):
                idx = i
                break
    if idx is None:
        return channels[0] if direction > 0 else channels[-1]
    new = (idx + direction) % len(channels)
    return channels[new]


def kill_running():
    if sys.platform == "win32":
        for img in ("ffmpeg.exe", "vlc.exe", "ffplay.exe"):
            # kill-ok: player/pipeline consumer (mpv/ffmpeg/tail), not the SDR holder
            subprocess.run(["taskkill", "/F", "/IM", img],
                                capture_output=True, **SUBPROC_KW)
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True,
        ).stdout
        for line in out.splitlines():
            if "tv_tuner.py" in line or "tv_live.py" in line:
                try:
                    pid = int(line.strip().rsplit(",", 1)[-1])
                    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, **SUBPROC_KW)
                except (ValueError, IndexError):
                    pass
    else:
        subprocess.run(["pkill", "-f", "tv_tuner.py"], capture_output=True)
        subprocess.run(["pkill", "-f", "tv_live.py"], capture_output=True)
        subprocess.run(["pkill", "-f", "ffmpeg"], capture_output=True)
        subprocess.run(["pkill", "-x", "vlc"], capture_output=True)


def respawn(rf: int, program: int):
    env = os.environ.copy()
    for k, v in CHAIN_DEFAULTS.items():
        env.setdefault(k, v)
    # Preserve the user's current audio-language pick if any.
    cmd = [PY, "-u", str(TV_TUNER_PY),
           "--rf", str(rf), "--program-id", str(program),
           "--player", "vlc"]
    if sys.platform == "win32":
        DETACHED = 0x00000008 | 0x00000200 | 0x08000000
        subprocess.Popen(cmd, env=env, creationflags=DETACHED,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(cmd, env=env, start_new_session=True,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("direction", choices=["next", "prev", "previous", "up", "down",
                                          "+", "-"])
    args = ap.parse_args()

    delta = +1 if args.direction in ("next", "up", "+") else -1

    channels = load_channels()
    if not channels:
        print("[chan] no scan data — run a scan via tv_tuner.py first")
        return 1

    rf, program = detect_current()
    target = find_neighbor(channels, rf, program, delta)
    if target is None:
        print("[chan] no channel to step to")
        return 1

    print(f"[chan] {('next' if delta>0 else 'prev')}: "
          f"{target['virtual']} {target['callsign']} "
          f"(RF {target['rf']} program {target['program']})")
    kill_running()
    time.sleep(2)
    respawn(target["rf"], target["program"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
