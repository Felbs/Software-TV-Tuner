#!/usr/bin/env python
"""antenna_fingerprint_analysis.py — offline research: do antennas share
structure across channels (frequency response + tower identity), and can
the time-knob transfer knowledge to never-visited channels?

Pure stdlib, read-only over lab/quality_history.csv. Outputs text tables
to stdout; the write-up lives in lab/antenna_fingerprint_research.md.

Run:  python lab/antenna_fingerprint_analysis.py [--unmerged]
"""
import csv
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
HISTORY = HERE / "quality_history.csv"

# ---------------------------------------------------------------- data prep

MERGE = {"rabbit": "A", "Antenna A": "A",
         "philips": "B", "Antenna B": "B",
         "discone": "C", "Antenna C": "C"}

def freq_mhz(rf):
    return (177 + (rf - 7) * 6) if rf < 14 else (473 + (rf - 14) * 6)

def regime(rf):
    return "VHF" if rf < 14 else "UHF"

# Tower map — ASSUMPTION, no explicit site data exists in the repo.
# DC-market geography from callsigns in ~/.tv_tuner/scan.json:
#   Tenleytown/NW-DC cluster: 7 WJLA, 9 WUSA, 15 WETA, 34 WRC, 35 WDCA,
#                             36 WTTG, 21 WDCW(DC half)
#   Fairfax/Manassas: 31 WPXW
#   RF21 is AMBIGUOUS: Baltimore MPT is co-channel and has been decoded here.
TOWER = {7: "TEN", 9: "TEN", 15: "TEN", 34: "TEN", 35: "TEN", 36: "TEN",
         21: "TEN?", 31: "FFX"}

def load(merged=True):
    rows = []
    with open(HISTORY, newline="", encoding="utf-8", errors="ignore") as f:
        for r in csv.DictReader(f):
            if not r["mer"]:
                continue
            rf = int(r["rf"])
            if rf == 27:            # n=5, useless
                continue
            ant = r["ant"]
            if ant == "?":
                continue
            if merged:
                ant = MERGE.get(ant)
                if ant is None:
                    continue
            try:
                ts = datetime.fromisoformat(r["ts"])
            except ValueError:
                continue
            rows.append((ts, rf, ant, float(r["mer"])))
    return rows

# ------------------------------------------------------------- basic stats

def median(v):
    s = sorted(v)
    n = len(s)
    return None if not n else (s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2]))

def pearson(x, y):
    n = len(x)
    if n < 3:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((a - my) ** 2 for a in y))
    if sx < 1e-9 or sy < 1e-9:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)

def spearman(x, y):
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        rk = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk
    return pearson(rank(x), rank(y))

# -------------------------------------------------- hour curves & cell stats

MIN_BIN_N = 3      # samples for an hour bin to count
MIN_COMMON = 6     # common hour bins needed for a curve correlation
MIN_CELL_N = 30    # rows for a (rf, ant) cell to be "solid"
MIN_CELL_DAYS = 2  # distinct days for a solid cell

def hour_curve(rows, rf, ant):
    """hour -> median MER (bins with >= MIN_BIN_N samples only)."""
    bins = defaultdict(list)
    for ts, r, a, m in rows:
        if r == rf and a == ant:
            bins[ts.hour].append(m)
    return {h: median(v) for h, v in bins.items() if len(v) >= MIN_BIN_N}

def cell_stat(rows, rf, ant):
    """Hour-balanced median MER for a cell: median of hour-bin medians.
    Removes the 'this cell was only sampled at 3am' bias. Returns
    (stat, n_rows, n_days, n_bins) or None."""
    c = hour_curve(rows, rf, ant)
    sub = [(ts, m) for ts, r, a, m in rows if r == rf and a == ant]
    if not c or len(sub) < MIN_CELL_N:
        return None
    days = len(set(ts.date() for ts, _ in sub))
    if days < MIN_CELL_DAYS:
        return None
    return (median(list(c.values())), len(sub), days, len(c))

def curve_corr(c1, c2):
    common = sorted(set(c1) & set(c2))
    if len(common) < MIN_COMMON:
        return None, len(common)
    return pearson([c1[h] for h in common], [c2[h] for h in common]), len(common)

# ------------------------------------------------------------------ tasks

def task1_curve_correlations(rows, ants, rfs):
    print("\n== A1: hour-curve correlations ==")
    curves = {(rf, a): hour_curve(rows, rf, a) for rf in rfs for a in ants}
    same_ant, cross_ant = [], []
    for a in ants:
        pairs = []
        for i, r1 in enumerate(rfs):
            for r2 in rfs[i + 1:]:
                c, n = curve_corr(curves[(r1, a)], curves[(r2, a)])
                if c is not None:
                    pairs.append((r1, r2, c, n))
                    same_ant.append(c)
        if pairs:
            print(f"  antenna {a}: same-antenna channel-pair correlations")
            for r1, r2, c, n in sorted(pairs, key=lambda p: -p[2]):
                print(f"    RF{r1:>2} x RF{r2:>2}  r={c:+.2f}  ({n} common hour bins)")
    for a1 in ants:
        for a2 in ants:
            if a1 >= a2:
                continue
            for i, r1 in enumerate(rfs):
                for r2 in rfs:
                    if r1 == r2:
                        continue
                    c, n = curve_corr(curves[(r1, a1)], curves[(r2, a2)])
                    if c is not None:
                        cross_ant.append(c)
    def summ(v):
        return (f"mean {sum(v)/len(v):+.2f}  median {median(v):+.2f}  n={len(v)}"
                if v else "n=0")
    print(f"  SAME-antenna cross-channel:  {summ(same_ant)}")
    print(f"  CROSS-antenna cross-channel: {summ(cross_ant)}")
    return same_ant, cross_ant, curves


def task2_freq_smoothness(rows, ants, rfs):
    print("\n== A2: frequency smoothness |dMER| vs |df| ==")
    out = {}
    for a in ants:
        cells = {rf: cell_stat(rows, rf, a) for rf in rfs}
        cells = {rf: c for rf, c in cells.items() if c}
        prs_same, prs_cross = [], []
        for i, r1 in enumerate(sorted(cells)):
            for r2 in sorted(cells)[i + 1:]:
                dm = abs(cells[r1][0] - cells[r2][0])
                df = abs(freq_mhz(r1) - freq_mhz(r2))
                (prs_same if regime(r1) == regime(r2) else prs_cross).append(
                    (df, dm, r1, r2))
        if len(prs_same) >= 4:
            rho = spearman([p[0] for p in prs_same], [p[1] for p in prs_same])
            print(f"  antenna {a}: within-regime pairs n={len(prs_same)}  "
                  f"spearman(|df|,|dMER|)={rho:+.2f}" if rho is not None else
                  f"  antenna {a}: degenerate")
            for df, dm, r1, r2 in sorted(prs_same):
                print(f"    RF{r1:>2}-RF{r2:<2} df={df:>3.0f} MHz  |dMER|={dm:4.1f} dB")
            out[a] = rho
        if prs_cross:
            dmc = [p[1] for p in prs_cross]
            print(f"    cross-regime (VHF-UHF) pairs n={len(prs_cross)}  "
                  f"|dMER| mean={sum(dmc)/len(dmc):.1f} dB")
    return out


def task3_tower(rows, ants, rfs, curves, include_21=True):
    print(f"\n== A3: tower sharing (RF21 {'IN' if include_21 else 'OUT'}) ==")
    same_site, diff_site = [], []
    for a in ants:
        for i, r1 in enumerate(rfs):
            for r2 in rfs[i + 1:]:
                if regime(r1) != regime(r2):
                    continue
                if not include_21 and 21 in (r1, r2):
                    continue
                t1 = TOWER.get(r1, "?").rstrip("?")
                t2 = TOWER.get(r2, "?").rstrip("?")
                c, n = curve_corr(curves[(r1, a)], curves[(r2, a)])
                if c is None:
                    continue
                df = abs(freq_mhz(r1) - freq_mhz(r2))
                (same_site if t1 == t2 else diff_site).append((c, df, a, r1, r2))
    for name, v in (("same-site", same_site), ("diff-site", diff_site)):
        if v:
            cs = [x[0] for x in v]
            dfs = [x[1] for x in v]
            print(f"  {name}: n={len(v)}  mean r={sum(cs)/len(cs):+.2f}  "
                  f"median r={median(cs):+.2f}  mean |df|={sum(dfs)/len(dfs):.0f} MHz")
        else:
            print(f"  {name}: n=0")
    for c, df, a, r1, r2 in sorted(diff_site, key=lambda x: -x[0]):
        print(f"    diff-site pair ant {a} RF{r1}xRF{r2} r={c:+.2f} df={df:.0f}")
    return same_site, diff_site


def task4_fingerprints(rows, ants, rfs):
    print("\n== A4: antenna fingerprints (relative MER vs channel mean) ==")
    cells = {(rf, a): cell_stat(rows, rf, a) for rf in rfs for a in ants}
    rel = defaultdict(list)   # (ant, band) -> [rel dB]
    print("  per-channel table (hour-balanced median MER, solid cells only):")
    hdr = "  RF   f/MHz band " + "".join(f"{a:>8}" for a in ants) + "   ch-mean"
    print(hdr)
    for rf in rfs:
        got = {a: cells[(rf, a)][0] for a in ants if cells[(rf, a)]}
        if len(got) < 2:
            row = "".join(f"{got.get(a, float('nan')):8.1f}"
                          if a in got else "       -" for a in ants)
            print(f"  {rf:>3} {freq_mhz(rf):>6} {band(rf):>4} {row}   (single-antenna)")
            continue
        chm = sum(got.values()) / len(got)
        row = ""
        for a in ants:
            if a in got:
                row += f"{got[a]:8.1f}"
                rel[(a, band(rf))].append(got[a] - chm)
            else:
                row += "       -"
        print(f"  {rf:>3} {freq_mhz(rf):>6} {band(rf):>4} {row}   {chm:7.1f}")
    print("\n  fingerprint: antenna x band -> relative MER (dB vs channel mean)")
    for (a, b), v in sorted(rel.items()):
        m = sum(v) / len(v)
        sd = math.sqrt(sum((x - m) ** 2 for x in v) / len(v)) if len(v) > 1 else 0.0
        print(f"    ant {a:>8} {b:>7}: {m:+5.1f} dB  (sd {sd:.1f}, n={len(v)} channels)")
    # day-stability of the fingerprint
    print("\n  fingerprint stability across days (per-day A-minus-B, shared channels):")
    for rf in rfs:
        byday = defaultdict(lambda: defaultdict(list))
        for ts, r, a, m in rows:
            if r == rf:
                byday[ts.date()][a].append(m)
        diffs = []
        for d, per in sorted(byday.items()):
            if len(per.get("A", [])) >= MIN_BIN_N and len(per.get("B", [])) >= MIN_BIN_N:
                diffs.append((d, median(per["A"]) - median(per["B"])))
        if len(diffs) >= 2:
            v = [x[1] for x in diffs]
            m = sum(v) / len(v)
            sd = math.sqrt(sum((x - m) ** 2 for x in v) / len(v))
            sign_stable = all(x > 0 for x in v) or all(x < 0 for x in v)
            print(f"    RF{rf:>2}: A-B per-day {['%+.1f' % x for x in v]}  "
                  f"mean {m:+.1f} sd {sd:.1f}  sign-stable={sign_stable}")
    return cells, rel


def band(rf):
    if rf < 14:
        return "VHFhi"
    return "UHFlo" if rf < 25 else "UHFhi"

# ------------------------------------------------------- transfer model

TAU_MHZ = 24.0        # frequency kernel half-life-ish scale
SHRINK = 0.6          # pseudo-weight of the antenna-band mean

def predict_cell(rf, ant, cells, ants, exclude_rfs):
    """Predict MER for (rf, ant) using ONLY cells whose rf not in exclude_rfs.
    Distance-weighted regression in frequency within the same regime on the
    same antenna, shrunk toward the antenna's same-regime mean. Returns
    (estimate, total_weight, basis) or None."""
    f0, reg = freq_mhz(rf), regime(rf)
    num = den = 0.0
    used = []
    for (r, a), c in cells.items():
        if a != ant or c is None or r in exclude_rfs or regime(r) != reg:
            continue
        w = 2.0 ** (-abs(freq_mhz(r) - f0) / TAU_MHZ)
        num += w * c[0]
        den += w
        used.append(r)
    # antenna same-regime mean (fallback + shrink target)
    pool = [c[0] for (r, a), c in cells.items()
            if a == ant and c is not None and r not in exclude_rfs
            and regime(r) == reg]
    if not pool:
        # regime never seen on this antenna: antenna overall mean + global
        # regime offset learned from other antennas
        pool_all = [c[0] for (r, a), c in cells.items()
                    if a == ant and c is not None and r not in exclude_rfs]
        if not pool_all:
            return None
        offs = []
        for a2 in ants:
            if a2 == ant:
                continue
            pin = [c[0] for (r, a), c in cells.items()
                   if a == a2 and c and r not in exclude_rfs and regime(r) == reg]
            pout = [c[0] for (r, a), c in cells.items()
                    if a == a2 and c and r not in exclude_rfs and regime(r) != reg]
            if pin and pout:
                offs.append(sum(pin) / len(pin) - sum(pout) / len(pout))
        off = sum(offs) / len(offs) if offs else 0.0
        return (sum(pool_all) / len(pool_all) + off, 0.1, "regime-offset")
    amean = sum(pool) / len(pool)
    if den == 0:
        return (amean, 0.1, "antenna-mean")
    est = (num + SHRINK * amean) / (den + SHRINK)
    return (est, den, "freq-weighted(%s)" % ",".join(str(r) for r in sorted(used)))


def task5_loo(rows, ants, rfs, cells):
    print("\n== A5: leave-one-out validation ==")
    solid = {(rf, a): c for (rf, a), c in cells.items() if c}
    global_med = median([c[0] for c in solid.values()])
    ant_med = {a: median([c[0] for (r, x), c in solid.items() if x == a])
               for a in ants}
    ant_med = {a: m for a, m in ant_med.items() if m is not None}

    # ---- LOO-CHANNEL (strict): whole channel never measured anywhere
    print("\n  -- LOO-CHANNEL (channel never visited on ANY antenna) --")
    err_m, err_a, err_g = [], [], []
    hits_m = hits_a = hits_b = 0
    n_best = 0
    lines = []
    for rf in rfs:
        truth = {a: solid[(rf, a)][0] for a in ants if (rf, a) in solid}
        if not truth:
            continue
        preds = {}
        for a in truth:
            p = predict_cell(rf, a, solid, ants, exclude_rfs={rf})
            if p:
                preds[a] = p
                err_m.append(abs(p[0] - truth[a]))
                err_a.append(abs(ant_med.get(a, global_med) - truth[a]))
                err_g.append(abs(global_med - truth[a]))
                lines.append(f"    RF{rf:>2} ant {a}: truth {truth[a]:5.1f}  "
                             f"model {p[0]:5.1f} ({p[0]-truth[a]:+4.1f})  "
                             f"ant-avg {ant_med.get(a, global_med):5.1f} "
                             f"({ant_med.get(a, global_med)-truth[a]:+4.1f})  "
                             f"[{p[2]}]")
        if len(truth) >= 2 and len(preds) >= 2:
            n_best += 1
            tb = max(truth, key=truth.get)
            if max(preds, key=lambda a: preds[a][0]) == tb:
                hits_m += 1
            if max(ant_med, key=ant_med.get) == tb:
                hits_a += 1
        print()
    print("\n".join(lines))
    def mae(v):
        return sum(v) / len(v) if v else float("nan")
    print(f"\n    MER MAE: model {mae(err_m):.2f} dB | antenna-avg baseline "
          f"{mae(err_a):.2f} dB | global-median baseline {mae(err_g):.2f} dB "
          f"(n={len(err_m)} cells)")
    if n_best:
        print(f"    best-antenna hit-rate: model {hits_m}/{n_best} | "
              f"always-best-antenna baseline {hits_a}/{n_best} | "
              f"random {n_best/ len([a for a in ant_med]):.1f}/{n_best} expected")

    # ---- LOO-CELL: channel seen on other antennas, not this one
    print("\n  -- LOO-CELL (channel known on other antennas, new on this one) --")
    err_m2, err_a2 = [], []
    lines = []
    for (rf, a), c in sorted(solid.items()):
        others = {a2: solid[(rf, a2)][0] for a2 in ants
                  if a2 != a and (rf, a2) in solid}
        if not others:
            continue
        # additive transfer: channel effect from other antennas + this
        # antenna's offset learned on shared channels (same regime pref)
        offs = []
        for a2, v2 in others.items():
            shared = [(solid[(r, a)][0] - solid[(r, a2)][0])
                      for r in rfs if r != rf
                      and (r, a) in solid and (r, a2) in solid
                      and regime(r) == regime(rf)]
            if not shared:
                shared = [(solid[(r, a)][0] - solid[(r, a2)][0])
                          for r in rfs if r != rf
                          and (r, a) in solid and (r, a2) in solid]
            if shared:
                offs.append(v2 + median(shared))
        if not offs:
            continue
        # blend additive estimate with the frequency-local estimate
        p = predict_cell(rf, a, {k: v for k, v in solid.items()
                                 if k != (rf, a)}, ants, exclude_rfs=set())
        add_est = sum(offs) / len(offs)
        est = 0.5 * add_est + 0.5 * p[0] if p else add_est
        err_m2.append(abs(est - c[0]))
        err_a2.append(abs(ant_med.get(a, global_med) - c[0]))
        lines.append(f"    RF{rf:>2} ant {a}: truth {c[0]:5.1f}  transfer "
                     f"{est:5.1f} ({est-c[0]:+4.1f})  ant-avg "
                     f"{ant_med.get(a, global_med):5.1f} "
                     f"({ant_med.get(a, global_med)-c[0]:+4.1f})")
    print("\n".join(lines))
    print(f"\n    MER MAE: transfer {mae(err_m2):.2f} dB | antenna-avg "
          f"baseline {mae(err_a2):.2f} dB (n={len(err_m2)} cells)")
    return mae(err_m), mae(err_a), mae(err_g)


# ------------------------------------------------------------------ main

def main(merged=True):
    rows = load(merged)
    ants = sorted(set(a for _, _, a, _ in rows))
    rfs = sorted(set(r for _, r, _, _ in rows))
    tag = "MERGED (rabbit=A, philips=B, discone=C)" if merged else "UNMERGED raw labels"
    print("#" * 72)
    print(f"# antenna fingerprint analysis — {tag}")
    print(f"# rows={len(rows)}  antennas={ants}  channels={rfs}")
    print("#" * 72)
    same, cross, curves = task1_curve_correlations(rows, ants, rfs)
    task2_freq_smoothness(rows, ants, rfs)
    task3_tower(rows, ants, rfs, curves, include_21=True)
    task3_tower(rows, ants, rfs, curves, include_21=False)
    cells, rel = task4_fingerprints(rows, ants, rfs)
    if merged:
        task5_loo(rows, ants, rfs, cells)


if __name__ == "__main__":
    main(merged="--unmerged" not in sys.argv)
