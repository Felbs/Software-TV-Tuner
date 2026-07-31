"""DVR scheduler for STVT.

Pick shows from the EPG grid (stvt_epg.py), schedule them to record,
then run the daemon to fire stvt_multirec.py at each show's start time.

Queue is persisted to ~/.tv_tuner/schedule.json so a daemon restart
keeps pending recordings.

Commands:

    stvt_schedule.py tv                                # INTERACTIVE controller (recommended)
    stvt_schedule.py list                              # what's queued
    stvt_schedule.py add-show <virt> "<title>"         # find next match in EIT, schedule
    stvt_schedule.py add --rf N --program P \
        --start "2026-06-04 23:00" --duration 60       # manual schedule
    stvt_schedule.py remove <id>                       # cancel a recording
    stvt_schedule.py run                               # daemon (fires recordings)

Example flow:

    python tools/tv_tuner.py --scan          # fresh EIT
    python tools/stvt_epg.py                 # look at the grid
    python tools/stvt_schedule.py add-show 5.1 "Fox 5 News"
    python tools/stvt_schedule.py add-show 4.1 "Dateline"
    python tools/stvt_schedule.py list
    python tools/stvt_schedule.py run        # leave running

One SDR constraint: two scheduled recordings on different RF muxes
that overlap in time CANNOT both record (one tuner, one tuning at a
time). `list` and `add-show` warn about conflicts but don't block them
— the daemon will record whichever started first and skip the rest.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HERE = Path(__file__).resolve().parent
PYTHON_EXE = sys.executable
MULTIREC_PY = HERE / "stvt_multirec.py"
QUEUE_PATH = Path(os.path.expanduser("~")) / ".tv_tuner" / "schedule.json"
SCAN_PATH = Path(os.path.expanduser("~")) / ".tv_tuner" / "scan.json"

GPS_OFFSET = 315_964_800
LEAP = 18

# Grace period past a recording's end before its window counts as blown.
# Shared by the scheduler daemon and by expire_stale() so the daemon and the
# UI can never disagree about what "missed" means.
MISS_GRACE_SEC = 120


def gps_to_unix(g: int) -> int:
    return int(g + GPS_OFFSET - LEAP)


def load_queue() -> list[dict]:
    if not QUEUE_PATH.exists():
        return []
    try:
        return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def save_queue(queue: list[dict]):
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")


def expire_stale(queue: list[dict]) -> int:
    """Mark pending entries whose window has passed as 'missed'; persist if any
    changed. Returns how many were expired.

    Found in real use 2026-07-29: a recording scheduled for 07/03 was still
    listed as 'pending' 27 days later. The daemon expires blown windows, but
    ONLY while it is running (see cmd_daemon) — and the DVR controller is
    perfectly usable without ever starting a daemon, so a queue touched only by
    the UI never expired anything. Expiring on read makes the rule hold
    regardless of who is running.
    """
    now = int(time.time())
    n = 0
    for q in queue:
        # 'recording'/'active' are included deliberately: if the daemon dies
        # mid-recording the entry is left in that state forever and shows up in
        # the UI as live work that will never finish. A genuinely in-progress
        # recording has end_unix in the FUTURE, so the grace check below can
        # never touch it.
        if q.get("status") not in ("pending", "recording", "active"):
            continue
        if q.get("end_unix", 0) + MISS_GRACE_SEC < now:
            q["status"] = "missed"
            n += 1
    if n:
        save_queue(queue)
    return n


def load_scan() -> dict:
    if not SCAN_PATH.exists():
        return {}
    try:
        return json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def find_channel(scan: dict, virtual: str) -> tuple[dict | None, dict | None]:
    """Resolve "5.1" -> (channel_dict, psip_channel_dict). Returns (None,
    None) if no match."""
    for c in scan.get("channels", []):
        psip = c.get("psip") or {}
        for p in psip.get("channels") or []:
            major = p.get("major")
            minor = p.get("minor", 1)
            virt = f"{major}.{minor}" if major else None
            if virt == virtual:
                return c, p
    return None, None


def find_events_for_program(scan: dict, virtual: str,
                            title_substring: str | None = None) -> list[dict]:
    """Return upcoming EIT events for the given virtual channel,
    optionally filtered by title substring (case-insensitive)."""
    chan, psip_ch = find_channel(scan, virtual)
    if not chan or not psip_ch:
        return []
    pnum = psip_ch.get("program_number")
    if not pnum:
        return []
    psip = chan.get("psip") or {}
    raw_events = (psip.get("events") or {}).get(str(pnum), [])
    now = int(time.time())
    out = []
    needle = title_substring.lower() if title_substring else None
    for e in raw_events:
        start = gps_to_unix(e.get("start_gps", 0))
        length = e.get("length_sec") or 0
        if start + length < now:
            continue  # already ended
        title = e.get("title") or "(untitled)"
        if needle and needle not in title.lower():
            continue
        out.append({
            "rf":         chan["rf"],
            "program":    pnum,
            "virtual":    virtual,
            "callsign":   psip_ch.get("short_name") or chan.get("callsign"),
            "title":      title,
            "start_unix": start,
            "end_unix":   start + length,
            "length_sec": length,
        })
    out.sort(key=lambda x: x["start_unix"])
    return out


def make_id(rf: int, program: int, start_unix: int, title: str) -> str:
    """Stable ID for a scheduled recording."""
    slug = "".join(c if c.isalnum() else "_" for c in title.lower())[:24]
    ts = datetime.fromtimestamp(start_unix).strftime("%Y%m%d_%H%M")
    return f"{ts}_rf{rf}_p{program}_{slug}"


def add_to_queue(entry: dict) -> str:
    queue = load_queue()
    if any(q["id"] == entry["id"] for q in queue):
        return "duplicate"
    queue.append(entry)
    queue.sort(key=lambda x: x["start_unix"])
    save_queue(queue)
    return "added"


def detect_conflicts(queue: list[dict], entry: dict) -> list[dict]:
    """Return queue entries that overlap entry's time window AND sit on
    a different RF (one SDR can only tune one mux at a time)."""
    conflicts = []
    for q in queue:
        if q.get("status") in ("done", "skipped"):
            continue
        if q["id"] == entry["id"]:
            continue
        if q["rf"] == entry["rf"]:
            continue  # same mux is fine (multirec handles all programs)
        overlap = (q["start_unix"] < entry["end_unix"] and
                   q["end_unix"] > entry["start_unix"])
        if overlap:
            conflicts.append(q)
    return conflicts


def fmt_time(unix_t: int) -> str:
    return datetime.fromtimestamp(unix_t).strftime("%a %m/%d %I:%M %p").lstrip("0")


# ── Commands ─────────────────────────────────────────────────────────

def cmd_list(args) -> int:
    queue = load_queue()
    if not queue:
        print("[schedule] queue is empty")
        return 0
    expire_stale(queue)
    now = int(time.time())
    print(f"[schedule] {len(queue)} entries:")
    print()
    print(f"{'id':<48} {'virt':<6} {'when':<22} {'len':>4} {'title':<28} {'status':<10}")
    print("-" * 130)
    for q in queue:
        status = q.get("status", "pending")
        if status == "pending" and q["start_unix"] < now < q["end_unix"]:
            status = "active*"
        elif status == "pending" and q["end_unix"] < now:
            status = "missed"
        when = fmt_time(q["start_unix"])
        minutes = q["length_sec"] // 60
        print(f"{q['id']:<48} {q['virtual']:<6} {when:<22} "
              f"{minutes:>4} {q['title'][:28]:<28} {status:<10}")
    return 0


def cmd_add_show(args) -> int:
    scan = load_scan()
    if not scan:
        print("[schedule] no scan.json — run `tv_tuner.py --scan` first",
              file=sys.stderr)
        return 1
    matches = find_events_for_program(scan, args.virtual, args.title)
    if not matches:
        print(f"[schedule] no upcoming events match "
              f"{args.virtual!r} '{args.title}'", file=sys.stderr)
        # Hint: show what IS on this channel
        any_upcoming = find_events_for_program(scan, args.virtual)
        if any_upcoming:
            print(f"[schedule] upcoming on {args.virtual}:", file=sys.stderr)
            for e in any_upcoming[:6]:
                print(f"           {fmt_time(e['start_unix'])}  "
                      f"{e['title']}", file=sys.stderr)
        return 1
    target = matches[0]  # next instance
    # Adaptive-era tuneability guard: the guide can list stations whose EIT
    # was cross-carried by another broadcaster but whose own mux this antenna
    # has never locked — recording those tapes static.
    chan, _ = find_channel(scan, args.virtual)
    if chan is not None and not chan.get("lock"):
        print(f"[schedule] WARNING: {args.virtual} rides RF{chan.get('rf')} "
              f"which did NOT lock in the last scan — this antenna likely "
              f"cannot decode it and the recording would be empty. "
              f"Scheduling anyway (re-scan or change antennas to fix).",
              file=sys.stderr)
    result, entry, conflicts = schedule_event(target, mux=False)
    if result == "duplicate":
        print(f"[schedule] already scheduled: {entry['id']}")
        return 0
    print(f"[schedule] added: {entry['id']}")
    print(f"           {entry['virtual']} {entry['callsign']} "
          f"\"{entry['title']}\"")
    print(f"           start {fmt_time(entry['start_unix'])}  "
          f"({entry['length_sec'] // 60} min)")
    if conflicts:
        print(f"\n[schedule] WARNING: {len(conflicts)} mux-conflict(s) — "
              f"one SDR can't tune two RFs at once:")
        for c in conflicts:
            print(f"           RF{c['rf']} {c['virtual']} "
                  f"\"{c['title']}\" at {fmt_time(c['start_unix'])}")
        print("           The earlier-starting entry will win; others "
              "get skipped.")
    return 0


def cmd_add(args) -> int:
    try:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d %H:%M")
    except ValueError:
        print("[schedule] --start must be 'YYYY-MM-DD HH:MM' "
              "(24-hour, local time)", file=sys.stderr)
        return 1
    start_unix = int(start_dt.timestamp())
    end_unix = start_unix + int(args.duration * 60)
    scan = load_scan()
    callsign = "?"
    virtual = f"{args.rf}.{args.program}"
    for c in scan.get("channels", []):
        if c.get("rf") != args.rf:
            continue
        callsign = c.get("callsign") or callsign
        psip = c.get("psip") or {}
        for p in psip.get("channels") or []:
            if p.get("program_number") == args.program:
                major = p.get("major")
                minor = p.get("minor", 1)
                if major:
                    virtual = f"{major}.{minor}"
                callsign = p.get("short_name") or callsign
                break
    title = args.title or f"Manual RF{args.rf} p{args.program}"
    entry = {
        "id":         make_id(args.rf, args.program, start_unix, title),
        "rf":         args.rf,
        "program":    args.program,
        "virtual":    virtual,
        "callsign":   callsign,
        "title":      title,
        "start_unix": start_unix,
        "end_unix":   end_unix,
        "length_sec": end_unix - start_unix,
        "status":     "pending",
    }
    queue = load_queue()
    conflicts = detect_conflicts(queue, entry)
    result = add_to_queue(entry)
    if result == "duplicate":
        print(f"[schedule] already scheduled: {entry['id']}")
        return 0
    print(f"[schedule] added: {entry['id']}")
    if conflicts:
        print(f"[schedule] WARNING: {len(conflicts)} mux-conflict(s)")
    return 0


def schedule_event(event: dict, mux: bool = False) -> tuple[str, dict, list[dict]]:
    """Queue an event dict (from find_events_for_program). Returns
    (status, entry, conflicts). status is "added" or "duplicate"."""
    title = event["title"]
    if mux:
        title = f"[MUX] {title}"
    entry = {
        "id":         make_id(event["rf"], event["program"],
                              event["start_unix"], title),
        "rf":         event["rf"],
        "program":    event["program"],
        "virtual":    event["virtual"],
        "callsign":   event["callsign"],
        "title":      title,
        "start_unix": event["start_unix"],
        "end_unix":   event["end_unix"],
        "length_sec": event["length_sec"],
        "status":     "pending",
    }
    if mux:
        entry["mux_record"] = True
    queue = load_queue()
    conflicts = detect_conflicts(queue, entry)
    status = add_to_queue(entry)
    return status, entry, conflicts


def cmd_remove(args) -> int:
    queue = load_queue()
    before = len(queue)
    queue = [q for q in queue if q["id"] != args.id and
             not q["id"].startswith(args.id)]
    after = len(queue)
    if before == after:
        print(f"[schedule] no entry matches {args.id!r}", file=sys.stderr)
        return 1
    save_queue(queue)
    print(f"[schedule] removed {before - after} entry/entries")
    return 0


def build_channel_snapshot(scan: dict) -> list[dict]:
    """For each (virtual_channel, program) in the scan, return a row
    with 'on_now' + 'next' EIT events. Sorted by virtual channel."""
    now = int(time.time())
    rows = []
    for c in scan.get("channels", []):
        if c.get("not_detected"):
            continue
        callsign = c.get("callsign")
        if not callsign or callsign in ("?", "None"):
            continue
        psip = c.get("psip") or {}
        psip_by_pnum = {p.get("program_number"): p
                        for p in (psip.get("channels") or [])
                        if p.get("program_number")}
        for prog in c.get("programs") or []:
            pnum = prog.get("program_num") or prog.get("program_id")
            if not pnum:
                continue
            match = psip_by_pnum.get(pnum) or {}
            major = match.get("major")
            minor = match.get("minor", 1)
            virtual = f"{major}.{minor}" if major else f"{c['rf']}.{pnum}"
            short = match.get("short_name") or callsign
            events_raw = (psip.get("events") or {}).get(str(pnum), [])
            future = []
            on_now = None
            for e in events_raw:
                start = gps_to_unix(e.get("start_gps", 0))
                length = e.get("length_sec") or 0
                if start + length < now:
                    continue
                ev = {
                    "rf":         c["rf"],
                    "program":    pnum,
                    "virtual":    virtual,
                    "callsign":   short,
                    "title":      e.get("title") or "(untitled)",
                    "start_unix": start,
                    "end_unix":   start + length,
                    "length_sec": length,
                }
                if start <= now < start + length and on_now is None:
                    on_now = ev
                else:
                    future.append(ev)
            future.sort(key=lambda x: x["start_unix"])
            rows.append({
                "rf":       c["rf"],
                "program":  pnum,
                "virtual":  virtual,
                "callsign": short,
                "on_now":   on_now,
                "next":     future[0] if future else None,
            })
    rows.sort(key=lambda r: (int(r["virtual"].split(".")[0]),
                              int(r["virtual"].split(".")[1])))
    return rows


def _mux_summary(rf: int, rows: list[dict]) -> tuple[str, str]:
    """Build a human-friendly mux header. Returns (title, sublabel).
    Title: 'RF 34 — NBC mux (4.x + 44.x + 4.3 + 4.4)'
    Sublabel: '6 channels — record all at once: rrf 34'

    Derives the "primary network" from the major-virtual frequency
    so the user sees, e.g., that RF 34 carries NBC's 4.x sub-channels
    plus Telemundo's 44.x — even though scan.json's per-RF callsign
    field only names one."""
    if not rows:
        return f"RF {rf}", "(no channels)"
    majors: dict[str, list[str]] = {}
    for r in rows:
        major = r["virtual"].split(".")[0]
        majors.setdefault(major, []).append(r["callsign"])
    # Pick the largest major group as the "primary"
    primary_major = max(majors.keys(), key=lambda m: len(majors[m]))
    primary_call = majors[primary_major][0]
    major_groups = sorted(majors.keys(), key=lambda m: int(m) if m.isdigit() else 9999)
    groups_str = " + ".join(f"{m}.x" for m in major_groups)
    title = f"RF {rf} -- {primary_call} mux ({groups_str})"
    sublabel = (f"{len(rows)} channel{'s' if len(rows) != 1 else ''} share "
                f"this broadcast -- record them all at once with: rrf {rf}")
    return title, sublabel


def render_tv_table(rows: list[dict]) -> str:
    """Render channels grouped by RF mux, with a header per section
    explaining what's in that mux and how to record the whole thing.
    Row numbers are global so `r <#>`, `rn <#>`, `rmux <#>` still work."""
    # Group by RF, keeping sorted-by-virtual order within each group
    by_rf: dict[int, list[tuple[int, dict]]] = {}
    for i, r in enumerate(rows, 1):
        by_rf.setdefault(r["rf"], []).append((i, r))

    out = []
    # Sort sections by their first virtual channel (so NBC's RF34
    # comes before Fox's RF36, etc.)
    section_order = sorted(by_rf.keys(),
                            key=lambda rf: int(by_rf[rf][0][1]["virtual"]
                                                .split(".")[0]))
    for rf in section_order:
        group = by_rf[rf]
        title, sublabel = _mux_summary(rf, [r for _, r in group])
        out.append("")
        out.append(f"+-- {title} {'-' * max(1, 92 - len(title))}")
        out.append(f"|   {sublabel}")
        # A mux with NO guide data at all repeats "(no guide data)" once per
        # sub-channel and looks like a bug in the DVR. It isn't: the scan
        # captured this mux's channel table (that's how we know the callsigns)
        # but no EIT show listings during its dwell. Say so once, here, instead
        # of leaving an unexplained column of blanks.
        if not any(r["on_now"] or r["next"] for _, r in group):
            out.append(f"|   no EPG captured for this mux -- channel names came "
                       f"through but show listings did not.")
            out.append(f"|   Re-scan with a longer dwell to give its EIT tables "
                       f"time to cycle:")
            out.append(f"|     python tools/tv_tuner.py --scan --scan-dwell 20")
            out.append(f"|   (some stations transmit sparse or no EIT at all -- "
                       f"recording still works via `rrf {rf}`)")
        out.append("|")
        for i, r in group:
            on = r["on_now"]
            nx = r["next"]
            on_str = on["title"][:30] if on else "(no guide data)"
            nx_str = ""
            if nx:
                nx_when = datetime.fromtimestamp(nx["start_unix"]).strftime("%I:%M%p").lstrip("0").lower()
                nx_str = f"{nx_when} {nx['title'][:24]}"
            out.append(f"|   {i:>3}  {r['virtual']:<5} {r['callsign']:<12} "
                       f"{on_str:<32} {nx_str}")
    return "\n".join(out)


def render_pending(queue: list[dict]) -> str:
    now = int(time.time())
    pending = [q for q in queue
               if q.get("status") in ("pending", "recording", "active")]
    if not pending:
        return "  (queue empty)"
    out = []
    for q in pending:
        status = q.get("status", "pending")
        if status == "pending" and q["start_unix"] <= now < q["end_unix"]:
            status = "active*"
        when = fmt_time(q["start_unix"])
        mux = " [MUX]" if q.get("mux_record") else ""
        out.append(f"  {q['virtual']:<5} {q['callsign']:<10} "
                   f"\"{q['title']}\"{mux}  "
                   f"{when}  ({q['length_sec']//60}min)  [{status}]")
    return "\n".join(out)


def find_vlc() -> str | None:
    """Locate vlc.exe so we can spawn a 'spy' watcher on live.ts."""
    if sys.platform == "win32":
        for cand in (r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                     r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"):
            if Path(cand).exists():
                return cand
        return None
    import shutil
    return shutil.which("vlc")


def find_live_ts() -> Path | None:
    """Look for the live.ts that an active tv_live is writing to."""
    here = Path(__file__).resolve().parent
    cand = here / "data" / "tv_live" / "live.ts"
    if cand.exists() and cand.stat().st_size > 1_000_000:
        return cand
    return None


import threading

_WATCH_VLC: subprocess.Popen | None = None
_WATCH_FFMPEG: subprocess.Popen | None = None
_WATCH_STOP_EVT: threading.Event | None = None
_WATCH_TAIL: threading.Thread | None = None
_WATCH_LABEL: str = ""

# Use the same tail-pipe pattern multirec uses to feed ffmpeg's stdin.
from stvt_multirec import tail_into_pipe  # type: ignore

# Borrow ffmpeg path from tv_tuner so we don't re-resolve.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from tv_tuner import FFMPEG  # noqa: E402


def _proc_running(p: subprocess.Popen | None) -> bool:
    return p is not None and p.poll() is None


def stop_watch() -> bool:
    global _WATCH_VLC, _WATCH_FFMPEG, _WATCH_STOP_EVT, _WATCH_TAIL, _WATCH_LABEL
    if _WATCH_STOP_EVT is not None:
        _WATCH_STOP_EVT.set()
    for label, p in (("vlc", _WATCH_VLC), ("ffmpeg", _WATCH_FFMPEG)):
        if not _proc_running(p):
            continue
        try:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
        except (OSError, ValueError):
            pass
    if _WATCH_TAIL is not None:
        _WATCH_TAIL.join(timeout=1)
    had = _WATCH_VLC is not None or _WATCH_FFMPEG is not None
    _WATCH_VLC = None
    _WATCH_FFMPEG = None
    _WATCH_STOP_EVT = None
    _WATCH_TAIL = None
    _WATCH_LABEL = ""
    return had


def start_watch(channel_row: dict) -> str:
    """Spawn ffmpeg + VLC chained via pipes:
        tail(live.ts) -> ffmpeg(-map 0:p:N -c copy) -> VLC stdin

    VLC's own --programs filter is unreliable on a growing TS (it opens
    at byte 0 and plays the first program in the PAT regardless). The
    ffmpeg demux step guarantees only program N's bytes reach VLC and
    starts from the file's current END so playback is at the live edge,
    not from whenever the recording started."""
    global _WATCH_VLC, _WATCH_FFMPEG, _WATCH_STOP_EVT, _WATCH_TAIL, _WATCH_LABEL
    vlc = find_vlc()
    if not vlc:
        return "VLC not found at C:\\Program Files\\VideoLAN\\VLC\\vlc.exe"
    live = find_live_ts()
    if not live:
        return ("no active live.ts found - is a recording in progress? "
                "(start a recording with rrf <RF> first)")
    stop_watch()
    pnum = channel_row["program"]
    title = f"STVT spy: {channel_row['virtual']} {channel_row['callsign']}"
    # Start from current end (with 500 KB lookback so ffmpeg has enough
    # to find a PMT cycle and demux cleanly without burning seconds).
    try:
        live_size = live.stat().st_size
    except OSError:
        live_size = 0
    start_offset = max(0, live_size - 500_000)

    ff_creationflags = 0x08000000 if sys.platform == "win32" else 0
    ffmpeg_proc = subprocess.Popen(
        [FFMPEG, "-hide_banner", "-loglevel", "warning",
         "-fflags", "+discardcorrupt", "-err_detect", "ignore_err",
         "-f", "mpegts", "-i", "pipe:0",
         "-map", f"0:p:{pnum}", "-c", "copy",
         "-f", "mpegts", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=ff_creationflags,
    )
    vlc_creationflags = 0
    if sys.platform == "win32":
        vlc_creationflags = 0x00000008 | 0x00000200 | 0x08000000
    vlc_proc = subprocess.Popen(
        [vlc, "--no-video-title-show", "--meta-title", title,
         "--file-caching", "1000", "-"],
        stdin=ffmpeg_proc.stdout,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=vlc_creationflags,
    )
    # Hand the read end fully to VLC so ffmpeg's stdout closes cleanly
    # when VLC exits.
    try:
        ffmpeg_proc.stdout.close()
    except (OSError, ValueError):
        pass

    stop_evt = threading.Event()
    tail_thread = threading.Thread(
        target=tail_into_pipe,
        args=(live, ffmpeg_proc.stdin, stop_evt, start_offset),
        daemon=True,
    )
    tail_thread.start()

    _WATCH_VLC = vlc_proc
    _WATCH_FFMPEG = ffmpeg_proc
    _WATCH_STOP_EVT = stop_evt
    _WATCH_TAIL = tail_thread
    _WATCH_LABEL = f"{channel_row['virtual']} {channel_row['callsign']}"
    return (f"watching {_WATCH_LABEL} (program {pnum}) at live edge - "
            f"close VLC or type `unwatch` to stop")


def cmd_tv(args) -> int:
    """Interactive DVR controller: EPG view + record-from-grid."""
    # Broadcasters send smart quotes / em-dashes; default cp1252 stdout
    # chokes on those. Reconfigure once so the whole loop is safe.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    while True:
        scan = load_scan()
        if not scan:
            print("no scan.json — run `tv_tuner.py --scan` first")
            return 1
        rows = build_channel_snapshot(scan)
        queue = load_queue()
        expire_stale(queue)
        os.system("cls" if sys.platform == "win32" else "clear")
        print("=================================================================")
        print(f"  STVT DVR    {datetime.now().strftime('%a %m/%d %I:%M %p').lstrip('0')}    "
              f"({len(rows)} channels)")
        print("=================================================================")
        print()
        print(render_tv_table(rows))
        print()
        print("PENDING RECORDINGS:")
        print(render_pending(queue))
        print()
        watching_str = (f"  [now watching: {_WATCH_LABEL}]"
                        if _proc_running(_WATCH_VLC) or _proc_running(_WATCH_FFMPEG)
                        else "")
        print(f"Commands:{watching_str}")
        print("  r   <#>          record ON-NOW of channel #")
        print("  rn  <#>          record NEXT show on channel #")
        print("  rmux <#>         record ENTIRE MUX (all programs) for")
        print("                   ON-NOW's duration (uses channel #'s mux)")
        print("  rrf  <RF>        record the WHOLE RF mux (e.g. rrf 36 to")
        print("                   grab Fox + all its sub-channels at once)")
        print("  watch <#>        open VLC on channel # using the ACTIVE")
        print("                   recording's live.ts (flip subchannels")
        print("                   live while rrf is recording)")
        print("  unwatch          close the VLC watcher")
        print("  show <#>         show next 6 events for channel #")
        print("  rm  <id-prefix>  cancel a pending recording")
        print("  recs             browse recordings folder (play/delete)")
        print("  refresh          reload scan + queue")
        print("  q                quit")
        print()
        # NOTE: the panel has a guide, waterfall and antenna picker but NO
        # recording queue — scheduling lives here in the CLI only. Don't
        # advertise DVR features it doesn't have.
        print("  Prefer clicking to typing? The web panel has a clickable guide,")
        print("  waterfall and antenna picker (recording stays here in the CLI):")
        print("      python adaptive-tv/tv_tuna_panel.py   ->  "
              "http://localhost:8642")
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "q" or cmd == "quit":
            stop_watch()
            return 0
        if cmd == "refresh":
            continue
        if cmd == "rm":
            if not arg:
                print("rm <id-prefix>"); input("[enter]"); continue
            queue = [q for q in queue if not q["id"].startswith(arg)
                     and q["id"] != arg]
            save_queue(queue)
            print("removed."); input("[enter]"); continue
        if cmd == "recs":
            # Hand control to the recordings browser. Spawning a child
            # python lets it own stdin without interfering with this
            # loop's input() state.
            recs_py = HERE / "stvt_recordings.py"
            subprocess.call([PYTHON_EXE, str(recs_py)])
            continue
        if cmd == "unwatch":
            if stop_watch():
                print("watcher stopped.")
            else:
                print("(no watcher running)")
            input("[enter]"); continue
        if cmd == "watch":
            try:
                idx = int(arg) - 1
                row = rows[idx]
            except (ValueError, IndexError):
                print(f"bad #: '{arg}'"); input("[enter]"); continue
            msg = start_watch(row)
            print(msg)
            input("[enter]"); continue

        if cmd == "rrf":
            try:
                rf = int(arg)
            except ValueError:
                print(f"bad RF: '{arg}'"); input("[enter]"); continue
            mux_rows = [r for r in rows if r["rf"] == rf and r["on_now"]]
            if not mux_rows:
                print(f"no channels on RF {rf} with on-now data")
                input("[enter]"); continue
            # Use the longest on-now event as the recording window
            best = max(mux_rows, key=lambda r: r["on_now"]["length_sec"])
            ev = best["on_now"]
            status, entry, conflicts = schedule_event(ev, mux=True)
            if status == "duplicate":
                print(f"already scheduled: {entry['id']}")
            else:
                print(f"queued: [MUX] RF{rf} -- {len(mux_rows)} channels for "
                      f"{ev['length_sec']//60} min "
                      f"(window picked from \"{ev['title']}\" on "
                      f"{ev['virtual']})")
                if conflicts:
                    print(f"WARNING: conflicts with {len(conflicts)} other "
                          f"queued recording(s) on different muxes.")
                if not is_daemon_running():
                    print("NOTE: scheduler daemon is NOT running. Start it "
                          "with:  python tools/stvt_schedule.py run")
            input("\n[enter]"); continue

        if cmd in ("r", "rn", "rmux", "show"):
            try:
                idx = int(arg) - 1
                row = rows[idx]
            except (ValueError, IndexError):
                print(f"bad #: '{arg}'"); input("[enter]"); continue
            if cmd == "show":
                events = find_events_for_program(scan, row["virtual"])
                print(f"\nupcoming on {row['virtual']} {row['callsign']}:")
                for e in events[:6]:
                    print(f"  {fmt_time(e['start_unix'])}  "
                          f"({e['length_sec']//60}min)  {e['title']}")
                input("\n[enter]"); continue
            ev = row["on_now"] if cmd in ("r", "rmux") else row["next"]
            if not ev:
                print(f"no event to record on {row['virtual']}")
                input("[enter]"); continue
            mux = cmd == "rmux"
            status, entry, conflicts = schedule_event(ev, mux=mux)
            if status == "duplicate":
                print(f"already scheduled: {entry['id']}")
            else:
                tag = "[MUX] " if mux else ""
                print(f"queued: {tag}{entry['virtual']} \"{entry['title']}\" "
                      f"at {fmt_time(entry['start_unix'])} "
                      f"({entry['length_sec']//60} min)")
                if conflicts:
                    print(f"WARNING: conflicts with {len(conflicts)} other "
                          f"queued recording(s) on different muxes — "
                          f"earliest wins.")
                if not is_daemon_running():
                    print("NOTE: scheduler daemon is NOT running. Start it "
                          "with:  python tools/stvt_schedule.py run")
            input("\n[enter]"); continue

        print(f"unknown command: {cmd!r}"); input("[enter]")


def is_daemon_running() -> bool:
    """Best-effort detect of an active scheduler daemon."""
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["wmic", "process", "where", "name='python.exe'",
                 "get", "CommandLine", "/format:csv"],
                capture_output=True, text=True,
                creationflags=0x08000000,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        for line in out.splitlines():
            if "stvt_schedule.py" in line and " run" in line:
                return True
        return False
    try:
        out = subprocess.run(["pgrep", "-af", "stvt_schedule.py.*run"],
                              capture_output=True, text=True).stdout
        return bool(out.strip())
    except (OSError, FileNotFoundError):
        return False


def compute_skip_reason(entry: dict, active_ids: list[str],
                         queue: list[dict]) -> str | None:
    """Decide whether `entry` should be skipped because the SDR is
    already busy. Returns the human-readable skip reason, or None if
    it's safe to fire.

    The SDR is single-tenant: only one multirec can hold the device.
    If another entry is already in `active_ids`, the new fire must be
    skipped (even on the SAME RF — a 2nd multirec on the same RF would
    spawn its own tv_live which can't acquire the SDR and dies fast,
    producing a stub ~30s file that LOOKS done but isn't).

    Same-RF and different-RF skips get distinct messages so the user
    knows whether to use rmux (same) or just accept the conflict
    (different).

    Mirrors the inline logic in cmd_run; extracted so unit tests can
    pin the skip rules without spawning the daemon.
    """
    for eid in active_ids:
        active_entry = next((q for q in queue if q["id"] == eid), None)
        if not active_entry:
            continue
        if active_entry["rf"] == entry["rf"]:
            return (f"same-mux RF{entry['rf']} record already active "
                    f"(\"{active_entry['title']}\"); use rmux to cover both")
        return (f"different-mux RF{active_entry['rf']} record already "
                f"active (\"{active_entry['title']}\"); only one SDR")
    return None


def compute_fire_action(entry: dict, active_ids: list[str],
                         queue: list[dict]) -> tuple[str, str | None]:
    """Decide what the daemon should do with a fire-ready entry.
    Returns (action, reason) where action is one of:
        "fire"  -> safe to start now
        "skip"  -> mark skipped (covered by another recording on same mux)
        "defer" -> wait, try again next poll (cross-mux SDR contention
                   that will resolve when the active recording finishes)

    Defer-on-cross-mux is what lets back-to-back schedules work. Without
    it the daemon would skip every cross-mux entry whose fire window
    opens while a previous recording's tail is still running (typical
    when slot N's +pad collides with slot N+1's -lead_sec). With it,
    the new entry waits a few seconds and starts late — recorded, not
    skipped. The cascade-clamp in compute_fire_duration_min then keeps
    the late-fired entry inside its own original end window.
    """
    for eid in active_ids:
        active_entry = next((q for q in queue if q["id"] == eid), None)
        if not active_entry:
            continue
        if active_entry["rf"] == entry["rf"]:
            return ("skip",
                    f"same-mux RF{entry['rf']} record already active "
                    f"(\"{active_entry['title']}\"); use rmux to cover "
                    f"both")
        return ("defer",
                f"waiting for different-mux RF{active_entry['rf']} "
                f"record (\"{active_entry['title']}\") to finish before "
                f"firing RF{entry['rf']}")
    return ("fire", None)


def compute_fire_duration_min(entry: dict, now_unix: int) -> tuple[int, bool]:
    """Decide how many minutes to record for this entry, clamped to the
    time remaining until entry["end_unix"].

    Returns (duration_min, clamped). `clamped` is True iff the clamp
    actually shortened the recording below the original length.

    Bug this prevents: if the daemon fires an entry LATE (e.g. it's
    1:24 AM but the entry's start_unix was 12:00 AM with a 90-minute
    length), naively passing length_sec//60 + 1 = 91 min to multirec
    would record until ~2:55 AM and steal the next mux slot. Clamping
    to (end_unix - now_unix) keeps the recording inside its original
    window, with a 1-min pad and a 1-min floor.
    """
    original_sec = max(0, entry["length_sec"])
    remaining_sec = entry["end_unix"] - now_unix
    # Floor at 60s so an extremely-late fire still gets a usable file
    # instead of `--duration 0` (which multirec would reject).
    clamped_sec = max(60, min(original_sec, remaining_sec))
    duration_min = max(1, clamped_sec // 60 + 1)  # 1-min pad
    was_clamped = remaining_sec < original_sec
    return duration_min, was_clamped


def fire_recording(entry: dict, out_dir: Path | None,
                   pre_roll_sec: int) -> subprocess.Popen | None:
    """Spawn stvt_multirec.py for this entry. Returns Popen or None.

    Entry shapes supported:
      single-program:  {"program": N}              -> --programs N
      multi-program:   {"programs": [N1, N2, ...]} -> --programs N1,N2,...
      whole-mux:       {"mux_record": True}        -> no --programs flag
                                                      (records every program)
    """
    now_unix = int(time.time())
    duration_min, was_clamped = compute_fire_duration_min(entry, now_unix)
    if was_clamped:
        original_min = max(1, entry["length_sec"] // 60)
        print(f"[scheduler] late-fire clamp: recording for "
              f"{duration_min} min instead of original {original_min} min "
              f"(entry {entry['id']} end window passes in "
              f"{max(0, entry['end_unix'] - now_unix)}s)")
    cmd = [PYTHON_EXE, "-u", str(MULTIREC_PY),
           "--rf", str(entry["rf"]),
           "--duration", str(duration_min)]
    if entry.get("mux_record"):
        pass  # no --programs = multirec records all
    elif entry.get("programs"):
        cmd += ["--programs",
                ",".join(str(p) for p in entry["programs"])]
    else:
        cmd += ["--programs", str(entry["program"])]
    if out_dir:
        cmd += ["--output-dir", str(out_dir)]
    log_dir = QUEUE_PATH.parent / "scheduler_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{entry['id']}.log"
    log_fh = log_path.open("w", encoding="utf-8", errors="replace")
    log_fh.write(f"[scheduler] firing {entry['id']} at "
                 f"{datetime.now().isoformat()}\n")
    log_fh.write(f"[scheduler] cmd: {' '.join(cmd)}\n\n")
    log_fh.flush()
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (0x00000008 | 0x00000200 | 0x08000000)  # detached
    return subprocess.Popen(
        cmd,
        stdout=log_fh, stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )


def cmd_run(args) -> int:
    out_dir = Path(os.path.expanduser(args.output_dir)) if args.output_dir else None
    print(f"[scheduler] daemon started — polling every "
          f"{args.poll_sec}s.")
    print(f"[scheduler] queue path: {QUEUE_PATH}")
    if out_dir:
        print(f"[scheduler] output: {out_dir}")
    print("[scheduler] Ctrl+C to stop.")
    print()

    active: dict[str, subprocess.Popen] = {}
    deferred_logged: set[str] = set()  # entries we've already printed DEFER for
    while True:
        try:
            queue = load_queue()
            now = int(time.time())

            # Reap finished children
            done_ids = []
            for eid, proc in active.items():
                if proc.poll() is not None:
                    done_ids.append(eid)
            for eid in done_ids:
                rc = active[eid].returncode
                print(f"[scheduler] {datetime.now().strftime('%H:%M:%S')}  "
                      f"finished {eid} (exit {rc})")
                del active[eid]
                for q in queue:
                    if q["id"] == eid:
                        q["status"] = "done" if rc == 0 else f"err{rc}"
                        break
                save_queue(queue)

            # Fire anything whose start time has arrived
            for entry in queue:
                if entry.get("status") != "pending":
                    continue
                if entry["id"] in active:
                    continue
                # Skip missed shows (>MISS_GRACE_SEC past end)
                if entry["end_unix"] + MISS_GRACE_SEC < now:
                    entry["status"] = "missed"
                    save_queue(queue)
                    print(f"[scheduler] {datetime.now().strftime('%H:%M:%S')}  "
                          f"missed {entry['id']} (window passed)")
                    continue
                # Fire if start is within args.lead_sec
                if entry["start_unix"] - args.lead_sec <= now:
                    # SDR is single-tenant. compute_fire_action decides
                    # whether to fire, skip (same-mux already covered),
                    # or defer (cross-mux contention that will resolve
                    # when the active record finishes — enables proper
                    # back-to-back scheduling).
                    action, reason = compute_fire_action(
                        entry, list(active.keys()), queue
                    )
                    if action == "skip":
                        print(f"[scheduler] "
                              f"{datetime.now().strftime('%H:%M:%S')}  "
                              f"SKIP {entry['id']}: {reason}")
                        entry["status"] = f"skipped: {reason}"
                        save_queue(queue)
                        deferred_logged.discard(entry["id"])
                        continue
                    if action == "defer":
                        # Don't fire, don't mark skipped — wait for the
                        # active record to finish. The "missed" check at
                        # the top of the loop catches the case where
                        # active never finishes and this entry's window
                        # actually expires.
                        if entry["id"] not in deferred_logged:
                            print(f"[scheduler] "
                                  f"{datetime.now().strftime('%H:%M:%S')}  "
                                  f"DEFER {entry['id']}: {reason}")
                            deferred_logged.add(entry["id"])
                        continue
                    print(f"[scheduler] {datetime.now().strftime('%H:%M:%S')}  "
                          f"firing {entry['id']}  "
                          f"({entry['virtual']} \"{entry['title']}\")")
                    proc = fire_recording(entry, out_dir, args.lead_sec)
                    if proc:
                        active[entry["id"]] = proc
                        entry["status"] = "recording"
                        save_queue(queue)
                        deferred_logged.discard(entry["id"])

            time.sleep(args.poll_sec)
        except KeyboardInterrupt:
            print(f"\n[scheduler] stopping — {len(active)} active "
                  f"recording(s) will continue in the background.")
            return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show queued recordings")

    sub.add_parser("tv", help="interactive DVR controller (EPG + "
                              "record-from-grid)")

    p_add = sub.add_parser("add-show",
                            help="schedule next instance of a show by title")
    p_add.add_argument("virtual", help='Virtual channel e.g. "4.1"')
    p_add.add_argument("title", help="Title substring to search for")

    p_man = sub.add_parser("add", help="manually schedule by rf+program+time")
    p_man.add_argument("--rf", type=int, required=True)
    p_man.add_argument("--program", type=int, required=True)
    p_man.add_argument("--start", required=True,
                       help='Start time "YYYY-MM-DD HH:MM" local')
    p_man.add_argument("--duration", type=float, required=True,
                       help="Duration in minutes")
    p_man.add_argument("--title", default=None)

    p_rm = sub.add_parser("remove", help="cancel a recording by id (prefix ok)")
    p_rm.add_argument("id")

    p_run = sub.add_parser("run", help="daemon — fire recordings at start times")
    p_run.add_argument("--poll-sec", type=int, default=15,
                       help="How often to check the queue (default 15s)")
    p_run.add_argument("--lead-sec", type=int, default=30,
                       help="Start recording this many seconds early "
                            "(chain lock takes ~15s, default 30)")
    p_run.add_argument("--output-dir", default=None,
                       help="Where to write the recordings")

    args = ap.parse_args()
    cmds = {"list": cmd_list, "add-show": cmd_add_show, "add": cmd_add,
            "remove": cmd_remove, "run": cmd_run, "tv": cmd_tv}
    return cmds[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
