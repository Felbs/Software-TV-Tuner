"""Find duplicate recording files under tools/recordings/.

Multirec timestamps each file with the chain-spawn time, not the show
start time, so if a recording was retried (chain died, daemon kicked
off a fresh attempt) you can end up with two near-identical .ts files
with different YYYYMMDD_HHMMSS suffixes. Same content, different name.

This tool finds duplicates without reading 10 GB of TS bytes:

  1. Group files by exact size in bytes (true duplicates always match
     on size; partial / restarted files usually don't).
  2. Within each size group, hash the first + middle + last 64 KB of
     each file. That's enough to distinguish two different broadcasts
     while staying cheap (192 KB read regardless of file size).
  3. Report the resulting groups, marking the oldest mtime as the
     "keeper" suggestion.

NOTHING is deleted. The tool only reports. Use --delete-extras to
actually remove the non-keepers, and only after eyeballing the output
once first.

Usage:
    python tools/stvt_dedup.py
    python tools/stvt_dedup.py --dir D:/dvr-archive
    python tools/stvt_dedup.py --full-hash       # hash entire file (slow)
    python tools/stvt_dedup.py --delete-extras   # ACTUALLY DELETE dupes
    python tools/stvt_dedup.py --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_REC_DIR = REPO / "tools" / "recordings"

SAMPLE_BYTES = 64 * 1024   # 64 KB per probe point


@dataclass
class Entry:
    path:    str
    size:    int
    mtime:   float
    hash:    str


def sample_hash(path: Path, size: int) -> str:
    """Hash three 64 KB slices of the file: start, middle, end. For TS
    files under 192 KB just hash the whole thing — they're too small
    for sampling to matter.

    Why three points instead of one: a TS recording is a stream of
    188-byte packets. Two different recordings of the same channel
    might start with the same PAT/PMT but diverge at the first video
    packet. Sampling the middle and end catches that without reading
    the entire 1 GB file."""
    h = hashlib.sha1(usedforsecurity=False)
    try:
        with open(path, "rb") as f:
            if size <= 3 * SAMPLE_BYTES:
                # Tiny file — hash everything
                h.update(f.read())
            else:
                h.update(f.read(SAMPLE_BYTES))
                f.seek(size // 2)
                h.update(f.read(SAMPLE_BYTES))
                f.seek(-SAMPLE_BYTES, os.SEEK_END)
                h.update(f.read(SAMPLE_BYTES))
    except OSError as exc:
        # Hash the error message so identical errors collide, while
        # distinct paths don't accidentally match.
        return f"ERR:{exc.errno}:{path.name}"
    return h.hexdigest()


def full_hash(path: Path) -> str:
    """Hash the entire file. Slow on multi-GB recordings — only use
    when you want absolute certainty."""
    h = hashlib.sha1(usedforsecurity=False)
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError as exc:
        return f"ERR:{exc.errno}:{path.name}"
    return h.hexdigest()


def collect(rec_dir: Path, full: bool) -> list[Entry]:
    entries: list[Entry] = []
    for path in sorted(rec_dir.glob("*.ts")):
        try:
            st = path.stat()
        except OSError as exc:
            print(f"[dedup] cannot stat {path.name}: {exc}",
                  file=sys.stderr)
            continue
        if st.st_size == 0:
            # Zero-byte files trivially match each other but aren't
            # actually duplicates of any real recording. Skip.
            continue
        entries.append(Entry(
            path=str(path),
            size=st.st_size,
            mtime=st.st_mtime,
            hash="",   # filled in later
        ))
    return entries


def group_by_size(entries: list[Entry]) -> dict[int, list[Entry]]:
    out: dict[int, list[Entry]] = defaultdict(list)
    for e in entries:
        out[e.size].append(e)
    # Only sizes with >=2 candidates are interesting
    return {sz: lst for sz, lst in out.items() if len(lst) >= 2}


def fingerprint_groups(size_groups: dict[int, list[Entry]],
                       full: bool) -> list[list[Entry]]:
    """Within each size bucket, hash each candidate, then group by
    hash. Returns a flat list of duplicate groups (each with >=2
    entries)."""
    results: list[list[Entry]] = []
    hasher = full_hash if full else lambda p: sample_hash(p, Path(p).stat().st_size)
    for _, candidates in size_groups.items():
        # Fingerprint each candidate
        for e in candidates:
            try:
                if full:
                    e.hash = hasher(Path(e.path))
                else:
                    e.hash = sample_hash(Path(e.path), e.size)
            except OSError as exc:
                e.hash = f"ERR:{exc.errno}"
        by_hash: dict[str, list[Entry]] = defaultdict(list)
        for e in candidates:
            by_hash[e.hash].append(e)
        for grp in by_hash.values():
            if len(grp) >= 2:
                results.append(grp)
    return results


def render(groups: list[list[Entry]]) -> str:
    if not groups:
        return "[dedup] no duplicates found."
    lines = [f"[dedup] {len(groups)} duplicate group(s) found:"]
    total_redundant_bytes = 0
    for i, grp in enumerate(groups, 1):
        grp_sorted = sorted(grp, key=lambda e: e.mtime)
        keeper = grp_sorted[0]
        extras = grp_sorted[1:]
        bytes_in_extras = sum(e.size for e in extras)
        total_redundant_bytes += bytes_in_extras
        lines.append("")
        lines.append(f"  group {i}  ({len(grp)} files, "
                     f"{grp[0].size/1e6:.1f} MB each, "
                     f"hash {grp[0].hash[:12]}...)")
        for e in grp_sorted:
            label = "KEEP " if e is keeper else "extra"
            mtime = datetime.fromtimestamp(e.mtime).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"    [{label}] {mtime}  {Path(e.path).name}")
    lines.append("")
    lines.append(f"[dedup] total redundant space: "
                 f"{total_redundant_bytes/1024/1024/1024:.2f} GB "
                 f"({total_redundant_bytes/1e6:.0f} MB)")
    lines.append("[dedup] re-run with --delete-extras to remove the "
                 "non-keeper files.")
    return "\n".join(lines)


def delete_extras(groups: list[list[Entry]], dry: bool) -> int:
    """Remove all non-keeper files. Returns number actually deleted.
    The "keeper" is the file with the oldest mtime in each group."""
    deleted = 0
    for grp in groups:
        keeper, *extras = sorted(grp, key=lambda e: e.mtime)
        for e in extras:
            p = Path(e.path)
            if dry:
                print(f"[dedup] would delete: {p}")
                continue
            try:
                p.unlink()
                print(f"[dedup] deleted: {p}")
                deleted += 1
            except OSError as exc:
                print(f"[dedup] failed to delete {p}: {exc}",
                      file=sys.stderr)
    return deleted


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dir", default=str(DEFAULT_REC_DIR),
                    help=f"Recording directory to scan "
                         f"(default {DEFAULT_REC_DIR})")
    ap.add_argument("--full-hash", action="store_true",
                    help="Hash entire file instead of 3x 64 KB samples. "
                         "Slow on multi-GB recordings; use when you want "
                         "byte-for-byte certainty.")
    ap.add_argument("--delete-extras", action="store_true",
                    help="ACTUALLY delete the non-keeper files in each "
                         "duplicate group. Make sure you've reviewed the "
                         "report first.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Pair with --delete-extras to print what would "
                         "be deleted without removing anything.")
    ap.add_argument("--json", action="store_true",
                    help="Machine-readable output")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    rec_dir = Path(os.path.expanduser(args.dir))
    if not rec_dir.exists():
        print(f"[dedup] directory not found: {rec_dir}", file=sys.stderr)
        return 1

    print(f"[dedup] scanning {rec_dir}", file=sys.stderr)
    entries = collect(rec_dir, args.full_hash)
    print(f"[dedup] {len(entries)} non-empty .ts files", file=sys.stderr)
    size_groups = group_by_size(entries)
    print(f"[dedup] {len(size_groups)} size bucket(s) with >=2 files; "
          f"hashing...", file=sys.stderr)
    groups = fingerprint_groups(size_groups, args.full_hash)

    if args.json:
        out = []
        for grp in groups:
            grp_sorted = sorted(grp, key=lambda e: e.mtime)
            out.append({
                "size":   grp_sorted[0].size,
                "hash":   grp_sorted[0].hash,
                "keeper": grp_sorted[0].path,
                "extras": [e.path for e in grp_sorted[1:]],
            })
        print(json.dumps(out, indent=2))
        return 0

    print(render(groups))

    if args.delete_extras:
        print()
        n = delete_extras(groups, dry=args.dry_run)
        if args.dry_run:
            print(f"[dedup] dry-run: would have deleted "
                  f"{sum(len(g)-1 for g in groups)} file(s)")
        else:
            print(f"[dedup] deleted {n} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
