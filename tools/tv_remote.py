"""Software TV Remote — channel-flipping front-end for tv_tuner.

Run this in its own PowerShell window alongside (or instead of) the
interactive tv_tuner picker. It shows the TV guide and a channel
prompt. Each time you type a row number or virtual channel and press
Enter, the remote kills any currently-tuned pipeline and launches a
fresh tv_tuner --rf X --program Y subprocess for the new channel.
The TV window flashes for a moment during the swap, then comes up on
the new channel — same UX as a hardware remote.

Loads ~/.tv_tuner/scan.json the same way the picker does. Run a
scan first if you don't have one:
    python tv_tuner.py --scan --region na
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Reuse tv_tuner's loaders and renderers so the guide format stays
# identical between the picker and the remote.
from tv_tuner import (  # noqa: E402
    load_scan, print_scan_table, expand_channels_from_scan,
    PYTHON_EXE, NEW_PROCESS_GROUP, SCAN_PATH, PID_PATH,
)

TV_TUNER_PY = HERE / "tv_tuner.py"


REMOTE_BANNER = r"""
   .   *   ✦   .   *   .   ✦   *   .   *   ✦   .   *   .   *

      ╔═══════════════════════════╗            ┌─────────┐
      ║                           ║      ★     │   (O)   │
   *  ║   SOFTWARE TV REMOTE      ║            ├─────────┤      *
      ║                           ║      .     │  1 2 3  │
      ╚═══════════════════════════╝            │  4 5 6  │   ✦
                                                │  7 8 9  │
   ✦       ___                                  │    0    │
          ( o o )         *                     ├─────────┤      *
   .       \ - /                                │   /^\   │
          _/ | \_                               │ < ( ) > │
   *     /       \           .                  │   \v/   │
         |  ~~~  |                              ├─────────┤   ✦
   .     \_______/      ✦                       │ vol  ch │
                                                └─────────┘     *
       *   .   *   ✦   *   .   *   ✦   .   *   .   ✦   .   *
"""


def kill_subprocess(proc: subprocess.Popen | None):
    """Best-effort terminate of the currently-playing tv_tuner."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            # Send Ctrl+Break to the whole process group so tv_tuner's
            # signal handler runs and shuts ffmpeg/ffplay/tv_live too.
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    except Exception:
        pass


def nuke_orphans():
    """Scan for stuck Software TV Tuner processes from a crashed
    previous session and kill them. The PID lockfile only knows
    about the parent process; if that parent crashed, its children
    (tv_live still holding the SDR, ffmpeg, ffplay) become orphans
    that pin CPU and prevent a new scan from opening the device.

    We identify orphans conservatively: only python.exe processes
    whose command line references one of our scripts, plus any
    ffmpeg/ffplay process whose command line includes the
    distinctive pipe + decoder flags our pipeline uses. This avoids
    nuking unrelated VLC / Plex / OBS ffmpeg instances."""
    if sys.platform != "win32":
        return 0
    # Single PowerShell call gets every candidate's PID + command line.
    ps_cmd = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name='python.exe' or Name='ffmpeg.exe' or Name='ffplay.exe'\" "
        "| Select-Object ProcessId, Name, CommandLine "
        "| ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return 0
    if result.returncode != 0 or not result.stdout.strip():
        return 0
    import json as _json
    try:
        items = _json.loads(result.stdout)
    except _json.JSONDecodeError:
        return 0
    if isinstance(items, dict):
        items = [items]
    me = os.getpid()
    target_pids = []
    for it in items:
        pid = it.get("ProcessId")
        if not pid or int(pid) == me:
            continue
        name = (it.get("Name") or "").lower()
        cmdline = it.get("CommandLine") or ""
        cmdline_l = cmdline.lower()
        is_ours = False
        if name == "python.exe":
            for marker in ("tv_tuner.py", "tv_live.py",
                           "tv_live_softvit.py", "sdr_sweep.py",
                           "tv_remote.py", "tv_player.py"):
                if marker in cmdline_l:
                    is_ours = True
                    break
        elif name in ("ffmpeg.exe", "ffplay.exe"):
            # Distinctive flag combos used by tv_tuner's build_ffmpeg_cmd
            # and spawn_ffplay. Avoids nuking unrelated ffmpeg jobs.
            if (("-f mpegts -i pipe:0" in cmdline_l) or
                ("tv_tuner" in cmdline_l) or
                ("data\\tv_live" in cmdline_l) or
                ("tv_live\\live.ts" in cmdline_l)):
                is_ours = True
        if is_ours:
            target_pids.append(int(pid))
    if not target_pids:
        return 0
    print(f"[remote] sweeping {len(target_pids)} stuck process(es) "
          f"from a previous session...")
    for pid in target_pids:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
    # SDRplay needs a beat to fully release after a forced kill.
    time.sleep(3)
    try:
        PID_PATH.unlink()
    except OSError:
        pass
    return len(target_pids)


def kill_existing_tv_tuner():
    """If there's a tv_tuner already running (e.g. from a separate
    PowerShell window or a previous remote pick), shut down it AND its
    children — tv_live, ffmpeg, ffplay — so the SDR is fully released
    before we spawn a fresh tv_tuner. Reads ~/.tv_tuner/tv_tuner.pid.

    Uses Windows `taskkill /F /T /PID` because TerminateProcess on the
    parent leaves grandchildren (tv_live holding the SDR, ffmpeg,
    ffplay) orphaned — the next tv_tuner then can't open the SDR and
    crashes a few seconds in. /T terminates the whole process tree."""
    if not PID_PATH.exists():
        return False
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    if pid == os.getpid():
        return False
    print(f"[remote] closing existing tv_tuner (PID {pid}) and children...")
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception as e:
        print(f"[remote] couldn't kill tree for PID {pid}: {e}")
        return False
    # Wait until the lockfile is cleared (tv_tuner removes it on exit)
    # OR a few seconds have passed (taskkill /F is forced — lockfile
    # may not get cleaned).
    for _ in range(20):
        if not PID_PATH.exists():
            break
        time.sleep(0.25)
    try:
        PID_PATH.unlink()
    except OSError:
        pass
    # Give the SDRplay driver enough time to fully release the device
    # before the next tv_live tries to open it.
    time.sleep(3)
    return True


def launch_channel(rf: int, program: int) -> subprocess.Popen:
    """Spawn `tv_tuner.py --rf X --program Y` for the given channel.
    Returns a Popen handle — caller is responsible for killing it
    before launching another."""
    # `--program-id` tells tv_tuner the value is the canonical PSIP
    # program_id, not a 1-based subchannel index — so it skips the
    # probe_program_id translation that would otherwise pick the wrong
    # subchannel (e.g. WTTG-DT 5.1's program_id is 3, not 1).
    cmd = [
        PYTHON_EXE, "-u", str(TV_TUNER_PY),
        "--rf", str(rf), "--program-id", str(program),
        "--player", "ffplay",
    ]
    print(f"[remote] spawning: tv_tuner --rf {rf} --program-id {program}")
    return subprocess.Popen(
        cmd,
        creationflags=NEW_PROCESS_GROUP,
    )


def resolve_channel(ans: str, rows: list[dict]) -> dict | None:
    """Translate user input ("12", "5.1", etc.) into a row dict.
    Bare integer = row index. Dotted = virtual channel match."""
    if "." in ans:
        for r in rows:
            if r["virtual"] == ans:
                return r
        return None
    try:
        n = int(ans)
        if 1 <= n <= len(rows):
            return rows[n - 1]
    except ValueError:
        pass
    return None


def main() -> int:
    print(REMOTE_BANNER)
    # Sweep stuck-from-crashed-session processes: orphaned tv_live still
    # holding the SDR, leftover ffmpeg/ffplay eating CPU. We do this
    # only if the PID lockfile is missing or stale (suggesting a previous
    # parent crashed without cleaning up its children); otherwise we
    # assume a tv_tuner is legitimately playing right now and leave it
    # alone until the user picks a new channel.
    if not PID_PATH.exists():
        nuke_orphans()
    scan = load_scan()
    if scan is None:
        print(f"\nNo scan found at {SCAN_PATH}.")
        print("Run a scan first:")
        print("    python tv_tuner.py --scan --region na")
        return 1
    rows = print_scan_table(scan)
    if not rows:
        print("(no channels — try re-scanning)")
        return 1

    current: subprocess.Popen | None = None
    last_label = ""

    try:
        while True:
            try:
                if last_label:
                    prompt = (f"\n📺  Now tuned: {last_label} — "
                              f"channel? [row # or 5.1, g=guide, q=quit]: ")
                else:
                    prompt = ("\n📺  Channel? [row # or 5.1, g=guide, "
                              "q=quit]: ")
                ans = input(prompt).strip().lower()
            except EOFError:
                break
            if ans in ("q", "quit", "exit"):
                break
            if ans in ("g", "guide", "list", "?"):
                rows = print_scan_table(scan)
                continue
            if not ans:
                continue
            r = resolve_channel(ans, rows)
            if r is None:
                print("  invalid — type a row #, a virtual channel like "
                      "5.1, or 'g' to show the guide again")
                continue
            if r.get("not_detected"):
                print(f"  ! {r['virtual']} {r['callsign']} wasn't "
                      f"detected by the last scan — tv_tuner will try "
                      f"and likely fail (signal too weak at this antenna).")
            # Two cleanup paths: (a) we own the current subprocess and
            # can terminate-and-wait it cleanly; (b) some OTHER tv_tuner
            # is running (the user opened the remote while a TV was
            # already playing) — find it via the PID lockfile and force-
            # kill its whole process tree so the SDR is fully released.
            kill_subprocess(current)
            current = None
            kill_existing_tv_tuner()
            try:
                current = launch_channel(r["rf"], r["program"])
                last_label = (f"{r['virtual']} {r['callsign']}"
                              + (f" {r.get('network', '')}"
                                 if r.get('network') and
                                    r['network'].upper() != r['callsign'].upper()
                                 else ""))
            except Exception as e:
                print(f"  spawn failed: {e}")
                last_label = ""
    finally:
        kill_subprocess(current)

    print("\n[remote] goodbye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
