#!/usr/bin/env python
"""time_knob_prior.py — expected-MER prior for never-visited channels
(PROTOTYPE — imported by NOTHING; wired into nothing).

Research: lab/antenna_fingerprint_research.md (2026-07-11). Summary of
what the data licensed, and what it refused:

  * LICENSED: antennas have a stable BAND-REGIME fingerprint (VHF vs UHF).
    On this rig the split is huge (rabbit/A is ~-3 dB relative at VHF,
    philips/B ~+3 dB) and sign-stable across days. Transferring the
    antenna's same-regime mean to an unseen channel cut leave-one-out
    MER error from 2.0 -> 1.6 dB overall, and 2.5 -> 0.7 dB on VHF.
  * REFUSED: within a regime, per-channel "territories" (+/-3.5 dB,
    e.g. rabbit OWNS RF21, philips owns RF15) dominate and do NOT
    transfer — a frequency-distance kernel did NOT beat the plain
    regime mean, and best-antenna prediction for unseen channels lost
    to "always suggest the globally best antenna" (2/7 vs 5/7).
  * REFUSED: hour-curve shapes are channel-specific (same-antenna
    cross-channel r ~= +0.08); the prior carries NO hour structure.

So this module is deliberately minimal: the validated regime-mean
transfer with honest confidence, and an antenna suggester that admits
a toss-up whenever the gap is inside territory range.

Public API (pure functions over time_knob-style row dicts —
ts: datetime, rf: int, ant: str (opaque), mer: float|None):

    prior(rf, ant, rows)  -> {mer_estimate, confidence, basis,
                              expected_mae_db, pool_spread_db} | None
    suggest(rf, rows)     -> ranked [(ant, estimate, confidence)],
                              plus decisive flag — see docstring

Where it WOULD plug in (NOT wired — decisions for a live session):
  1. Guide badge for a never-visited channel: "expect ~16 dB on
     Antenna B (prior, +/-2 dB)" instead of a blank.
  2. Deep-tune starting antenna: order the antenna trial loop by
     suggest() instead of port order (saves one full antenna trial
     when the regimes differ; admits toss-up inside one regime).
  3. Fast-tune qualification: a never-visited channel whose prior is
     confidently above the cliff (>= 15.2 + expected_mae_db) could
     take the MEMORY-TUNE fast path immediately instead of earning it
     over N visits. The +/- is honest: a channel predicted 16.0 +/- 2.0
     does NOT qualify; 18.5 +/- 1.6 does.

CLI:
    python time_knob_prior.py selftest    # synthetic checks, no files
    python time_knob_prior.py loo         # leave-one-out on the real
                                          # lab/quality_history.csv
"""
import csv
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
HISTORY = HERE / "lab" / "quality_history.csv"

HALF_LIFE_DAYS = 14.0    # same recency law as time_knob.py
MIN_BIN_N = 3            # samples for an hour bin to count
MIN_CELL_N = 30          # rows for a (rf, ant) cell to feed the prior
MIN_CELL_DAYS = 2        # distinct days for a cell to feed the prior
TERRITORY_DB = 3.5       # observed per-channel antenna-territory range;
                         # rankings inside this gap are a TOSS-UP
MAE_SOLID = 1.6          # validated LOO MAE, >=2 same-regime channels
MAE_FALLBACK = 2.5       # validated LOO error scale for fallback paths


def _regime(rf):
    return "VHF" if rf < 14 else "UHF"


def _weight(ts, now):
    age = max(0.0, (now - ts).total_seconds() / 86400.0)
    return 0.5 ** (age / HALF_LIFE_DAYS)


def _wmedian(pairs):
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


def _cells(rows, now=None):
    """(rf, ant) -> (hour-balanced weighted-median MER, n_rows, n_days).
    Hour-balancing (median of hour-bin medians) removes the 'this cell
    was only ever sampled at 3 am' bias. Only solid cells qualify."""
    now = now or datetime.now()
    bins = defaultdict(lambda: defaultdict(list))   # (rf,ant) -> hour -> [(v,w)]
    days = defaultdict(set)
    for r in rows:
        if r.get("mer") is None:
            continue
        k = (r["rf"], r["ant"])
        bins[k][r["ts"].hour].append((r["mer"], _weight(r["ts"], now)))
        days[k].add(r["ts"].date())
    out = {}
    for k, hb in bins.items():
        meds = [_wmedian(v) for v in hb.values() if len(v) >= MIN_BIN_N]
        n = sum(len(v) for v in hb.values())
        if meds and n >= MIN_CELL_N and len(days[k]) >= MIN_CELL_DAYS:
            m = sorted(meds)
            stat = (m[len(m) // 2] if len(m) % 2
                    else 0.5 * (m[len(m) // 2 - 1] + m[len(m) // 2]))
            out[k] = (stat, n, len(days[k]))
    return out


def prior(rf, ant, rows, now=None, _cells_cache=None):
    """Transfer prior for channel `rf` on antenna `ant`, built ONLY from
    OTHER channels (rows for rf itself are excluded — if the channel has
    real history, use time_knob.curve, not this).

    Returns None (nothing known about this antenna) or dict:
      mer_estimate      dB, the validated regime-mean transfer
      confidence        "solid" | "thin" | "fallback"
      basis             human-readable provenance string
      expected_mae_db   honest error bar from leave-one-out validation
      pool_spread_db    stdev of the pool — territory-size honesty
    """
    cells = _cells_cache if _cells_cache is not None else _cells(rows, now)
    reg = _regime(rf)
    pool = [(r, c[0], c[2]) for (r, a), c in cells.items()
            if a == ant and r != rf and _regime(r) == reg]
    if pool:
        vals = [v for _, v, _ in pool]
        est = sum(vals) / len(vals)
        mean = est
        spread = (math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
                  if len(vals) > 1 else 0.0)
        solid = len(pool) >= 2 and all(d >= 2 for _, _, d in pool)
        return dict(
            mer_estimate=round(est, 1),
            confidence="solid" if solid else "thin",
            basis="%s mean of RF%s on %s" % (
                reg, ",".join(str(r) for r, _, _ in sorted(pool)), ant),
            expected_mae_db=MAE_SOLID if solid else MAE_FALLBACK,
            pool_spread_db=round(spread, 1))
    # regime never seen on this antenna: antenna mean + cross-antenna
    # regime offset (validated fallback path, e.g. VHF prior for an
    # antenna only ever used on UHF)
    mine = [c[0] for (r, a), c in cells.items() if a == ant and r != rf]
    if not mine:
        return None
    offs = []
    for a2 in set(a for _, a in cells):
        if a2 == ant:
            continue
        pin = [c[0] for (r, a), c in cells.items()
               if a == a2 and r != rf and _regime(r) == reg]
        pout = [c[0] for (r, a), c in cells.items()
                if a == a2 and r != rf and _regime(r) != reg]
        if pin and pout:
            offs.append(sum(pin) / len(pin) - sum(pout) / len(pout))
    off = sum(offs) / len(offs) if offs else 0.0
    return dict(
        mer_estimate=round(sum(mine) / len(mine) + off, 1),
        confidence="fallback",
        basis="%s mean %+.1f dB cross-antenna %s offset" % (ant, off, reg),
        expected_mae_db=MAE_FALLBACK,
        pool_spread_db=None)


def suggest(rf, rows, now=None):
    """Ranked antenna suggestion for a never-visited channel.

    Returns (ranked, decisive):
      ranked   [(ant, mer_estimate, confidence)] best-first
      decisive True only when the top gap exceeds TERRITORY_DB —
               validation showed per-channel territories flip rankings
               inside that range, so a small gap is a TOSS-UP and the
               caller should just start with its globally best antenna.
    """
    cells = _cells(rows, now)
    ants = sorted(set(a for _, a in cells))
    ranked = []
    for a in ants:
        p = prior(rf, a, rows, now, _cells_cache=cells)
        if p:
            ranked.append((a, p["mer_estimate"], p["confidence"]))
    ranked.sort(key=lambda t: -t[1])
    decisive = (len(ranked) >= 2
                and ranked[0][1] - ranked[1][1] > TERRITORY_DB
                and ranked[0][2] != "fallback")
    return ranked, decisive


# ---------------------------------------------------------------- LOO rig

def _load_history(path=HISTORY):
    rows = []
    if not Path(path).exists():
        return rows
    merge = {"rabbit": "A", "Antenna A": "A", "philips": "B",
             "Antenna B": "B", "discone": "C", "Antenna C": "C"}
    with open(path, newline="", encoding="utf-8", errors="ignore") as f:
        for r in csv.DictReader(f):
            if not r["mer"] or r["ant"] not in merge:
                continue
            try:
                rows.append(dict(ts=datetime.fromisoformat(r["ts"]),
                                 rf=int(r["rf"]), ant=merge[r["ant"]],
                                 mer=float(r["mer"])))
            except ValueError:
                continue
    return [r for r in rows if r["rf"] != 27]     # n=5, useless


def loo(rows=None, now=None, verbose=True):
    """Leave-one-CHANNEL-out over the real history: for every solid
    (rf, ant) cell, hide ALL rows of that rf and predict via prior().
    Returns (model_mae, baseline_mae). Baseline = antenna median."""
    rows = _load_history() if rows is None else rows
    if not rows:
        print("no history at %s — nothing to validate" % HISTORY)
        return None, None
    now = now or max(r["ts"] for r in rows)
    cells = _cells(rows, now)
    ants = sorted(set(a for _, a in cells))
    ant_med = {}
    for a in ants:
        v = sorted(c[0] for (r, x), c in cells.items() if x == a)
        ant_med[a] = v[len(v) // 2] if v else None
    em, eb = [], []
    for (rf, a), truth in sorted(cells.items()):
        held = [r for r in rows if r["rf"] != rf]
        p = prior(rf, a, held, now)
        if p is None:
            continue
        em.append(abs(p["mer_estimate"] - truth[0]))
        eb.append(abs(ant_med[a] - truth[0]))
        if verbose:
            print("  RF%-2d %s: truth %5.1f  prior %5.1f (%+.1f)  "
                  "ant-med %5.1f (%+.1f)  [%s/%s]" % (
                      rf, a, truth[0], p["mer_estimate"],
                      p["mer_estimate"] - truth[0], ant_med[a],
                      ant_med[a] - truth[0], p["confidence"], p["basis"]))
    mm = sum(em) / len(em) if em else float("nan")
    mb = sum(eb) / len(eb) if eb else float("nan")
    print("LOO scorecard: prior MAE %.2f dB vs antenna-median baseline "
          "%.2f dB (n=%d cells)" % (mm, mb, len(em)))
    return mm, mb


# ---------------------------------------------------------------- selftest

def selftest():
    ok = 0

    def chk(cond, msg):
        nonlocal ok
        assert cond, "FAIL: " + msg
        ok += 1
        print("  ok -", msg)

    now = datetime(2026, 7, 11, 12, 0)

    def mk(rf, ant, mer, day, hour):
        return dict(ts=now - timedelta(days=day, hours=now.hour - hour),
                    rf=rf, ant=ant, mer=mer)

    def cell_rows(rf, ant, mer):
        # 2 days x 3 hours x 6 samples = solid cell
        return [mk(rf, ant, mer + 0.1 * (i % 3 - 1), d, h)
                for d in (1, 2) for h in (9, 14, 20) for i in range(6)]

    # rig: antenna "portX" great at UHF (18) bad at VHF (11),
    #      antenna "portY" the reverse (12 / 17). Opaque labels.
    rows = (cell_rows(34, "portX", 18.0) + cell_rows(36, "portX", 18.4)
            + cell_rows(8, "portX", 11.0)
            + cell_rows(34, "portY", 12.0) + cell_rows(36, "portY", 12.4)
            + cell_rows(8, "portY", 17.0) + cell_rows(10, "portY", 17.4))

    # 1. regime transfer: unseen UHF channel inherits the UHF mean
    p = prior(31, "portX", rows, now)
    chk(p and abs(p["mer_estimate"] - 18.2) < 0.3 and
        p["confidence"] == "solid", "unseen UHF channel gets UHF mean, solid")
    chk("34" in p["basis"] and "36" in p["basis"], "basis names the pool")

    # 2. regime split respected: unseen VHF channel does NOT see UHF cells
    p = prior(9, "portY", rows, now)
    chk(p and abs(p["mer_estimate"] - 17.2) < 0.3,
        "unseen VHF channel pools VHF only")
    chk(prior(9, "portX", rows, now)["confidence"] == "thin",
        "single-channel pool is thin, not solid")

    # 3. the prior never peeks at the channel itself
    spiked = rows + cell_rows(31, "portX", 5.0)
    p = prior(31, "portX", spiked, now)
    chk(abs(p["mer_estimate"] - 18.2) < 0.3,
        "rows for the target rf are excluded (pure transfer)")

    # 4. fallback: regime never seen on this antenna -> cross-antenna offset
    # portZ only ever saw UHF (15.0). portY says VHF runs ~+5.2 dB above
    # UHF (17.4 vs mean 12.2), so the VHF prior should land near 20.2.
    p = prior(8, "portZ", rows + cell_rows(34, "portZ", 15.0), now)
    chk(p and p["confidence"] == "fallback" and
        abs(p["mer_estimate"] - 20.2) < 0.4,
        "unseen regime falls back to antenna mean + learned regime offset")

    # 5. thin data: unknown antenna -> honest None
    chk(prior(31, "portQ", rows, now) is None, "unknown antenna -> None")
    chk(prior(31, "portX", [], now) is None, "no rows -> None")

    # 6. suggest(): decisive across a regime flip, toss-up inside one
    ranked, decisive = suggest(9, rows, now)
    chk(ranked[0][0] == "portY" and decisive,
        "VHF suggestion flips to portY, decisive (gap > territory)")
    close = (cell_rows(34, "pA", 17.0) + cell_rows(36, "pA", 17.4)
             + cell_rows(34, "pB", 16.0) + cell_rows(36, "pB", 16.4))
    ranked, decisive = suggest(31, close, now)
    chk(not decisive, "1 dB gap inside a regime = toss-up (territory law)")

    # 7. single-day cells never feed the prior (RF31 lesson)
    one_day = [mk(34, "pS", 18.0, 1, h) for h in range(10)] * 4
    chk(prior(31, "pS", one_day, now) is None,
        "single-day cells are not solid enough to transfer")

    print("selftest: %d checks passed" % ok)

    # real-data leave-one-out, when the history is present
    if HISTORY.exists():
        print("\nreal-data leave-one-channel-out (lab/quality_history.csv):")
        mm, mb = loo(verbose=True)
        if mm is not None:
            assert mm < mb, ("FAIL: prior (%.2f) must beat antenna-median "
                             "baseline (%.2f) on this rig's history"
                             % (mm, mb))
            print("  ok - prior beats antenna-median baseline "
                  "(%.2f < %.2f dB MAE)" % (mm, mb))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if cmd == "selftest":
        selftest()
    elif cmd == "loo":
        loo()
    else:
        print(__doc__)
