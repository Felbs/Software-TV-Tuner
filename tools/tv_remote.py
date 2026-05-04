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
      ║                           ║       ★    │    ⏻    │
   *  ║   SOFTWARE TV REMOTE      ║            ├─────────┤
      ║                           ║       .    │  1 2 3  │      *
      ╚═══════════════════════════╝            │  4 5 6  │
                                                │  7 8 9  │
   ✦                 by Felbs                   │    0    │   ✦
                                                ├─────────┤
                                                │    ▲    │
   .   *   .   ✦   *   .   *   .   ✦            │  ◀ ● ▶  │      *
                                                │    ▼    │
                                                ├─────────┤
                                                │ vol  ch │
       *   .   *   ✦   *   .   *   ✦   .   *  . └─────────┘  *
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


def kill_existing_tv_tuner():
    """If there's a tv_tuner already running (e.g. from a separate
    PowerShell window), shut it down so the remote owns the only TV
    pipeline. Reads ~/.tv_tuner/tv_tuner.pid which tv_tuner writes
    when run_pipeline starts and clears on graceful exit."""
    if not PID_PATH.exists():
        return
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return
    if pid == os.getpid():
        return
    try:
        # Probe the PID — os.kill(pid, 0) raises if it's gone.
        if sys.platform == "win32":
            import ctypes
            PROCESS_TERMINATE = 0x0001
            PROCESS_QUERY_INFORMATION = 0x0400
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_TERMINATE | PROCESS_QUERY_INFORMATION, False, pid)
            if not handle:
                # Process is gone; clear stale lockfile.
                try:
                    PID_PATH.unlink()
                except OSError:
                    pass
                return
            print(f"[remote] closing existing tv_tuner (PID {pid})...")
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.CloseHandle(handle)
        else:
            os.kill(pid, signal.SIGTERM)
            print(f"[remote] closing existing tv_tuner (PID {pid})...")
        # Wait for SDR + TV window to actually release.
        for _ in range(20):
            if not PID_PATH.exists():
                break
            time.sleep(0.25)
        try:
            PID_PATH.unlink()
        except OSError:
            pass
        # Extra grace for SDR driver release after a force-kill.
        time.sleep(2)
    except Exception as e:
        print(f"[remote] couldn't kill PID {pid}: {e}")


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
    # Shut down any tv_tuner running from a previous session so we own
    # the only TV pipeline.
    kill_existing_tv_tuner()
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
            kill_subprocess(current)
            current = None
            time.sleep(2)  # Let SDR fully release before re-opening.
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
