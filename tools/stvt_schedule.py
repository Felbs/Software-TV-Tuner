"""DVR scheduler for STVT.

Pick shows from the EPG grid (stvt_epg.py), schedule them to record,
then run the daemon to fire stvt_multirec.py at each show's start time.

Queue is persisted to ~/.tv_tuner/schedule.json so a daemon restart
keeps pending recordings.

Commands:

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
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HERE = Path(__file__).resolve().parent
PYTHON_EXE = sys.executable
MULTIREC_PY = HERE / "stvt_multirec.py"
QUEUE_PATH = Path(os.path.expanduser("~")) / ".tv_tuner" / "schedule.json"
SCAN_PATH = Path(os.path.expanduser("~")) / ".tv_tuner" / "scan.json"

GPS_OFFSET = 315_964_800
LEAP = 18


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
    entry = {
        "id":         make_id(target["rf"], target["program"],
                              target["start_unix"], target["title"]),
        "rf":         target["rf"],
        "program":    target["program"],
        "virtual":    target["virtual"],
        "callsign":   target["callsign"],
        "title":      target["title"],
        "start_unix": target["start_unix"],
        "end_unix":   target["end_unix"],
        "length_sec": target["length_sec"],
        "status":     "pending",
    }
    queue = load_queue()
    conflicts = detect_conflicts(queue, entry)
    result = add_to_queue(entry)
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
        print(f"           The earlier-starting entry will win; others "
              f"get skipped.")
    return 0


def cmd_add(args) -> int:
    try:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d %H:%M")
    except ValueError:
        print(f"[schedule] --start must be 'YYYY-MM-DD HH:MM' "
              f"(24-hour, local time)", file=sys.stderr)
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


def fire_recording(entry: dict, out_dir: Path | None,
                   pre_roll_sec: int) -> subprocess.Popen | None:
    """Spawn stvt_multirec.py for this entry. Returns Popen or None."""
    duration_min = max(1, entry["length_sec"] // 60 + 1)  # 1 min pad
    cmd = [PYTHON_EXE, "-u", str(MULTIREC_PY),
           "--rf", str(entry["rf"]),
           "--programs", str(entry["program"]),
           "--duration", str(duration_min)]
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
    print(f"[scheduler] Ctrl+C to stop.")
    print()

    active: dict[str, subprocess.Popen] = {}
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
                # Skip missed shows (>2 min past end)
                if entry["end_unix"] + 120 < now:
                    entry["status"] = "missed"
                    save_queue(queue)
                    print(f"[scheduler] {datetime.now().strftime('%H:%M:%S')}  "
                          f"missed {entry['id']} (window passed)")
                    continue
                # Fire if start is within args.lead_sec
                if entry["start_unix"] - args.lead_sec <= now:
                    # Mux conflict: refuse to start if active record is on
                    # a different RF
                    for eid, proc in active.items():
                        active_entry = next((q for q in queue if q["id"] == eid),
                                             None)
                        if active_entry and active_entry["rf"] != entry["rf"]:
                            print(f"[scheduler] "
                                  f"{datetime.now().strftime('%H:%M:%S')}  "
                                  f"SKIP {entry['id']}: RF{entry['rf']} "
                                  f"conflicts with active RF"
                                  f"{active_entry['rf']}")
                            entry["status"] = "skipped"
                            save_queue(queue)
                            break
                    if entry.get("status") != "pending":
                        continue
                    print(f"[scheduler] {datetime.now().strftime('%H:%M:%S')}  "
                          f"firing {entry['id']}  "
                          f"({entry['virtual']} \"{entry['title']}\")")
                    proc = fire_recording(entry, out_dir, args.lead_sec)
                    if proc:
                        active[entry["id"]] = proc
                        entry["status"] = "recording"
                        save_queue(queue)

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
            "remove": cmd_remove, "run": cmd_run}
    return cmds[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
