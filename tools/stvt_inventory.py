"""STVT recording inventory: list every .ts file under recordings/ with
size, duration, and program metadata.

Multirec drops files into `tools/recordings/` with a naming convention
that encodes the mux + program + virtual + title + timestamp, e.g.

    mux_p3_5_1_WTTG-DT_20260604_224641.ts
    mux_FULL_rf31_20260605_012429.ts

After a busy night you can end up with dozens of files and no easy way
to see which ones are full-length, which are stubs (30s shovels from a
chain that never locked), and which ones are duplicates. This tool
walks the directory and renders a human-readable table.

Duration is read from ffprobe so we know what's actually in the file
(not just its size). Stubs and zero-duration files get flagged so you
can sweep them out before they pile up.

The recording directory defaults to `tools/recordings/`, but a custom
path can be passed to look at archived material on another drive.

Usage:
    python tools/stvt_inventory.py
    python tools/stvt_inventory.py --dir D:/dvr-archive
    python tools/stvt_inventory.py --json
    python tools/stvt_inventory.py --short-only      # show < 60s files
    python tools/stvt_inventory.py --no-ffprobe      # skip duration probe
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_REC_DIR = REPO / "tools" / "recordings"

# Best-effort ffprobe locations. Each candidate is tried in order.
FFPROBE_CANDIDATES = [
    r"C:\ffmpeg\bin\ffprobe.exe",
    "ffprobe",
]


def find_ffprobe() -> str | None:
    """Return a usable ffprobe path, or None if no binary is reachable.

    Why probe via subprocess instead of shutil.which: on Windows the
    bundled ffmpeg lives at C:\\ffmpeg\\bin\\ffprobe.exe and isn't on
    PATH for cmd-launched terminals, but everywhere else `ffprobe` on
    PATH is preferred."""
    import shutil
    for cand in FFPROBE_CANDIDATES:
        if os.path.isabs(cand) and Path(cand).exists():
            return cand
        path = shutil.which(cand)
        if path:
            return path
    return None


# Name patterns multirec writes. Captures whatever metadata it can,
# leaves blanks for what it can't parse. The matches don't enforce
# anything — unmatched files still get listed.
FULL_RE = re.compile(
    r"^mux_FULL_rf(?P<rf>\d+)_(?P<date>\d{8})_(?P<time>\d{6})\.ts$"
)
PROG_RE = re.compile(
    # `short` can contain underscores (e.g. "WETA_UK"), so we use a
    # non-greedy match and anchor on the trailing date_time.ts.
    r"^mux_p(?P<pnum>\d+)_(?P<major>\d+)_(?P<minor>\d+)_"
    r"(?P<short>.+?)_(?P<date>\d{8})_(?P<time>\d{6})\.ts$"
)
# Older / hand-rolled naming used by stvt_remote
REMOTE_RE = re.compile(
    r"^(?P<time>\d{6})_v(?P<virtual>\d+\.\d+)_(?P<short>[^_]+)"
    r"(?:_(?P<title>.+))?\.ts$"
)


@dataclass
class Entry:
    path:        str
    size_mb:     float
    mtime_iso:   str
    duration_s:  float | None
    kind:        str       # "full", "program", "remote", "unknown"
    rf:          int | None
    virtual:     str | None
    short_name:  str | None
    title:       str | None
    started_at:  str | None    # parsed from filename
    note:        str       # human-readable flag (e.g. "STUB", "empty")


def parse_filename(name: str) -> dict:
    """Match a recording's filename against the known patterns. Returns
    a dict with the fields we could extract."""
    m = FULL_RE.match(name)
    if m:
        return {
            "kind":   "full",
            "rf":     int(m.group("rf")),
            "virtual": None,
            "short":  None,
            "title":  None,
            "stamp":  f"{m.group('date')}_{m.group('time')}",
        }
    m = PROG_RE.match(name)
    if m:
        return {
            "kind":   "program",
            "rf":     None,   # not in this pattern — mux RF is implicit
            "virtual": f"{m.group('major')}.{m.group('minor')}",
            "short":  m.group("short"),
            "title":  None,
            "stamp":  f"{m.group('date')}_{m.group('time')}",
        }
    m = REMOTE_RE.match(name)
    if m:
        return {
            "kind":   "remote",
            "rf":     None,
            "virtual": m.group("virtual"),
            "short":  m.group("short"),
            "title":  m.group("title") or None,
            "stamp":  None,   # remote names have only HHMMSS
        }
    return {"kind": "unknown", "rf": None, "virtual": None, "short": None,
            "title": None, "stamp": None}


def stamp_to_iso(stamp: str | None) -> str | None:
    """Parse a YYYYMMDD_HHMMSS stamp into ISO format. None if input is
    None or unparseable."""
    if not stamp:
        return None
    try:
        dt = datetime.strptime(stamp, "%Y%m%d_%H%M%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def probe_duration(ffprobe: str, path: Path) -> float | None:
    """Return container duration in seconds via ffprobe, or None on
    failure. Times out at 10s so a corrupt file can't hang the tool."""
    try:
        rc = subprocess.run(
            [ffprobe, "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(path)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if rc.returncode != 0:
        return None
    try:
        return float(rc.stdout.strip())
    except ValueError:
        return None


def classify(size_mb: float, duration_s: float | None) -> str:
    """Human-readable note about whether the file looks OK, stubby, or
    empty. The thresholds are deliberately conservative: a 30s file
    from a chain that never locked is the usual failure mode."""
    if size_mb == 0:
        return "EMPTY (0 bytes)"
    if duration_s is None:
        # ffprobe failed; size alone -- under 1 MB is almost certainly junk
        return "no-probe; <1MB" if size_mb < 1 else "no-probe"
    if duration_s < 5:
        return f"STUB ({duration_s:.0f}s)"
    if duration_s < 60:
        return f"SHORT ({duration_s:.0f}s)"
    return "OK"


def collect(rec_dir: Path, ffprobe: str | None) -> list[Entry]:
    out: list[Entry] = []
    if not rec_dir.exists():
        return out
    for path in sorted(rec_dir.glob("*.ts")):
        try:
            st = path.stat()
        except OSError as exc:
            print(f"[inventory] cannot stat {path.name}: {exc}",
                  file=sys.stderr)
            continue
        size_mb = st.st_size / 1_000_000
        meta = parse_filename(path.name)
        duration = probe_duration(ffprobe, path) if ffprobe else None
        out.append(Entry(
            path=str(path),
            size_mb=size_mb,
            mtime_iso=datetime.fromtimestamp(st.st_mtime)
                              .strftime("%Y-%m-%d %H:%M:%S"),
            duration_s=duration,
            kind=meta["kind"],
            rf=meta["rf"],
            virtual=meta["virtual"],
            short_name=meta["short"],
            title=meta["title"],
            started_at=stamp_to_iso(meta["stamp"]),
            note=classify(size_mb, duration),
        ))
    return out


def render_table(entries: list[Entry]) -> str:
    if not entries:
        return "(no recordings found)"
    lines = []
    header = (f"{'file':<48} {'size':>10} {'len':>8} {'kind':<8} "
              f"{'virt':<6} {'callsign':<10} {'note':<14}")
    lines.append(header)
    lines.append("-" * len(header))
    for e in entries:
        name = Path(e.path).name
        if len(name) > 48:
            name = name[:45] + "..."
        size_str = f"{e.size_mb:>8.1f} MB"
        len_str = (f"{int(e.duration_s)//60:>3}m{int(e.duration_s)%60:02d}s"
                   if e.duration_s is not None else "  ?")
        virt = e.virtual or ""
        short = (e.short_name or "")[:10]
        lines.append(f"{name:<48} {size_str:>10} {len_str:>8} "
                     f"{e.kind:<8} {virt:<6} {short:<10} {e.note:<14}")
    # Totals
    total_mb = sum(e.size_mb for e in entries)
    total_secs = sum((e.duration_s or 0) for e in entries)
    flagged = sum(1 for e in entries
                  if e.note not in ("OK", "no-probe") and
                  not e.note.startswith("no-probe"))
    lines.append("")
    lines.append(f"  {len(entries)} files, "
                 f"{total_mb/1024:.2f} GB total, "
                 f"{int(total_secs)//3600}h{(int(total_secs)%3600)//60}m "
                 f"of content")
    if flagged:
        lines.append(f"  {flagged} flagged as STUB/SHORT/EMPTY — "
                     f"re-run with --short-only to list just those")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dir", default=str(DEFAULT_REC_DIR),
                    help=f"Recording directory to scan "
                         f"(default {DEFAULT_REC_DIR})")
    ap.add_argument("--json", action="store_true",
                    help="Machine-readable output")
    ap.add_argument("--short-only", action="store_true",
                    help="Show only files flagged as STUB / SHORT / EMPTY")
    ap.add_argument("--no-ffprobe", action="store_true",
                    help="Skip the duration probe (faster, but stubs only "
                         "get classified by size)")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    rec_dir = Path(os.path.expanduser(args.dir))
    if not rec_dir.exists():
        print(f"[inventory] directory not found: {rec_dir}", file=sys.stderr)
        return 1

    ffprobe = None if args.no_ffprobe else find_ffprobe()
    if not args.no_ffprobe and not ffprobe:
        print("[inventory] ffprobe not found on PATH or at "
              "C:\\ffmpeg\\bin\\ffprobe.exe; durations will be blank. "
              "(pass --no-ffprobe to silence this warning)", file=sys.stderr)

    entries = collect(rec_dir, ffprobe)
    if args.short_only:
        entries = [e for e in entries
                   if e.note not in ("OK",) and
                   not e.note.startswith("no-probe")]

    if args.json:
        print(json.dumps([asdict(e) for e in entries], indent=2))
        return 0

    print(render_table(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
