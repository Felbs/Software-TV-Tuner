"""EPG accuracy checker.

Cross-references the show titles in our EPG grid (loaded from
~/.tv_tuner/scan.json's PSIP/EIT data) against an online listing for
the same DC-area channels. Prints rows where the broadcaster's EIT
and the internet listings disagree, so you can tell which entries in
your grid are stale.

It is a sanity check, not a replacement for a fresh scan: when you
rescan, the broadcaster sends fresh EIT and the grid updates.

This script fetches TVPassport's free public listings page for the
WashingtonDC market — no API key needed — and parses the schedule
into channel/time/title rows for comparison.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

SCAN_PATH = Path(os.path.expanduser("~")) / ".tv_tuner" / "scan.json"

GPS_OFFSET = 315_964_800
LEAP = 18


def gps_to_unix(g):
    return int(g + GPS_OFFSET - LEAP)


def load_scan_now_showing() -> list[dict]:
    """Return [{virtual, callsign, title, start_unix, length}] for each
    show currently airing per the broadcaster's EIT."""
    if not SCAN_PATH.exists():
        return []
    data = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    now = int(time.time())
    out = []
    for c in data.get("channels", []):
        callsign = c.get("callsign")
        if not callsign or callsign in ("?", "None"):
            continue
        psip = c.get("psip") or {}
        psip_chs = psip.get("channels") or []
        psip_events = psip.get("events") or {}
        psip_by_pnum = {p.get("program_number"): p for p in psip_chs
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
            for e in psip_events.get(str(pnum), []):
                start = gps_to_unix(e.get("start_gps", 0))
                length = e.get("length_sec") or 0
                if start <= now < start + length:
                    out.append({
                        "virtual":    str(virtual),
                        "callsign":   short,
                        "title":      e.get("title") or "(untitled)",
                        "start_unix": start,
                        "length":     length,
                    })
                    break
    return out


class TVPassportParser(HTMLParser):
    """Parse a TVPassport listings page into a list of
    {virtual, callsign, title, time} dicts.

    The page DOM uses .channel rows with the callsign + virtual, and
    each row has .listing items containing title + start time.

    NOTE: HTML structures change. If TVPassport's markup shifts this
    parser stops matching — that's OK, this is a sanity check tool,
    not a service.
    """

    def __init__(self):
        super().__init__()
        self.rows = []
        self._cur_chan = None
        self._cur_listing = {}
        self._mode = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        cls = attrs_d.get("class", "")
        if "channel" in cls and "channel-info" not in cls:
            self._cur_chan = {"callsign": "", "virtual": ""}
        if "callsign" in cls or "channel-callsign" in cls:
            self._mode = "callsign"; self._text = []
        elif "channel-number" in cls or "virtual" in cls:
            self._mode = "virtual"; self._text = []
        elif "listing-title" in cls or "show-title" in cls:
            self._mode = "title"; self._text = []
        elif "listing-time" in cls or "show-time" in cls:
            self._mode = "time"; self._text = []

    def handle_endtag(self, tag):
        if self._mode is None:
            return
        text = "".join(self._text).strip()
        if self._mode == "callsign" and self._cur_chan is not None:
            self._cur_chan["callsign"] = text
        elif self._mode == "virtual" and self._cur_chan is not None:
            self._cur_chan["virtual"] = text
        elif self._mode == "title":
            self._cur_listing["title"] = text
        elif self._mode == "time":
            self._cur_listing["time"] = text
            if self._cur_chan and self._cur_listing.get("title"):
                self.rows.append({
                    "callsign": self._cur_chan.get("callsign", ""),
                    "virtual":  self._cur_chan.get("virtual", ""),
                    "title":    self._cur_listing["title"],
                    "time":     self._cur_listing["time"],
                })
            self._cur_listing = {}
        self._mode = None
        self._text = []

    def handle_data(self, data):
        if self._mode is not None:
            self._text.append(data)


def fetch_listings(market_url: str) -> list[dict]:
    """Fetch + parse a TV listings page. Returns parsed rows."""
    req = urllib.request.Request(
        market_url,
        headers={"User-Agent": "Mozilla/5.0 stvt-epg-verify"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", errors="replace")
    p = TVPassportParser()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.rows


def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market-url",
                    default="https://www.tvpassport.com/tv-listings/zip/20001/atrz",
                    help="URL of an online listings page for your market")
    ap.add_argument("--max-rows", type=int, default=40,
                    help="How many channels to check")
    args = ap.parse_args()

    print(f"[verify] now      : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[verify] market   : {args.market_url}")
    print("[verify] reading scan EIT...")
    scan_now = load_scan_now_showing()
    if not scan_now:
        print("[verify] no scan data — run a fresh scan via "
              "tv_tuner.py --scan")
        return 1
    print(f"[verify] scan says {len(scan_now)} channels are airing something now")
    print()

    print("[verify] fetching online listings... (may take a few seconds)")
    try:
        net_rows = fetch_listings(args.market_url)
    except Exception as e:
        print(f"[verify] fetch failed: {e}")
        print("[verify] partial verification only — try a fresh scan if "
              "shows look stale")
        net_rows = []
    print(f"[verify] fetched {len(net_rows)} rows from online listings")
    print()

    # Build callsign-keyed online lookup
    by_call = {}
    for r in net_rows:
        cs = (r.get("callsign") or "").upper()
        if cs:
            by_call.setdefault(cs, []).append(r)

    print(f"{'virt':<6} {'callsign':<8} {'scan title':<35} {'online title':<35} match")
    print("-" * 100)
    match = mismatch = unknown = 0
    for row in scan_now[: args.max_rows]:
        cs_match = by_call.get((row["callsign"] or "").upper(), [])
        if not cs_match:
            verdict = "?"
            online = "(not in fetch)"
            unknown += 1
        else:
            # First matching listing for this channel
            online = (cs_match[0].get("title") or "?")
            if normalize(row["title"]) == normalize(online):
                verdict = "OK"
                match += 1
            elif (normalize(row["title"]) in normalize(online)
                  or normalize(online) in normalize(row["title"])):
                verdict = "close"
                match += 1
            else:
                verdict = "MISMATCH"
                mismatch += 1
        print(f"{row['virtual']:<6} {row['callsign'][:8]:<8} "
              f"{row['title'][:35]:<35} {online[:35]:<35} {verdict}")
    print()
    print(f"[verify] {match} match, {mismatch} mismatch, {unknown} not in online fetch")
    if mismatch + unknown > match:
        print("[verify] scan data looks stale — run a fresh scan:")
        print("          python tools/tv_tuner.py --scan")
    return 0


if __name__ == "__main__":
    sys.exit(main())
