"""EPG (Electronic Program Guide) grid for STVT.

Reads scan.json's per-program EIT data and renders a printable grid:

           6:00 PM      6:30 PM      7:00 PM      7:30 PM
  4.1 WRC  Dateline: U. NBC Nightly  Wheel of Fr. Jeopardy
  4.2 COZI Magnum P.I.  Magnum P.I.  Hawaii 5-0   Hawaii 5-0
  ...

Rules:
  * Default view is 2 hours forward from the current half-hour boundary,
    in four 30-minute columns.
  * Events that span multiple columns show their title in the first one
    they occupy.
  * Programs the scan couldn't pull EIT for show "(no guide data)".
  * Currently-airing event is highlighted (ANSI bold) when the terminal
    supports color.

GPS-to-UTC math:
  GPS epoch  = 1980-01-06 00:00:00 UTC
  Unix epoch = 1970-01-01 00:00:00 UTC
  difference = 315964800 sec
  GPS does NOT include leap seconds; UTC does. As of 2025+ the
  GPS-UTC offset is 18 seconds, so:
      unix_time = gps_time + 315964800 - 18

Usage:
  python tools/stvt_epg.py                     # full grid, all channels
  python tools/stvt_epg.py --hours 3           # 3-hour window
  python tools/stvt_epg.py --rf 34             # only this RF
  python tools/stvt_epg.py --watch             # auto-refresh every 60s
  python tools/stvt_epg.py --no-color          # plain ASCII
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

SCAN_PATH = Path(os.path.expanduser("~")) / ".tv_tuner" / "scan.json"

GPS_UNIX_OFFSET = 315_964_800       # difference between epochs
GPS_UTC_LEAP_SECONDS = 18           # current as of 2025+; check IERS for updates


def gps_to_unix(gps_seconds: int) -> int:
    return int(gps_seconds + GPS_UNIX_OFFSET - GPS_UTC_LEAP_SECONDS)


def load_epg() -> tuple[list[dict], int | None]:
    """Returns (channels, scan_unix_timestamp). channels = sorted list of
    {rf, virtual, callsign, network, events:[{title, start_unix, length, rating, desc}]}"""
    if not SCAN_PATH.exists():
        return [], None
    try:
        data = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], None

    # The scan_at time tells us how stale the grid is.
    scan_unix = None
    scanned_at = data.get("scanned_at")
    if scanned_at:
        try:
            # ISO-8601 string
            import datetime as dt
            scan_unix = int(dt.datetime.fromisoformat(scanned_at).timestamp())
        except (ValueError, TypeError):
            pass

    out = []
    for c in data.get("channels", []):
        if c.get("not_detected"):
            continue
        callsign = c.get("callsign")
        if not callsign or callsign in ("?", "None"):
            continue
        network_hint = c.get("network_hint") or ""
        psip = c.get("psip") or {}
        psip_channels = psip.get("channels") or []
        psip_events = psip.get("events") or {}

        # program_number -> (major.minor, short_name)
        psip_by_pnum = {p.get("program_number"): p for p in psip_channels
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

            # Events for this program are keyed by program_number (as str)
            evs_raw = psip_events.get(str(pnum), [])
            events = []
            for e in evs_raw:
                start_unix = gps_to_unix(e.get("start_gps", 0))
                events.append({
                    "title":      e.get("title") or "(untitled)",
                    "start_unix": start_unix,
                    "length":     e.get("length_sec") or 0,
                    "rating":     e.get("rating") or "",
                    "desc":       e.get("description") or "",
                })
            events.sort(key=lambda x: x["start_unix"])
            # Adaptive-era tuneability: the guide can KNOW about stations the
            # current antenna cannot receive (EIT is cross-carried between
            # broadcasters). Badge each row from the scan's lock results so
            # the grid never implies an untunable station is watchable.
            if c.get("lock"):
                tune = "+"          # locked = tuneable on this antenna
            elif c.get("hot") or (c.get("pilot_snr_db") or 0) >= 20:
                tune = "~"          # carrier seen, never locked = marginal
            else:
                tune = "x"          # guide data only — out of reach
            out.append({
                "rf":       c["rf"],
                "program":  pnum,
                "virtual":  str(virtual),
                "callsign": short,
                "network":  network_hint,
                "tune":     tune,
                "snr_db":   c.get("pilot_snr_db"),
                "events":   events,
            })

    def sort_key(r):
        try:
            major, _, minor = r["virtual"].partition(".")
            return (int(major), int(minor or "1"))
        except ValueError:
            return (9999, 0)
    out.sort(key=sort_key)
    return out, scan_unix


def fmt_time(unix_t: int, twelve_hour: bool = True) -> str:
    if twelve_hour:
        return time.strftime("%-I:%M %p", time.localtime(unix_t)) \
               if hasattr(time, "_strftime_dash_I_supported") \
               else time.strftime("%I:%M %p", time.localtime(unix_t)).lstrip("0")
    return time.strftime("%H:%M", time.localtime(unix_t))


def event_at(events: list[dict], when_unix: int) -> dict | None:
    """Find the event covering `when_unix`. Events are sorted by start."""
    for e in events:
        if e["start_unix"] <= when_unix < e["start_unix"] + e["length"]:
            return e
    return None


def truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    if n <= 1:
        return s[:n]
    return s[:n - 1] + "."


def color_supported() -> bool:
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 4
            h = kernel32.GetStdHandle(-11)  # STDOUT
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(h, ctypes.byref(mode))
            kernel32.SetConsoleMode(h, mode.value | 4)
            return True
        except OSError:
            return False
    return True


BOLD = "\033[1m"; CYAN = "\033[36m"; YELLOW = "\033[33m"
DIM  = "\033[2m"; RESET = "\033[0m"; GREEN = "\033[32m"


def render_grid(channels: list[dict], rf_filter: int | None,
                hours: float, color: bool, scan_unix: int | None) -> str:
    now = int(time.time())
    grid_start = (now // 1800) * 1800
    slot_secs = 1800
    n_slots = max(2, int(hours * 3600 / slot_secs))

    chan_width = 12
    slot_width = max(10, (118 - chan_width) // n_slots)

    if rf_filter is not None:
        channels = [c for c in channels if c["rf"] == rf_filter]

    if not channels:
        return "(no channels in scan match this filter — run a scan?)\n"

    c = lambda s, code: f"{code}{s}{RESET}" if color else s

    lines = []
    title = f"STVT EPG  {time.strftime('%a %b %d %Y', time.localtime(now))}  " \
            f"({n_slots * 30}-min window)"
    lines.append(c(title, BOLD))

    # Staleness banner
    if scan_unix is not None:
        age_h = (now - scan_unix) / 3600
        if age_h > 24:
            lines.append(c(f"  ! scan is {age_h:.0f} h old — EIT events may be "
                           f"out of date. Re-scan for fresh guide data.",
                           YELLOW))

    # Header row: time slot labels
    header = " " * chan_width + " | "
    slot_starts = []
    for i in range(n_slots):
        slot_t = grid_start + i * slot_secs
        slot_starts.append(slot_t)
        header += truncate(fmt_time(slot_t), slot_width - 1).ljust(slot_width)
    lines.append(c(header, CYAN))
    lines.append("-" * (chan_width + 3 + n_slots * slot_width))

    for ch in channels:
        cell_strs = []
        for slot_t in slot_starts:
            ev = event_at(ch["events"], slot_t)
            if ev is None:
                cell_strs.append("(no data)")
                continue
            # Skip showing the title if this is a continuation of an earlier
            # slot we already labelled — that way two consecutive 30-min
            # slots of a 60-min show only print the title once.
            prev_slot = slot_t - slot_secs
            prev_ev = event_at(ch["events"], prev_slot) if prev_slot >= grid_start else None
            same = prev_ev is not None and prev_ev["start_unix"] == ev["start_unix"]
            if same:
                cell_strs.append(">> " + truncate(ev["title"], slot_width - 3))
            else:
                cell_strs.append(truncate(ev["title"], slot_width - 1))

        # Highlight "now" column
        now_idx = max(0, min(n_slots - 1, (now - grid_start) // slot_secs))
        styled = []
        for i, s in enumerate(cell_strs):
            cell = s.ljust(slot_width)
            if i == now_idx and event_at(ch["events"], now) is not None:
                cell = c(cell, BOLD + GREEN)
            elif s.startswith("(no data)"):
                cell = c(cell, DIM)
            styled.append(cell)

        ch_label = f"{ch.get('tune', ' ')} {ch['virtual']:<5} {truncate(ch['callsign'], 6):<6}"
        lines.append(f"{ch_label} | " + "".join(styled))

    lines.append("")
    lines.append(c("  green = on now    » = show continues from prior slot", DIM))
    lines.append(c("  + = tuneable on this antenna    ~ = weak carrier (may not decode)"
                   "    x = guide data only, out of reach", DIM))
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--hours", type=float, default=2,
                    help="Window length in hours (default 2 -> 4 slots)")
    ap.add_argument("--rf", type=int, default=None,
                    help="Only show channels on this RF")
    ap.add_argument("--no-color", action="store_true",
                    help="Plain ASCII output")
    ap.add_argument("--watch", action="store_true",
                    help="Auto-refresh every 60s until Ctrl+C")
    args = ap.parse_args()

    color = (not args.no_color) and color_supported()

    # Make stdout tolerate broadcaster oddities (smart quotes, special
    # punctuation in show titles). Without this, Windows cp1252 chokes
    # on a single � and the whole grid fails to print.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    def render_once():
        channels, scan_unix = load_epg()
        if not channels:
            print("(no scan data — run `python tools/tv_tuner.py --scan` first)",
                  file=sys.stderr)
            return False
        if sys.platform == "win32":
            os.system("cls" if args.watch else "rem")  # clear if watching
        else:
            if args.watch:
                os.system("clear")
        out = render_grid(channels, args.rf, args.hours, color, scan_unix)
        # Belt-and-suspenders for any chars the reconfigure couldn't handle.
        try:
            sys.stdout.write(out)
        except UnicodeEncodeError:
            sys.stdout.write(out.encode("ascii", "replace").decode("ascii"))
        return True

    if args.watch:
        try:
            while True:
                ok = render_once()
                if not ok:
                    return 1
                time.sleep(60)
        except KeyboardInterrupt:
            print()
            return 0
    else:
        return 0 if render_once() else 1


if __name__ == "__main__":
    sys.exit(main())
