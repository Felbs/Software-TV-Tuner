#!/usr/bin/env python
"""time_knob.py — universal hour-resolved channel-quality memory (PROTOTYPE).

Nothing imports this yet. Pure stdlib. Design: lab/time_knob_research.md.

The idea: "best channel is a function of time" (proven 7/06 overnight cube).
Instead of a one-night fossil (cube_map.json, hardcoded antenna names), keep an
append-only history of every quality measurement the rig produces — scans,
live-viewing telemetry, lab soaks — and derive per-channel hour-of-day curves
on demand, with recency decay and honest "unknown" for hours never observed.

Universality laws honored here:
  * antenna is an OPAQUE string exactly as the producing tool logged it
    ("Antenna B", "rabbit", whatever a stranger's rig calls its ports);
    ownership is a learned per-hour statistic, never a hardcoded name.
  * no location, no channel list, no personal calibration in code.
  * recency decay (default half-life 14 days) — every config goes stale.
  * a bin fed by a single day is never reported as solid: diurnal claims
    need >= 2 distinct days (the RF31 lesson: 16 hour-bins, all one day).

Public API (all pure functions over plain dicts):
    record(row)                 append one measurement to the history file
    load()                      read history -> list of row dicts
    curve(rf, rows, ...)        {hour: {mer, loss, n, days, conf}}
    best_hours(rf, rows, ...)   (best_hours, worst_hours, swing_db) or None
    hint(rf, rows, now_hour)    "usually better around 19h" | None
    antenna_by_hour(rf, rows)   learned per-hour antenna ownership
    nerd_card(rf, rows)         text block for the panel's NERD card

CLI:
    python time_knob.py selftest
    python time_knob.py backfill          # harvest historical logs -> history
    python time_knob.py report [rf ...]   # print curves
    python time_knob.py card <rf>         # print one NERD card
"""
import csv
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, date, time as dtime
from pathlib import Path

HERE = Path(__file__).parent
HISTORY = HERE / "lab" / "quality_history.csv"
FIELDS = ["ts", "rf", "ant", "mer", "loss_pct", "source", "date_known"]

HALF_LIFE_DAYS = 14.0   # recency half-life; every config is a loan
SOLID_N = 8             # samples for a solid bin ...
SOLID_DAYS = 2          # ... spread over at least this many distinct days
THIN_N = 3              # below solid but usable with a caveat
HINT_DB = 2.0           # hint only when best solid hour beats now by this much

# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def record(row, path=HISTORY):
    """Append one measurement. row keys: ts (datetime|iso str), rf (int),
    ant (str, opaque), mer (float|None), loss_pct (float|None),
    source (str), date_known (bool, default True). Append-only, no locks —
    single-writer-per-tool, last-write-wins is fine for a log."""
    ts = row["ts"]
    if isinstance(ts, datetime):
        ts = ts.replace(microsecond=0).isoformat()
    if row.get("mer") is None and row.get("loss_pct") is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FIELDS)
        w.writerow([ts, int(row["rf"]), row.get("ant") or "?",
                    "" if row.get("mer") is None else round(float(row["mer"]), 2),
                    "" if row.get("loss_pct") is None else round(float(row["loss_pct"]), 3),
                    row.get("source", "?"), int(row.get("date_known", True))])
    return True


def load(path=HISTORY):
    """History file -> list of row dicts (ts becomes datetime). Missing file
    -> [] (fresh install: everything is honestly unknown)."""
    if not Path(path).exists():
        return []
    out = []
    with open(path, newline="", encoding="utf-8", errors="ignore") as f:
        for r in csv.DictReader(f):
            try:
                out.append(dict(
                    ts=datetime.fromisoformat(r["ts"]),
                    rf=int(r["rf"]), ant=r["ant"] or "?",
                    mer=float(r["mer"]) if r["mer"] else None,
                    loss_pct=float(r["loss_pct"]) if r["loss_pct"] else None,
                    source=r.get("source", "?"),
                    date_known=r.get("date_known", "1") == "1"))
            except (ValueError, KeyError):
                continue  # one bad line never poisons the model
    return out

# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------

def _weight(ts, now, half_life):
    age = max(0.0, (now - ts).total_seconds() / 86400.0)
    return 0.5 ** (age / half_life)


def _wmedian(pairs):
    """Weighted median of [(value, weight)]."""
    pairs = sorted(p for p in pairs if p[0] is not None)
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2.0:
            return v
    return pairs[-1][0]


def _bins(rf, rows, ant=None, now=None, half_life=HALF_LIFE_DAYS):
    """hour -> dict(mer_w=[(v,w)], loss_w=[(v,w)], days=set)."""
    now = now or datetime.now()
    bins = {h: dict(mer_w=[], loss_w=[], days=set()) for h in range(24)}
    for r in rows:
        if r["rf"] != rf or (ant is not None and r["ant"] != ant):
            continue
        b = bins[r["ts"].hour]
        w = _weight(r["ts"], now, half_life)
        if r["mer"] is not None:
            b["mer_w"].append((r["mer"], w))
        if r["loss_pct"] is not None:
            b["loss_w"].append((r["loss_pct"], w))
        b["days"].add(r["ts"].date())
    return bins


def curve(rf, rows, ant=None, now=None, half_life=HALF_LIFE_DAYS):
    """Hour-of-day quality curve. Returns {hour: bin} where bin is
      {mer, loss, n, days, conf}   conf in: "solid" | "thin" | "trace"
                                           | "borrowed" | None (unknown)
    Empty hours borrow neighbors (h±1) at half weight — flagged, never
    passed off as measured. mer is None whenever conf is None."""
    raw = _bins(rf, rows, ant, now, half_life)
    out = {}
    for h in range(24):
        b = raw[h]
        n, days = len(b["mer_w"]), len(b["days"])
        if n == 0:
            # borrow from neighbors at half weight, honestly labeled
            mw = [(v, w * 0.5) for nb in ((h - 1) % 24, (h + 1) % 24)
                  for v, w in raw[nb]["mer_w"]]
            days_b = len(raw[(h - 1) % 24]["days"] | raw[(h + 1) % 24]["days"])
            if len(mw) >= THIN_N:
                out[h] = dict(mer=round(_wmedian(mw), 2), loss=None,
                              n=len(mw), days=days_b, conf="borrowed")
            else:
                out[h] = dict(mer=None, loss=None, n=0, days=0, conf=None)
            continue
        conf = ("solid" if n >= SOLID_N and days >= SOLID_DAYS
                else "thin" if n >= THIN_N else "trace")
        loss = _wmedian(b["loss_w"])
        out[h] = dict(mer=round(_wmedian(b["mer_w"]), 2),
                      loss=None if loss is None else round(loss, 3),
                      n=n, days=days, conf=conf)
    return out


def best_hours(rf, rows, ant=None, now=None, half_life=HALF_LIFE_DAYS,
               within_db=0.5):
    """(best_hours, worst_hours, swing_db) over confident bins, or None if
    fewer than 3 confident bins exist (fresh install honesty)."""
    c = curve(rf, rows, ant, now, half_life)
    ok = {h: b["mer"] for h, b in c.items() if b["conf"] in ("solid", "thin")}
    if len(ok) < 3:
        return None
    top, bot = max(ok.values()), min(ok.values())
    best = sorted(h for h, m in ok.items() if m >= top - within_db)
    worst = sorted(h for h, m in ok.items() if m <= bot + within_db)
    solid = [m for h, m in ok.items() if c[h]["conf"] == "solid"]
    swing = round((max(solid) - min(solid)) if len(solid) >= 2 else top - bot, 1)
    return best, worst, swing


def hint(rf, rows, now_hour=None, now=None, half_life=HALF_LIFE_DAYS):
    """Guide hint: 'usually better around 19h' — or None. Fires only when
    the current hour is confidently known AND a solid hour beats it by
    HINT_DB. Silence over guessing."""
    now = now or datetime.now()
    now_hour = now.hour if now_hour is None else now_hour
    c = curve(rf, rows, None, now, half_life)
    here = c[now_hour]
    if here["conf"] not in ("solid", "thin"):
        return None
    solid = {h: b["mer"] for h, b in c.items() if b["conf"] == "solid"}
    if not solid:
        return None
    bh = max(solid, key=lambda h: solid[h])
    if solid[bh] - here["mer"] >= HINT_DB:
        return "usually better around %02dh (%.1f vs %.1f dB now)" % (
            bh, solid[bh], here["mer"])
    return None


def antenna_by_hour(rf, rows, now=None, half_life=HALF_LIFE_DAYS, min_n=THIN_N):
    """Learned ownership: {hour: (ant_label, mer)} for hours where some
    antenna label has >= min_n samples; label with best weighted-median MER
    wins. Labels are whatever this rig logged — never hardcoded."""
    now = now or datetime.now()
    per = {}  # (hour, ant) -> [(mer, w)]
    for r in rows:
        if r["rf"] != rf or r["mer"] is None:
            continue
        per.setdefault((r["ts"].hour, r["ant"]), []).append(
            (r["mer"], _weight(r["ts"], now, half_life)))
    out = {}
    for (h, ant), mw in per.items():
        if len(mw) < min_n:
            continue
        m = _wmedian(mw)
        if h not in out or m > out[h][1]:
            out[h] = (ant, round(m, 2))
    return out


# --------------------------------------------------------------------------
# presentation (NERD card)
# --------------------------------------------------------------------------

_BARS = " .:-=+*#%@"

def nerd_card(rf, rows, now=None, half_life=HALF_LIFE_DAYS):
    """Text block: 24-hour MER sparkline ('?' = unknown), best/worst hours,
    confidence, learned antenna ownership where it varies."""
    now = now or datetime.now()
    c = curve(rf, rows, None, now, half_life)
    mers = [b["mer"] for b in c.values() if b["mer"] is not None]
    lines = ["RF%d hour profile (half-life %.0fd, %s)" % (
        rf, half_life, now.strftime("%Y-%m-%d"))]
    if not mers:
        lines.append("  no history yet — watch or scan and I'll learn")
        return "\n".join(lines)
    lo, hi = min(mers), max(mers)
    span = max(hi - lo, 0.1)
    spark = "".join(
        "?" if c[h]["mer"] is None else
        _BARS[min(int((c[h]["mer"] - lo) / span * (len(_BARS) - 1)),
                  len(_BARS) - 1)]
        for h in range(24))
    conf_row = "".join(
        {"solid": "S", "thin": "t", "trace": ".", "borrowed": "b",
         None: " "}[c[h]["conf"]] for h in range(24))
    lines.append("  hour 0-23  [%s]  %.1f..%.1f dB" % (spark, lo, hi))
    lines.append("  confidence [%s]  S=solid t=thin .=trace b=borrowed" % conf_row)
    bh = best_hours(rf, rows, None, now, half_life)
    if bh:
        best, worst, swing = bh
        lines.append("  best %s  worst %s  swing %.1f dB" % (
            ",".join("%02dh" % h for h in best),
            ",".join("%02dh" % h for h in worst), swing))
    own = antenna_by_hour(rf, rows, now, half_life)
    if len(set(a for a, _ in own.values())) > 1:
        runs, cur, start = [], None, None
        for h in range(24):
            a = own.get(h, (None,))[0]
            if a != cur:
                if cur:
                    runs.append("%02d-%02dh:%s" % (start, h - 1, cur))
                cur, start = a, h
        if cur:
            runs.append("%02d-%02dh:%s" % (start, 23, cur))
        lines.append("  antenna by hour: " + "  ".join(r for r in runs
                                                       if ":None" not in r))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# BACKFILL — one-time harvest of the historical logs (Task A miners).
# In the live design each producing tool calls record() itself; this section
# exists so day 1 of the time knob starts with a week of history instead of
# nothing. Site-specific filenames live HERE ONLY, never in the model above.
# --------------------------------------------------------------------------

def _j(line):
    try:
        return json.loads(line)
    except ValueError:
        return None


def _iter_timeonly(path, anchor, wrap_h=6):
    """Yield (datetime, rowdict) for jsonl with time-of-day-only 't' fields.
    Date inferred: starts at `anchor`, +1 day when time jumps back > wrap_h
    hours (midnight crossing). Hour-of-day is exact; date is best-effort."""
    cur, prev = anchor, None
    for line in open(path, encoding="utf-8", errors="ignore"):
        d = _j(line)
        if not d or "t" not in d:
            continue
        try:
            t = datetime.strptime(d["t"], "%H:%M:%S").time()
        except ValueError:
            continue
        s = t.hour * 3600 + t.minute * 60 + t.second
        if prev is not None and prev - s > wrap_h * 3600:
            cur += timedelta(days=1)
        prev = s
        yield datetime.combine(cur, t), d


def harvest(root=HERE, tvt=None):
    """Return normalized rows from every known historical source."""
    tvt = tvt or Path(os.path.expanduser("~")) / ".tv_tuner"
    lab = root / "lab"
    rows = []

    def add(ts, rf, ant, mer, loss, src, date_known=True):
        if rf is None or (mer is None and loss is None):
            return
        rows.append(dict(ts=ts.replace(microsecond=0), rf=int(rf),
                         ant=ant or "?", mer=mer, loss_pct=loss,
                         source=src, date_known=date_known))

    # -- flight_recorder.jsonl: panel meter/watch telemetry (iso, 10 s)
    fr = lab / "flight_recorder.jsonl"
    fr_min_rf = {}
    if fr.exists():
        for line in open(fr, encoding="utf-8", errors="ignore"):
            d = _j(line)
            if not d or "iso" not in d or d.get("rf") is None:
                continue
            ts = datetime.fromisoformat(d["iso"])
            ant = (d.get("knobs") or {}).get("ANTENNA")
            add(ts, d["rf"], ant, d.get("mer_db"), d.get("loss_pct"),
                "flight_recorder")
            fr_min_rf[ts.strftime("%Y-%m-%d %H:%M")] = int(d["rf"])

    # -- time-of-day-only lab logs (anchor dates from session forensics,
    #    see lab/time_knob_research.md "date inference")
    for fn, anchor, src in (("cube_log.jsonl", date(2026, 7, 5), "cube_log"),
                            ("night_lab.jsonl", date(2026, 7, 7), "night_lab"),
                            ("day_lab.jsonl", date(2026, 7, 7), "day_lab"),
                            ("ui_lab.jsonl", date(2026, 7, 9), "ui_lab")):
        p = root / fn
        if not p.exists():
            continue
        for ts, d in _iter_timeonly(p, anchor):
            # cube_log 'start' events carry an absolute 'until' date: re-anchor
            if d.get("event") == "start" and "until" in d:
                try:
                    u = datetime.fromisoformat(d["until"])
                    nd = u.date() - timedelta(
                        days=1 if ts.time() > u.time() else 0)
                    ts = datetime.combine(nd, ts.time())
                except ValueError:
                    pass
            if d.get("rf") is not None and d.get("mer_med") is not None:
                add(ts, d["rf"], d.get("ant"), d["mer_med"], None, src,
                    date_known=False)

    # -- overnight / evening / stress soaks (iso timestamps)
    p = lab / "overnight_20260704_2141.jsonl"
    if p.exists():
        for line in open(p, encoding="utf-8", errors="ignore"):
            d = _j(line)
            if d and "iso" in d and d.get("rf") is not None \
                    and d.get("mer_db") is not None:
                add(datetime.fromisoformat(d["iso"]), d["rf"], d.get("ant"),
                    d["mer_db"], None, "overnight_0704")
    p = lab / "evening_lab_20260704_1935.jsonl"
    if p.exists():
        expmap = {}
        for line in open(p, encoding="utf-8", errors="ignore"):
            d = _j(line)
            if not d:
                continue
            if d.get("event") == "exp" and isinstance(d.get("exp"), str):
                m = re.match(r"(\d+)_rf(\d+)", d["exp"])
                if m:
                    expmap[int(m.group(1))] = int(m.group(2))
            elif "mer_db" in d and isinstance(d.get("exp"), int) and "iso" in d:
                add(datetime.fromisoformat(d["iso"]), expmap.get(d["exp"]),
                    None, d["mer_db"], None, "evening_lab_0704")

    # -- drizzle_watch.csv: per-minute panel-chain MER+loss; rf joined from
    #    the flight recorder's same-minute row (both watch the same chain)
    p = lab / "drizzle_watch.csv"
    if p.exists():
        for r in csv.DictReader(open(p, encoding="utf-8", errors="ignore")):
            if not r.get("mer_med"):
                continue
            try:
                ts = datetime.strptime(r["time"], "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            rf = next((fr_min_rf.get(
                (ts - timedelta(minutes=o)).strftime("%Y-%m-%d %H:%M"))
                for o in range(3) if fr_min_rf.get(
                    (ts - timedelta(minutes=o)).strftime("%Y-%m-%d %H:%M"))),
                None)
            if rf:
                add(ts, rf, None, float(r["mer_med"]),
                    float(r["bad_pct"]) if r.get("bad_pct") else None,
                    "drizzle_watch")

    # -- scanner snapshots (~/.tv_tuner/scan*.json): mer_med on locked chans
    if tvt.exists():
        for p in sorted(tvt.glob("scan*.json")):
            try:
                d = json.load(open(p, encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if "scanned_at" not in d:
                continue
            ts = datetime.fromisoformat(d["scanned_at"])
            for c in d.get("channels", []):
                if c.get("lock") and c.get("mer_med") is not None:
                    add(ts, c["rf"], None, c["mer_med"], None, "scan")

    rows.sort(key=lambda r: r["ts"])
    return rows


def backfill(path=HISTORY):
    if path.exists():
        print("refusing: %s exists (append-only; delete it to re-backfill)"
              % path)
        return 0
    rows = harvest()
    for r in rows:
        record(r, path)
    print("backfilled %d rows -> %s" % (len(rows), path))
    return len(rows)


# --------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------

def selftest():
    import tempfile
    now = datetime(2026, 7, 10, 12, 0, 0)
    ok = 0

    def chk(cond, msg):
        nonlocal ok
        assert cond, "FAIL: " + msg
        ok += 1
        print("  ok -", msg)

    # 1. fresh install: no file -> empty, everything unknown, nothing crashes
    empty = load(Path(tempfile.gettempdir()) / "no_such_history_xyz.csv")
    chk(empty == [], "missing history loads as []")
    c = curve(9, empty, now=now)
    chk(all(b["conf"] is None for b in c.values()), "fresh install: all unknown")
    chk(best_hours(9, empty, now=now) is None, "fresh install: no best_hours")
    chk(hint(9, empty, now=now) is None, "fresh install: no hint")
    chk("no history yet" in nerd_card(9, empty, now=now),
        "fresh install: honest card")

    # 2. record/load roundtrip
    tmp = Path(tempfile.mkdtemp()) / "hist.csv"
    record(dict(ts=now, rf=9, ant="whatever the rig calls it", mer=17.2,
                loss_pct=0.5, source="test"), tmp)
    record(dict(ts=now, rf=9, ant="x", mer=None, loss_pct=None,
                source="test"), tmp)  # no-metric row must be rejected
    rows = load(tmp)
    chk(len(rows) == 1 and rows[0]["mer"] == 17.2 and
        rows[0]["ant"] == "whatever the rig calls it",
        "record/load roundtrip, opaque antenna label, empty row rejected")

    # 3. recency decay: 5 stale-good vs 5 fresh-bad at same hour -> fresh wins
    mk = lambda dt, mer, h=19: dict(ts=dt.replace(hour=h), rf=9, ant="a",
                                    mer=mer, loss_pct=None, source="t")
    stale = [mk(now - timedelta(days=60), 19.0) for _ in range(5)]
    fresh = [mk(now - timedelta(days=d), 13.0) for d in range(1, 6)]
    m = curve(9, stale + fresh, now=now)[19]["mer"]
    chk(m == 13.0, "60-day-old rows lose to fresh rows (half-life decay)")

    # 4. single-day bins are never solid
    one_day = [mk(now - timedelta(seconds=i), 15.0) for i in range(20)]
    chk(curve(9, one_day, now=now)[19]["conf"] == "thin",
        "20 samples on ONE day -> thin, not solid (RF31 lesson)")

    # 5. neighbor borrowing is labeled
    two_days = [mk(now - timedelta(days=d), 16.0, h=10)
                for d in (1, 2, 3, 4, 5)]
    c = curve(9, two_days, now=now)
    chk(c[11]["conf"] == "borrowed" and c[11]["mer"] == 16.0 and
        c[12]["conf"] is None, "empty hour borrows h±1, labeled; h+2 unknown")

    # 6. hint fires only on a confident >=2 dB gap
    good_evening = [mk(now - timedelta(days=d), 18.5, h=19)
                    for d in (1, 2, 3) for _ in range(3)]
    bad_noon = [mk(now - timedelta(days=d), 14.0, h=12)
                for d in (1, 2, 3) for _ in range(3)]
    h = hint(9, good_evening + bad_noon, now_hour=12, now=now)
    chk(h is not None and "19h" in h, "hint fires: noon 14.0 -> 'better at 19h'")
    chk(hint(9, good_evening + bad_noon, now_hour=19, now=now) is None,
        "no hint at the best hour")
    chk(hint(9, good_evening, now_hour=12, now=now) is None,
        "no hint when current hour is unknown (silence over guessing)")

    # 7. learned antenna ownership, opaque labels
    ant_b = [dict(ts=now - timedelta(days=d), rf=7, ant="Antenna B", mer=17.5,
                  loss_pct=None, source="t") for d in (1, 2, 3)]
    disc = [dict(ts=(now - timedelta(days=d)).replace(hour=3), rf=7,
                 ant="port-with-any-name", mer=14.5, loss_pct=None,
                 source="t") for d in (1, 2, 3)]
    own = antenna_by_hour(7, ant_b + disc, now=now)
    chk(own[12][0] == "Antenna B" and own[3][0] == "port-with-any-name",
        "per-hour ownership learned from opaque labels")

    print("selftest: %d checks passed" % ok)


# --------------------------------------------------------------------------

def _main(argv):
    cmd = argv[1] if len(argv) > 1 else "report"
    if cmd == "selftest":
        selftest()
        return
    if cmd == "backfill":
        backfill()
        return
    rows = load()
    if not rows:
        # no history file yet: run from a live harvest so `report` is useful
        # even before the user commits to a backfill
        rows = harvest()
        print("(no %s — reporting from a fresh harvest of %d rows; "
              "run `backfill` to persist)\n" % (HISTORY.name, len(rows)))
    if cmd == "card":
        print(nerd_card(int(argv[2]), rows))
        return
    if cmd == "hint":
        rf = int(argv[2])
        h = int(argv[3]) if len(argv) > 3 else None
        print(hint(rf, rows, now_hour=h) or "(no hint)")
        return
    # report
    rfs = ([int(a) for a in argv[2:]] if len(argv) > 2
           else sorted(set(r["rf"] for r in rows)))
    for rf in rfs:
        print(nerd_card(rf, rows))
        print()


if __name__ == "__main__":
    _main(sys.argv)
