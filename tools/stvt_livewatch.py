"""One-command live watcher: tv_live + VLC, glued together.

Convenience wrapper around the two-step "start the chain, then open a
player on live.ts" recipe. Spawns tv_live.py in the background, waits
until live.ts grows past the chain's lock threshold, opens VLC on the
file, then watches both processes. When VLC closes, tv_live is shut
down cleanly so the SDR is released.

This script does NOT modify tv_live's command line — every flag you
pass is forwarded verbatim, so it cannot break the DSP chain. The only
thing it owns is the VLC subprocess and the shutdown sequence.

Why use this instead of running them separately:
  * One terminal, one Ctrl+C to tear both down.
  * The SDR gets released the moment you close VLC — no orphan
    tv_live holding the device when you wander off.
  * stvt_livewatch only adds glue. tv_live runs with exactly the
    flags you give it.

Usage:
    python tools/stvt_livewatch.py --rf 34 --program 3
    python tools/stvt_livewatch.py --rf 36 -- --extra-tv-live-flag value
    python tools/stvt_livewatch.py --rf 31 --program 1 --lock-wait 90

Everything after `--` is forwarded to tv_live.py unchanged. Use this
for chain knobs the wrapper doesn't expose (gain, EQ flavor, etc.).

NOTE: this is a convenience wrapper. It deliberately reuses the
existing tv_live.py and VLC binaries and adds NO SDR / DSP logic of
its own.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
TV_LIVE_PY = TOOLS / "tv_live.py"
LIVE_TS = TOOLS / "data" / "tv_live" / "live.ts"

DEFAULT_VLC_WIN = r"C:\Program Files\VideoLAN\VLC\vlc.exe"
DEFAULT_VLC_WIN_X86 = r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"


def find_vlc() -> str | None:
    """Locate vlc on this host. Returns absolute path or None."""
    if sys.platform == "win32":
        for cand in (DEFAULT_VLC_WIN, DEFAULT_VLC_WIN_X86):
            if Path(cand).exists():
                return cand
        return shutil.which("vlc")
    return shutil.which("vlc")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--rf", type=int, required=True,
                    help="RF channel to tune (e.g. 34 for NBC in DC)")
    ap.add_argument("--program", type=int, default=None,
                    help="ATSC program number to play in VLC (default: "
                         "let VLC pick the first one in the PAT)")
    ap.add_argument("--lock-wait", type=int, default=60,
                    help="Max seconds to wait for live.ts to grow past "
                         "the lock threshold before opening VLC anyway "
                         "(default 60)")
    ap.add_argument("--lock-bytes", type=int, default=40_000_000,
                    help="Bytes live.ts must reach to count as 'locked' "
                         "(default 40 MB ~= 16s of ATSC mux)")
    ap.add_argument("--vlc-path", default=None,
                    help="Override path to vlc binary "
                         "(default: auto-detect)")
    ap.add_argument("--no-vlc", action="store_true",
                    help="Don't spawn VLC — just run tv_live and wait. "
                         "Useful for headless recording sessions.")
    # Anything after `--` is forwarded to tv_live verbatim.
    ap.add_argument("extra_tv_live", nargs=argparse.REMAINDER,
                    help="Pass extra flags through to tv_live after --")
    return ap.parse_args()


def spawn_tv_live(rf: int, extra: list[str]) -> subprocess.Popen:
    """Start tv_live.py on the given RF in the background. We DON'T
    redirect stdout/stderr — the user expects to see tv_live's lock
    progress in the same terminal."""
    cmd = [sys.executable, "-u", str(TV_LIVE_PY), "--rf", str(rf)]
    # Strip the leading `--` separator if argparse left it in.
    if extra and extra[0] == "--":
        extra = extra[1:]
    cmd.extend(extra)
    # New process group so our Ctrl+C in the wrapper terminal hits the
    # wrapper first; the wrapper then terminates tv_live in the cleanup
    # path. Without this, Ctrl+C would hit tv_live directly and skip
    # the orderly shutdown.
    creationflags = 0
    preexec = None
    if sys.platform == "win32":
        creationflags = 0x00000200  # CREATE_NEW_PROCESS_GROUP
    else:
        preexec = os.setsid
    return subprocess.Popen(cmd, creationflags=creationflags,
                            preexec_fn=preexec)


def wait_for_lock(deadline_sec: int, lock_bytes: int) -> bool:
    """Poll live.ts until it grows past lock_bytes or deadline passes.
    Returns True if locked, False if we gave up."""
    deadline = time.time() + deadline_sec
    last_print = 0.0
    while time.time() < deadline:
        if LIVE_TS.exists():
            sz = LIVE_TS.stat().st_size
            if sz >= lock_bytes:
                print(f"[livewatch] chain locked "
                      f"({sz/1e6:.1f} MB in live.ts)")
                return True
            now = time.time()
            if now - last_print >= 2:
                print(f"[livewatch] waiting for lock... "
                      f"({sz/1e6:.1f} / {lock_bytes/1e6:.1f} MB)",
                      flush=True)
                last_print = now
        else:
            now = time.time()
            if now - last_print >= 2:
                print("[livewatch] waiting for tv_live to create live.ts...",
                      flush=True)
                last_print = now
        time.sleep(0.5)
    return False


def spawn_vlc(vlc_path: str, program: int | None) -> subprocess.Popen:
    """Open VLC on the live.ts file. Uses --programs=N when program is
    given so VLC commits to one program from the start, instead of
    flipping through the PAT."""
    cmd = [vlc_path,
           "--no-video-title-show",
           "--meta-title", "STVT livewatch (RF chain)",
           "--file-caching", "3000",
           "--input-fast-seek",
           "--ts-seek-percent",
           "--play-and-exit"]
    if program is not None:
        cmd.append(f"--programs={program}")
    cmd.append(str(LIVE_TS))
    return subprocess.Popen(cmd)


def terminate(proc: subprocess.Popen, label: str, timeout: float = 5.0):
    """Best-effort orderly shutdown for a child process."""
    if proc.poll() is not None:
        return
    print(f"[livewatch] stopping {label} (PID {proc.pid})...")
    try:
        if sys.platform == "win32":
            # On Windows, SIGTERM doesn't exist for Popen; use
            # CTRL_BREAK_EVENT for processes we spawned with
            # CREATE_NEW_PROCESS_GROUP, fall back to terminate().
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except (ValueError, OSError):
                proc.terminate()
        else:
            proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[livewatch] {label} did not exit — killing")
        try:
            proc.kill()
        except OSError:
            pass


def main() -> int:
    args = parse_args()

    if not TV_LIVE_PY.exists():
        print(f"[livewatch] tv_live.py not found at {TV_LIVE_PY}",
              file=sys.stderr)
        return 2

    vlc_path = args.vlc_path or find_vlc()
    if not args.no_vlc and not vlc_path:
        print("[livewatch] VLC not found. Install it or pass --vlc-path / "
              "--no-vlc.", file=sys.stderr)
        return 2

    extra = list(args.extra_tv_live or [])
    print(f"[livewatch] starting tv_live on RF {args.rf}"
          f"{' (extra: ' + ' '.join(extra) + ')' if extra else ''}")
    tv_live = spawn_tv_live(args.rf, extra)

    vlc_proc: subprocess.Popen | None = None
    try:
        if args.no_vlc:
            print("[livewatch] --no-vlc: tv_live running headlessly. "
                  "Ctrl+C to stop.")
            tv_live.wait()
            return 0

        locked = wait_for_lock(args.lock_wait, args.lock_bytes)
        if not locked:
            print(f"[livewatch] WARNING: live.ts didn't reach "
                  f"{args.lock_bytes/1e6:.1f} MB in {args.lock_wait}s. "
                  f"Opening VLC anyway — playback may stutter until "
                  f"the chain catches up.", file=sys.stderr)

        if tv_live.poll() is not None:
            print(f"[livewatch] tv_live exited (code {tv_live.returncode}) "
                  f"before lock — check chain logs.", file=sys.stderr)
            return tv_live.returncode or 1

        vlc_proc = spawn_vlc(vlc_path, args.program)
        print(f"[livewatch] VLC running (PID {vlc_proc.pid}). "
              f"Close VLC to stop tv_live and release the SDR.")

        # Wait for whichever child exits first. If VLC closes, we stop
        # tv_live; if tv_live dies, we close VLC.
        while True:
            time.sleep(0.5)
            if vlc_proc.poll() is not None:
                print(f"[livewatch] VLC exited (code {vlc_proc.returncode})")
                break
            if tv_live.poll() is not None:
                print(f"[livewatch] tv_live exited "
                      f"(code {tv_live.returncode})")
                break

        return 0

    except KeyboardInterrupt:
        print("\n[livewatch] Ctrl+C — shutting down")
        return 130
    finally:
        if vlc_proc is not None:
            terminate(vlc_proc, "VLC", timeout=3)
        terminate(tv_live, "tv_live", timeout=10)
        print("[livewatch] done")


if __name__ == "__main__":
    sys.exit(main())
