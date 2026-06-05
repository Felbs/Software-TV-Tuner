"""Interactive browser / manager for the recordings directory.

Closes the set-top-box loop: after scheduling and recording with
stvt_schedule.py + stvt_multirec.py, this lets you browse the result,
play files in VLC, drop stubs, and find duplicates without leaving the
terminal.

Multirec timestamps EVERY file it writes during one chain spawn with
the same YYYYMMDD_HHMMSS suffix. That gives us a natural "session"
unit: the FULL mux file plus one per-program demux per channel all
share a suffix. This tool groups by that suffix so the user sees what
they actually recorded, not 200 anonymous .ts files.

Commands at the prompt:

    list / <enter>          show sessions (grouped by suffix)
    s <#>                   drill into session #'s file list
    p <#>                   play file # in VLC (detached)
    pmux <#>                play session #'s FULL mux file
                            (VLC's Playback>Program selector lets you
                             flip subchannels live)
    del <#>                 delete file # (confirm)
    dels <#>                delete entire session # (confirm)
    clean                   sweep STUB/SHORT/EMPTY files across all
                            sessions, with confirmation
    dups                    find duplicate recordings (calls stvt_dedup)
    q                       quit

Defaults to scanning tools/recordings/. Pass --dir to point elsewhere.

Reuses stvt_inventory's filename parser + STUB/SHORT/EMPTY classifier
so a file flagged in `inventory` is flagged here too — single source of
truth for what counts as junk.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_REC_DIR = REPO / "tools" / "recordings"

sys.path.insert(0, str(HERE))
# Reuse the inventory tool's parser + STUB classifier so we don't drift.
import stvt_inventory as inv   # noqa: E402

DEDUP_PY = HERE / "stvt_dedup.py"
PYTHON_EXE = sys.executable

# Multirec's timestamp suffix: _YYYYMMDD_HHMMSS.ts
SESSION_SUFFIX_RE = re.compile(r"_(?P<stamp>\d{8}_\d{6})\.ts$")


# ── data shapes ──────────────────────────────────────────────────────


@dataclass
class FileInfo:
    path:        Path
    name:        str
    size_mb:     float
    mtime:       float
    duration_s:  float | None
    kind:        str        # "full" | "program" | "remote" | "unknown"
    virtual:     str | None
    short_name:  str | None
    note:        str        # OK / STUB(..) / SHORT(..) / EMPTY (...) / etc.

    @property
    def is_junk(self) -> bool:
        """STUB / SHORT / EMPTY = sweep candidate. no-probe and OK are
        kept."""
        if self.note == "OK":
            return False
        if self.note.startswith("no-probe"):
            return False
        return True


@dataclass
class Session:
    stamp:    str            # "YYYYMMDD_HHMMSS" (multirec's suffix)
    rf:       int | None     # parsed from the FULL file if present
    files:    list[FileInfo]

    @property
    def started_at(self) -> str:
        try:
            dt = datetime.strptime(self.stamp, "%Y%m%d_%H%M%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return self.stamp

    @property
    def total_size_mb(self) -> float:
        return sum(f.size_mb for f in self.files)

    @property
    def has_full(self) -> bool:
        return any(f.kind == "full" for f in self.files)

    @property
    def full_file(self) -> FileInfo | None:
        for f in self.files:
            if f.kind == "full":
                return f
        return None

    @property
    def programs(self) -> list[str]:
        """Suspected channel names parsed from per-program filenames."""
        out = []
        for f in self.files:
            if f.kind == "program" and f.short_name:
                tag = (f"{f.virtual} {f.short_name}" if f.virtual
                       else f.short_name)
                out.append(tag)
        return out


# ── scanning ─────────────────────────────────────────────────────────


def session_stamp(name: str) -> str | None:
    """Return the YYYYMMDD_HHMMSS suffix of a recording filename, or
    None if the filename doesn't match the multirec pattern."""
    m = SESSION_SUFFIX_RE.search(name)
    return m.group("stamp") if m else None


def cluster_into_sessions(files: list[FileInfo]) -> list[Session]:
    """Group files by their multirec timestamp suffix. Files that don't
    match the pattern (e.g. hand-named imports) each become a
    one-file session keyed by their own name."""
    groups: dict[str, list[FileInfo]] = defaultdict(list)
    for f in files:
        stamp = session_stamp(f.name)
        if stamp is None:
            # Lone file — make it its own pseudo-session
            stamp = f"adhoc:{f.name}"
        groups[stamp].append(f)
    sessions: list[Session] = []
    for stamp, flist in groups.items():
        # The FULL file (if any) carries the RF in its name; pull that
        # out for the session header.
        rf = None
        for f in flist:
            if f.kind == "full":
                meta = inv.parse_filename(f.name)
                rf = meta.get("rf")
                break
        sessions.append(Session(stamp=stamp, rf=rf, files=flist))
    # Newest session first (so the most-recent recording is row 1)
    sessions.sort(key=lambda s: s.stamp, reverse=True)
    # Within a session, FULL first then by name for stable display
    for s in sessions:
        s.files.sort(key=lambda f: (0 if f.kind == "full" else 1, f.name))
    return sessions


def scan_dir(rec_dir: Path, ffprobe: str | None,
             quiet: bool = False) -> list[FileInfo]:
    """Walk rec_dir for .ts files, building a FileInfo for each. Skips
    files we can't stat (mid-delete by multirec, for instance)."""
    if not rec_dir.exists():
        return []
    out: list[FileInfo] = []
    paths = sorted(rec_dir.glob("*.ts"))
    if not quiet:
        print(f"[recs] probing {len(paths)} file(s) in {rec_dir} ...",
              file=sys.stderr, flush=True)
    for path in paths:
        try:
            st = path.stat()
        except OSError as exc:
            print(f"[recs] cannot stat {path.name}: {exc}", file=sys.stderr)
            continue
        size_mb = st.st_size / 1_000_000
        meta = inv.parse_filename(path.name)
        # Skip the probe on huge files when there's no ffprobe — saves
        # the user 10s of timeout per file on a stripped-down install.
        duration = inv.probe_duration(ffprobe, path) if ffprobe else None
        out.append(FileInfo(
            path=path,
            name=path.name,
            size_mb=size_mb,
            mtime=st.st_mtime,
            duration_s=duration,
            kind=meta["kind"],
            virtual=meta["virtual"],
            short_name=meta["short"],
            note=inv.classify(size_mb, duration),
        ))
    return out


# ── rendering ────────────────────────────────────────────────────────


def fmt_size(mb: float) -> str:
    if mb >= 1024:
        return f"{mb/1024:.2f} GB"
    return f"{mb:.0f} MB"


def fmt_dur(s: float | None) -> str:
    if s is None:
        return "  ?"
    s_int = int(s)
    return f"{s_int//60:>3}m{s_int%60:02d}s"


def render_session_list(sessions: list[Session]) -> str:
    if not sessions:
        return "(no recordings found)"
    lines = []
    header = (f"{'#':>3}  {'when':<19}  {'RF':>3}  "
              f"{'files':>5}  {'size':>10}  {'FULL':<5}  programs")
    lines.append(header)
    lines.append("-" * len(header))
    for i, s in enumerate(sessions, 1):
        rf_str = str(s.rf) if s.rf is not None else "?"
        full_str = "yes" if s.has_full else "no"
        progs = ", ".join(s.programs[:4])
        if len(s.programs) > 4:
            progs += f", +{len(s.programs) - 4} more"
        if not progs:
            progs = "(no per-program files)"
        if len(progs) > 60:
            progs = progs[:57] + "..."
        lines.append(f"{i:>3}  {s.started_at:<19}  {rf_str:>3}  "
                     f"{len(s.files):>5}  {fmt_size(s.total_size_mb):>10}  "
                     f"{full_str:<5}  {progs}")
    return "\n".join(lines)


def render_session_files(s: Session) -> str:
    lines = [f"Session {s.started_at}  RF{s.rf if s.rf is not None else '?'}  "
             f"{len(s.files)} file(s)  {fmt_size(s.total_size_mb)} total",
             ""]
    header = (f"{'#':>3}  {'kind':<8}  {'size':>10}  {'len':>8}  "
              f"{'virt':<6}  {'callsign':<10}  {'note':<14}  file")
    lines.append(header)
    lines.append("-" * len(header))
    for i, f in enumerate(s.files, 1):
        virt = f.virtual or ""
        short = (f.short_name or "")[:10]
        name = f.name
        if len(name) > 50:
            name = name[:47] + "..."
        lines.append(f"{i:>3}  {f.kind:<8}  {fmt_size(f.size_mb):>10}  "
                     f"{fmt_dur(f.duration_s):>8}  {virt:<6}  {short:<10}  "
                     f"{f.note:<14}  {name}")
    return "\n".join(lines)


# ── actions ──────────────────────────────────────────────────────────


def find_vlc() -> str | None:
    """Locate vlc.exe so we can play files. Mirrors stvt_schedule's
    lookup so users don't get inconsistent VLC paths between tools."""
    if sys.platform == "win32":
        for cand in (r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                     r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"):
            if Path(cand).exists():
                return cand
        return None
    import shutil
    return shutil.which("vlc")


def spawn_vlc(path: Path, title: str, program: int | None = None) -> str:
    """Open a .ts file in VLC, detached so we keep our prompt. Returns a
    one-line status message for the UI."""
    vlc = find_vlc()
    if not vlc:
        return ("VLC not found at C:\\Program Files\\VideoLAN\\VLC\\vlc.exe "
                "(install or edit find_vlc())")
    cmd = [vlc, "--no-video-title-show", "--meta-title", title,
           "--file-caching", "1000"]
    if program is not None:
        cmd.append(f"--programs={program}")
    cmd.append(str(path))
    creationflags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        creationflags = 0x00000008 | 0x00000200 | 0x08000000
    try:
        subprocess.Popen(cmd,
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL,
                          creationflags=creationflags)
    except OSError as exc:
        return f"VLC spawn failed: {exc}"
    return f"VLC opened on {path.name}"


def confirm(prompt: str) -> bool:
    """Yes/no confirm. Empty / N / anything-but-Y = no."""
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


def delete_file(f: FileInfo) -> bool:
    try:
        f.path.unlink()
        return True
    except OSError as exc:
        print(f"[recs] failed to delete {f.name}: {exc}", file=sys.stderr)
        return False


def delete_session(s: Session) -> int:
    deleted = 0
    for f in s.files:
        if delete_file(f):
            deleted += 1
    return deleted


def find_junk(sessions: list[Session]) -> list[FileInfo]:
    """STUB / SHORT / EMPTY files across all sessions."""
    out: list[FileInfo] = []
    for s in sessions:
        for f in s.files:
            if f.is_junk:
                out.append(f)
    return out


def run_dedup(rec_dir: Path, delete: bool) -> int:
    """Shell out to stvt_dedup.py — keeps its argparse semantics intact
    so any future flags it adds are accessible here too."""
    cmd = [PYTHON_EXE, "-u", str(DEDUP_PY), "--dir", str(rec_dir)]
    if delete:
        cmd.append("--delete-extras")
    try:
        return subprocess.call(cmd)
    except OSError as exc:
        print(f"[recs] dedup launch failed: {exc}", file=sys.stderr)
        return 1


# ── interactive loop ─────────────────────────────────────────────────


def interactive(rec_dir: Path, ffprobe: str | None) -> int:
    files = scan_dir(rec_dir, ffprobe)
    sessions = cluster_into_sessions(files)
    drilled: Session | None = None

    while True:
        if drilled is None:
            print()
            print("=" * 70)
            print(f"  STVT RECORDINGS   {rec_dir}")
            print("=" * 70)
            print(render_session_list(sessions))
        else:
            print()
            print("=" * 70)
            print(render_session_files(drilled))

        print()
        if drilled is None:
            print("Commands: s <#>   drill into session     "
                  "pmux <#>  play full mux")
            print("          dels <#> delete session        "
                  "clean     sweep stubs")
            print("          dups   find duplicates         "
                  "list      refresh")
            print("          q      quit")
        else:
            print("Commands: p <#>   play file              "
                  "del <#>   delete file")
            print("          list   back to sessions        "
                  "q         quit")

        try:
            line = input("\nrecs> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("q", "quit", "exit"):
            return 0
        if cmd == "list":
            drilled = None
            # Refresh on going back so newly written files appear
            files = scan_dir(rec_dir, ffprobe)
            sessions = cluster_into_sessions(files)
            continue
        if cmd == "refresh":
            files = scan_dir(rec_dir, ffprobe)
            sessions = cluster_into_sessions(files)
            drilled = None
            continue

        if cmd == "dups":
            run_dedup(rec_dir, delete=False)
            if confirm("delete duplicate extras now?"):
                run_dedup(rec_dir, delete=True)
                files = scan_dir(rec_dir, ffprobe)
                sessions = cluster_into_sessions(files)
            input("\n[enter] to continue")
            continue

        if cmd == "clean":
            junk = find_junk(sessions)
            if not junk:
                print("(no STUB/SHORT/EMPTY files found)")
                input("\n[enter] to continue")
                continue
            print(f"\n{len(junk)} junk file(s):")
            for f in junk:
                print(f"  [{f.note}] {fmt_size(f.size_mb):>10}  {f.name}")
            if confirm(f"delete all {len(junk)} files?"):
                n_del = 0
                for f in junk:
                    if delete_file(f):
                        n_del += 1
                print(f"deleted {n_del} of {len(junk)}.")
                files = scan_dir(rec_dir, ffprobe)
                sessions = cluster_into_sessions(files)
            input("\n[enter] to continue")
            continue

        # The numbered commands below need an integer arg
        try:
            idx = int(arg) - 1
        except ValueError:
            print(f"unknown or malformed: {line!r}")
            continue

        if drilled is None:
            # Session-scoped commands
            if not (0 <= idx < len(sessions)):
                print(f"no session #{idx+1}")
                continue
            sess = sessions[idx]
            if cmd == "s":
                drilled = sess
                continue
            if cmd == "pmux":
                full = sess.full_file
                if not full:
                    print("(this session has no FULL mux file)")
                    input("\n[enter] to continue")
                    continue
                print(spawn_vlc(full.path,
                                  f"STVT mux {sess.started_at}"))
                input("\n[enter] to continue")
                continue
            if cmd == "dels":
                print(f"about to delete session {sess.started_at} "
                      f"({len(sess.files)} file(s), "
                      f"{fmt_size(sess.total_size_mb)})")
                if confirm("proceed?"):
                    n = delete_session(sess)
                    print(f"deleted {n} file(s).")
                    files = scan_dir(rec_dir, ffprobe)
                    sessions = cluster_into_sessions(files)
                input("\n[enter] to continue")
                continue
            print(f"unknown command {cmd!r} in session list")
            continue

        # drilled is not None — file-scoped commands
        if not (0 <= idx < len(drilled.files)):
            print(f"no file #{idx+1}")
            continue
        f = drilled.files[idx]
        if cmd == "p":
            print(spawn_vlc(f.path, f"STVT play {f.name}"))
            input("\n[enter] to continue")
            continue
        if cmd == "del":
            print(f"about to delete {f.name} ({fmt_size(f.size_mb)})")
            if confirm("proceed?"):
                if delete_file(f):
                    print("deleted.")
                    files = scan_dir(rec_dir, ffprobe)
                    sessions = cluster_into_sessions(files)
                    # If our drilled session is gone, drop out
                    if not any(s.stamp == drilled.stamp for s in sessions):
                        drilled = None
                    else:
                        drilled = next(s for s in sessions
                                        if s.stamp == drilled.stamp)
            input("\n[enter] to continue")
            continue
        print(f"unknown command {cmd!r} in file view")


# ── CLI entry ────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dir", default=str(DEFAULT_REC_DIR),
                    help=f"Recording directory to browse "
                         f"(default {DEFAULT_REC_DIR})")
    ap.add_argument("--no-ffprobe", action="store_true",
                    help="Skip ffprobe duration probe (faster startup, "
                         "but only size-based classification)")
    ap.add_argument("--list", action="store_true",
                    help="Print the session table once and exit "
                         "(non-interactive mode)")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    rec_dir = Path(os.path.expanduser(args.dir))
    if not rec_dir.exists():
        print(f"[recs] directory not found: {rec_dir}", file=sys.stderr)
        return 1

    ffprobe = None if args.no_ffprobe else inv.find_ffprobe()
    if not args.no_ffprobe and not ffprobe:
        print("[recs] ffprobe not found — durations will be blank. "
              "(pass --no-ffprobe to silence)", file=sys.stderr)

    if args.list:
        files = scan_dir(rec_dir, ffprobe, quiet=True)
        sessions = cluster_into_sessions(files)
        print(render_session_list(sessions))
        return 0

    return interactive(rec_dir, ffprobe)


if __name__ == "__main__":
    sys.exit(main())
