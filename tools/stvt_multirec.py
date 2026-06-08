"""Multi-program recorder for a single ATSC 1.0 mux.

An ATSC 1.0 multiplex (one RF channel, ~6 MHz) carries N programs
interleaved on shared transport stream PIDs. Because tv_live.py
captures the entire mux into live.ts, we can demultiplex every program
at once with ffmpeg's per-program -map filter and write each to its
own .ts file.

This is the differentiator vs. a traditional single-tuner TV box:
ONE SDR tuning records EVERY channel in that multiplex simultaneously,
losslessly, at full bit-rate (we use -c copy = no transcode).

Usage:
    python tools/stvt_multirec.py --rf 36 --duration 5
    python tools/stvt_multirec.py --rf 34 --duration 30 --programs 4,41,42
    python tools/stvt_multirec.py --rf 35 --duration 60 --output-dir D:/Recordings
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tv_tuner import (
    FFMPEG, LIVE_TS, SCAN_PATH,
    env_with_sdrplay, spawn_tv_live, wait_for_live_ts,
    measure_convergence, kill_proc,
)

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO / "tools" / "recordings"

SUBPROC_KW = {}
if sys.platform == "win32":
    SUBPROC_KW["creationflags"] = 0x08000000  # CREATE_NO_WINDOW


def load_mux_programs(rf: int) -> list[dict]:
    if not SCAN_PATH.exists():
        return []
    try:
        data = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    chan = next((c for c in data.get("channels", [])
                 if c.get("rf") == rf), None)
    if not chan:
        return []
    psip = chan.get("psip") or {}
    psip_by_pnum = {p.get("program_number"): p
                    for p in (psip.get("channels") or [])
                    if p.get("program_number")}
    callsign = chan.get("callsign") or "?"
    out = []
    for prog in chan.get("programs") or []:
        pnum = prog.get("program_num") or prog.get("program_id")
        if not pnum:
            continue
        match = psip_by_pnum.get(pnum) or {}
        major = match.get("major")
        minor = match.get("minor", 1)
        virtual = f"{major}.{minor}" if major else f"{rf}.{pnum}"
        short = match.get("short_name") or callsign
        out.append({
            "program_number": pnum,
            "virtual": virtual,
            "callsign": short,
            "video_codec": prog.get("video_codec") or "?",
        })
    return out


def safe_callsign(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:16]


def build_ffmpeg_cmd(programs: list[dict], out_dir: Path,
                     timestamp: str, rf: int) -> tuple[list[str], list[Path]]:
    """Build the ffmpeg command for per-program demux outputs.

    The whole-mux passthrough (mux_FULL_rfN_*.ts), when enabled, is
    written separately by a parallel tail thread — NOT by ffmpeg. That's
    because `ffmpeg -map 0 -c copy -f mpegts` rewrites the PAT into a
    single synthetic program containing all source streams, losing the
    multi-program structure that makes the FULL file useful (VLC's
    Playback > Program submenu, ffprobe -show_programs, etc.). Raw
    byte-for-byte tee of live.ts preserves the original 7-program PAT.
    """
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-loglevel", "warning",
        # Read from stdin (pipe:0) so ffmpeg sees an open stream, not a
        # finite file. A tail thread feeds bytes from live.ts as they
        # arrive — ffmpeg never hits EOF until we close its stdin.
        # With 7 programs in one mux, analyze needs ~20 MB samples to
        # enumerate every codec's parameters.
        "-analyzeduration", "20000000",
        "-probesize", "30000000",
        "-fflags", "+discardcorrupt",
        "-err_detect", "ignore_err",
        "-f", "mpegts",
        "-i", "pipe:0",
    ]
    out_files = []
    for p in programs:
        pnum = p["program_number"]
        virt = p["virtual"].replace(".", "_")
        cs = safe_callsign(p["callsign"])
        fname = f"mux_p{pnum}_{virt}_{cs}_{timestamp}.ts"
        path = out_dir / fname
        cmd += [
            "-map", f"0:p:{pnum}",
            "-c", "copy",
            "-f", "mpegts",
            str(path),
        ]
        out_files.append(path)
    return cmd, out_files


def mux_full_path(out_dir: Path, rf: int, timestamp: str) -> Path:
    return out_dir / f"mux_FULL_rf{rf}_{timestamp}.ts"


def fmt_mb(n: int) -> str:
    return f"{n / (1024 * 1024):6.1f} MB"


def tail_into_pipe(src_path: Path, dest_fh, stop_evt: threading.Event,
                   start_offset: int = 0, tee_fh=None):
    """Tail-and-pipe: read new bytes from src_path as it grows and write
    them to dest_fh. Mirrors `tail -F` for ffmpeg's stdin so it sees an
    endless stream rather than a finite file. Returns when stop_evt is
    set or dest_fh becomes unwritable.

    If tee_fh is supplied, the same bytes are also written to it. We use
    this in multirec to write the whole-mux passthrough file directly
    from the raw live.ts stream (preserving the multi-program PAT that
    ffmpeg's remux would otherwise flatten). A tee_fh write failure does
    not interrupt the primary pipe; the tee is dropped silently after a
    failure to keep ffmpeg fed."""
    chunk = 256 * 1024
    pos = start_offset
    while not stop_evt.is_set():
        try:
            with open(src_path, "rb") as f:
                f.seek(pos)
                while not stop_evt.is_set():
                    data = f.read(chunk)
                    if not data:
                        time.sleep(0.05)
                        continue
                    try:
                        dest_fh.write(data)
                        dest_fh.flush()
                    except (BrokenPipeError, OSError, ValueError):
                        return
                    if tee_fh is not None:
                        try:
                            tee_fh.write(data)
                        except (OSError, ValueError):
                            tee_fh = None
                    pos += len(data)
        except FileNotFoundError:
            time.sleep(0.2)
        except OSError:
            time.sleep(0.2)


def status_printer(out_files: list[Path], programs: list[dict],
                   stop_evt: threading.Event, started_at: float,
                   duration_sec: float):
    while not stop_evt.is_set():
        elapsed = time.time() - started_at
        remaining = max(0, duration_sec - elapsed)
        print(f"\n[multirec] +{int(elapsed):>4}s / -{int(remaining):>4}s    "
              f"file sizes:")
        for i, f in enumerate(out_files):
            try:
                sz = f.stat().st_size
            except FileNotFoundError:
                sz = 0
            rate_kbs = (sz / max(1, elapsed)) / 1024
            if i < len(programs):
                p = programs[i]
                label = (f"prog {p['program_number']:>3}  "
                         f"{p['virtual']:<6}  {p['callsign']:<10}")
            else:
                label = "[FULL MUX]                       "
            print(f"  {label}  {fmt_mb(sz)}  ({rate_kbs:5.0f} KB/s)")
        if stop_evt.wait(timeout=5):
            return


def main() -> int:
    # A bare SIGTERM (from `timeout`, `kill`, or a supervisor) terminates
    # Python WITHOUT running `finally`, which would leave the spawned tv_live
    # chain holding the SDR forever. Turn SIGTERM into a KeyboardInterrupt so
    # the cleanup path below runs exactly like Ctrl-C. (SIGINT already does.)
    def _term(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _term)
    except (ValueError, OSError):
        pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rf", type=int, required=True,
                    help="ATSC RF channel number (the multiplex to tune)")
    ap.add_argument("--duration", type=float, default=5.0,
                    help="Record duration in minutes (default 5)")
    ap.add_argument("--programs", default=None,
                    help="Comma-separated program numbers (default: all)")
    ap.add_argument("--output-dir", default=str(DEFAULT_OUT_DIR),
                    help=f"Output directory (default {DEFAULT_OUT_DIR})")
    ap.add_argument("--keep-mux", action="store_true", default=None,
                    help="Force-write the whole-mux passthrough as a single "
                         "mux_FULL_rfN_<ts>.ts file (every program, every "
                         "PID). Default: ON for whole-RF records (no "
                         "--programs filter), OFF when --programs is set so "
                         "single-channel records stay small. Pass this flag "
                         "to force ON even with --programs.")
    ap.add_argument("--no-keep-mux", action="store_true",
                    help="Skip the whole-mux file even on whole-RF records, "
                         "to save disk.")
    args = ap.parse_args()

    duration_sec = args.duration * 60.0
    out_dir = Path(os.path.expanduser(args.output_dir))
    out_dir.mkdir(parents=True, exist_ok=True)

    programs = load_mux_programs(args.rf)
    if not programs:
        print(f"[multirec] RF {args.rf} has no programs in scan.json — "
              f"run `tv_tuner.py --scan` first, or pick a different RF.",
              file=sys.stderr)
        return 1

    if args.programs:
        wanted = {int(x) for x in args.programs.split(",")}
        programs = [p for p in programs if p["program_number"] in wanted]
        if not programs:
            print("[multirec] none of the requested programs are in this mux",
                  file=sys.stderr)
            return 1

    print(f"[multirec] RF {args.rf}  —  {len(programs)} programs to record "
          f"for {args.duration:g} minutes:")
    for p in programs:
        print(f"  prog {p['program_number']:>3}  {p['virtual']:<6}  "
              f"{p['callsign']:<10}  ({p['video_codec']})")
    if args.no_keep_mux:
        mux_msg = "off (--no-keep-mux)"
    elif args.keep_mux:
        mux_msg = "on (--keep-mux force)"
    elif args.programs:
        mux_msg = "off (--programs filter set; single-channel mode)"
    else:
        mux_msg = "ON (whole-RF record; one big file with every program)"
    print(f"[multirec] whole-mux passthrough file: {mux_msg}")
    print(f"[multirec] output: {out_dir}")

    try:
        LIVE_TS.unlink()
    except (FileNotFoundError, PermissionError, OSError):
        pass

    log_path = out_dir / f"tv_live_rf{args.rf}.log"
    log_fh = log_path.open("w", encoding="utf-8", errors="replace")
    print(f"[multirec] starting tv_live (RF {args.rf})...")
    try:
        tv_live = spawn_tv_live(args.rf, log_fh, viterbi="stock")
    except Exception as e:
        print(f"[multirec] tv_live spawn failed: {e}", file=sys.stderr)
        log_fh.close()
        return 1
    # Safety net: guarantee the SDR-holding chain dies on ANY exit path
    # (covers the window before the try/finally below, and clean shutdown).
    # kill_proc is idempotent — a second call after finally is a no-op.
    atexit.register(kill_proc, tv_live, "tv_live")

    ffmpeg_proc = None
    stop_evt = threading.Event()
    status_thread = None
    out_files: list[Path] = []
    full_mux_fh = None
    keep_mux = False
    started_at = 0.0
    try:
        print("[multirec] waiting for chain to lock (up to 30s)...")
        if not wait_for_live_ts(timeout_sec=30.0):
            print("[multirec] chain never produced live.ts bytes — abort",
                  file=sys.stderr)
            return 2

        deadline = time.time() + 10.0
        locked = False
        while time.time() < deadline:
            if tv_live.poll() is not None:
                print("[multirec] tv_live died during convergence",
                      file=sys.stderr)
                return 2
            pat = measure_convergence()
            if pat >= 5:
                print(f"[multirec] chain locked (PAT={pat} in 5MB).")
                locked = True
                break
            time.sleep(1.0)
        if not locked:
            print("[multirec] chain didn't converge in 10s — abort",
                  file=sys.stderr)
            return 2

        # Build up enough live.ts for ffmpeg's analyze pass before
        # spawning. At ~1 MB/s mux rate, 20 MB takes ~20s — ffmpeg's
        # probesize+analyzeduration need this much to enumerate every
        # program's codec parameters reliably.
        print("[multirec] buffering live.ts for ffmpeg analyze pass...")
        buf_deadline = time.time() + 30.0
        while time.time() < buf_deadline:
            try:
                sz = LIVE_TS.stat().st_size
            except OSError:
                sz = 0
            if sz >= 20_000_000:
                print(f"[multirec] live.ts has {sz/1e6:.0f} MB — proceeding.")
                break
            if tv_live.poll() is not None:
                print("[multirec] tv_live died while buffering",
                      file=sys.stderr)
                return 2
            time.sleep(1.0)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Smart default for keep_mux:
        #   --programs N      -> off (small single-channel file)
        #   no --programs     -> on  (whole-RF; one file with all channels
        #                              so VLC can switch programs)
        #   --no-keep-mux     -> always off (override, save disk)
        #   --keep-mux        -> always on  (override, force include)
        if args.no_keep_mux:
            keep_mux = False
        elif args.keep_mux:
            keep_mux = True
        else:
            keep_mux = not args.programs
        cmd, out_files = build_ffmpeg_cmd(programs, out_dir, timestamp,
                                          args.rf)
        # The FULL mux file is written by the tail thread as a raw tee
        # of live.ts (NOT by ffmpeg) so the original multi-program PAT
        # is preserved. See build_ffmpeg_cmd's docstring for why.
        if keep_mux:
            full_path = mux_full_path(out_dir, args.rf, timestamp)
            full_mux_fh = open(full_path, "wb")
            out_files.append(full_path)
        ffmpeg_log = out_dir / f"ffmpeg_rf{args.rf}_{timestamp}.log"
        ffmpeg_log_fh = ffmpeg_log.open("w", encoding="utf-8", errors="replace")
        print(f"[multirec] spawning ffmpeg with {len(programs)} parallel outputs...")
        ffmpeg_proc = subprocess.Popen(
            cmd, env=env_with_sdrplay(),
            stdin=subprocess.PIPE,
            stdout=ffmpeg_log_fh, stderr=subprocess.STDOUT,
            **SUBPROC_KW,
        )

        # Start tail thread feeding live.ts -> ffmpeg stdin. Start at
        # offset 0 so ffmpeg sees a fresh stream (it owns its own analyze
        # buffer; replaying from the start is the same cost either way).
        # When keep_mux is on, tail_into_pipe also tees the raw bytes
        # to full_mux_fh, preserving the source PAT (7 programs).
        tail_thread = threading.Thread(
            target=tail_into_pipe,
            args=(LIVE_TS, ffmpeg_proc.stdin, stop_evt, 0),
            kwargs={"tee_fh": full_mux_fh},
            daemon=True,
        )
        tail_thread.start()

        started_at = time.time()
        status_thread = threading.Thread(
            target=status_printer,
            args=(out_files, programs, stop_evt, started_at, duration_sec),
            daemon=True,
        )
        status_thread.start()

        deadline = started_at + duration_sec
        while time.time() < deadline:
            if tv_live.poll() is not None:
                print("[multirec] tv_live died mid-record — stopping",
                      file=sys.stderr)
                break
            if ffmpeg_proc.poll() is not None:
                print(f"[multirec] ffmpeg died mid-record (check {ffmpeg_log})",
                      file=sys.stderr)
                break
            time.sleep(1.0)

        print("\n[multirec] duration reached, shutting down...")

    finally:
        stop_evt.set()
        if status_thread is not None:
            status_thread.join(timeout=2)
        if ffmpeg_proc is not None and ffmpeg_proc.stdin is not None:
            try:
                ffmpeg_proc.stdin.close()
            except (OSError, BrokenPipeError):
                pass
            try:
                ffmpeg_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        kill_proc(ffmpeg_proc, "ffmpeg")
        kill_proc(tv_live, "tv_live")
        if full_mux_fh is not None:
            try:
                full_mux_fh.flush()
                full_mux_fh.close()
            except (OSError, ValueError):
                pass
        try:
            log_fh.close()
        except Exception:
            pass

    elapsed = time.time() - started_at if started_at else 0
    print("\n[multirec] recap:")
    total = 0
    for i, f in enumerate(out_files):
        try:
            sz = f.stat().st_size
        except FileNotFoundError:
            sz = 0
        total += sz
        verdict = "ok  " if sz > 1_000_000 else "tiny"
        if i < len(programs):
            p = programs[i]
            label = (f"prog {p['program_number']:>3}  "
                     f"{p['virtual']:<6} {p['callsign']:<10}")
        else:
            label = "[FULL MUX] (all programs, all PIDs)        "
        print(f"  {verdict} {label}  {fmt_mb(sz)}  -> {f.name}")
    rate = (total / max(1, elapsed)) / 1024 if elapsed else 0
    print(f"\n[multirec] total: {fmt_mb(total)} across {len(programs)} programs "
          f"in {elapsed:.0f}s ({rate:.0f} KB/s aggregate)")
    print(f"[multirec] files in: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
