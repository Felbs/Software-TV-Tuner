#!/usr/bin/env python
"""tower_axis_analysis.py — TASK A: does the antenna A-vs-B advantage
pattern cluster by TRANSMITTER SITE beyond what frequency predicts?
(OFFLINE research script, 2026-07-11. Imported by nothing, wires nothing.)

Background (lab/antenna_fingerprint_research.md): UHF antenna choice is
decided by per-channel "territories" (+/-3.5 dB) that refused to transfer
via any frequency-smooth model. The new hunch: territories are not random
per-channel noise — they are a DIRECTION fingerprint. This market has
(assumed) three sites: a dominant DC cluster, RF21 toward Baltimore, and
RF31 at a third site. If territories cluster by site, the "unlearnable"
UHF residual becomes learnable from ~one channel per tower.

Site membership below is a STATED ASSUMPTION (broadcast-market geography,
not measured by the rig). The analysis functions are generic: they take a
{rf: site_label} map; the DC-specific map lives only under __main__.

Tests:
  T1  per-channel A-B advantage (hour-balanced, recency-weighted medians)
      + per-day sign stability — the raw territory table.
  T2  site-separation permutation test: over all ways to pick the same
      number of "off-cluster" channels, how often does the A-advantage
      separate the groups as well as the true site map does? Exact test,
      tiny n, honest p.
  T3  frequency-confound audit: can a single contiguous frequency window
      ALSO produce the same separation? (If yes, direction and frequency
      ripple are confounded on this channel set — say so.)
  T4  sweep-to-sweep co-movement: within one antenna, correlate channel
      MER across scan sweeps. Same-site channels share a propagation path
      + interferer environment, so they should co-move more than
      cross-site pairs. This is NEW evidence vs the hour-curve test
      (which compared curve SHAPES across days and was negative).
  T5  day-to-day co-movement, same idea at day granularity, using ALL
      sources (flight recorder etc.), per antenna.

Run:  python lab/tower_axis_analysis.py
"""
import csv
import itertools
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
HISTORY = HERE / "quality_history.csv"

HALF_LIFE_DAYS = 14.0
MIN_BIN_N = 3
MIN_CELL_N = 30
MIN_CELL_DAYS = 2

MERGE = {"rabbit": "A", "Antenna A": "A", "philips": "B",
         "Antenna B": "B", "discone": "C", "Antenna C": "C"}


# ------------------------------------------------------------------ data

def load(path=HISTORY, merge=True):
    rows = []
    for r in csv.DictReader(open(path, newline="", encoding="utf-8",
                                 errors="ignore")):
        if not r["mer"]:
            continue
        ant = r["ant"]
        if merge:
            if ant not in MERGE:
                continue
            ant = MERGE[ant]
        elif ant == "?":
            continue
        try:
            rows.append(dict(ts=datetime.fromisoformat(r["ts"]),
                             rf=int(r["rf"]), ant=ant, mer=float(r["mer"]),
                             src=r["source"]))
        except (ValueError, KeyError):
            continue
    return [r for r in rows if r["rf"] != 27]      # n=5 fossil


def _w(ts, now):
    return 0.5 ** (max(0.0, (now - ts).total_seconds() / 86400.0)
                   / HALF_LIFE_DAYS)


def _wmed(pairs):
    pairs = sorted(pairs)
    if not pairs:
        return None
    tot = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= tot / 2:
            return v
    return pairs[-1][0]


def cells(rows, now=None):
    """(rf, ant) -> hour-balanced weighted-median MER (solid cells only).
    Same statistic as time_knob_prior._cells."""
    now = now or max(r["ts"] for r in rows)
    hb = defaultdict(lambda: defaultdict(list))
    days = defaultdict(set)
    for r in rows:
        k = (r["rf"], r["ant"])
        hb[k][r["ts"].hour].append((r["mer"], _w(r["ts"], now)))
        days[k].add(r["ts"].date())
    out = {}
    for k, b in hb.items():
        meds = [_wmed(v) for v in b.values() if len(v) >= MIN_BIN_N]
        n = sum(len(v) for v in b.values())
        if meds and n >= MIN_CELL_N and len(days[k]) >= MIN_CELL_DAYS:
            m = sorted(meds)
            out[k] = (m[len(m) // 2] if len(m) % 2
                      else 0.5 * (m[len(m) // 2 - 1] + m[len(m) // 2]))
    return out


# ------------------------------------------------------------ T1 deltas

def ab_deltas(rows, a="A", b="B", uhf_only=True):
    """{rf: A-minus-B dB} over channels solid on both antennas."""
    c = cells(rows)
    rfs = sorted(set(rf for rf, ant in c) )
    out = {}
    for rf in rfs:
        if uhf_only and rf < 14:
            continue
        if (rf, a) in c and (rf, b) in c:
            out[rf] = round(c[(rf, a)] - c[(rf, b)], 2)
    return out


def daily_sign_stability(rows, rf, a="A", b="B"):
    """Per-day A-B medians -> (n_days_both, n_days_same_sign_as_overall)."""
    per = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["rf"] == rf and r["ant"] in (a, b):
            per[r["ts"].date()][r["ant"]].append(r["mer"])
    deltas = []
    for d, m in sorted(per.items()):
        if a in m and b in m and len(m[a]) >= 3 and len(m[b]) >= 3:
            med = lambda v: sorted(v)[len(v) // 2]
            deltas.append(med(m[a]) - med(m[b]))
    if not deltas:
        return 0, 0, []
    overall = sorted(deltas)[len(deltas) // 2]
    same = sum(1 for d in deltas if (d > 0) == (overall > 0))
    return len(deltas), same, [round(d, 1) for d in deltas]


# ---------------------------------------------------- T2 permutation test

def site_separation_test(deltas, site_map, cluster_label):
    """Exact permutation test. deltas: {rf: A-B dB}. site_map: {rf: site}.
    Off-cluster = site != cluster_label. Statistic: mean(off) - mean(on).
    Returns (observed_stat, p_value, n_perms, perfectly_separated)."""
    rfs = sorted(deltas)
    off = [rf for rf in rfs if site_map.get(rf) != cluster_label]
    k = len(off)
    if k == 0 or k == len(rfs):
        return None
    def stat(off_set):
        o = [deltas[rf] for rf in rfs if rf in off_set]
        i = [deltas[rf] for rf in rfs if rf not in off_set]
        return sum(o) / len(o) - sum(i) / len(i)
    obs = stat(set(off))
    perms = list(itertools.combinations(rfs, k))
    ge = sum(1 for p in perms if stat(set(p)) >= obs - 1e-12)
    # perfect separation: every off-cluster delta beats every on-cluster
    perfect = min(deltas[rf] for rf in off) > max(
        deltas[rf] for rf in rfs if rf not in off)
    return obs, ge / len(perms), len(perms), perfect


# ------------------------------------------------- T3 frequency confound

def freq_window_confound(deltas, off_rfs):
    """Can ANY contiguous RF window reproduce the off-cluster set?
    Returns True if the off-cluster channels are exactly the channels
    inside some contiguous window of the observed channel list — i.e.
    direction and frequency ripple cannot be distinguished here."""
    rfs = sorted(deltas)
    off = set(off_rfs) & set(rfs)
    for i in range(len(rfs)):
        for j in range(i, len(rfs)):
            if set(rfs[i:j + 1]) == off:
                return True
    return False


# ------------------------------------------------ T4/T5 co-movement

def pearson(x, y):
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


def sweep_matrix(rows, ant, gap_s=1500, src="scan"):
    """Group `src` rows on one antenna into sweeps; -> {rf: {sweep_i: mer}}
    (last reading wins if a sweep hit the same rf twice)."""
    sc = sorted((r for r in rows if r["src"] == src and r["ant"] == ant),
                key=lambda r: r["ts"])
    mat, si, last = defaultdict(dict), -1, None
    for r in sc:
        if last is None or (r["ts"] - last).total_seconds() > gap_s:
            si += 1
        last = r["ts"]
        mat[r["rf"]][si] = r["mer"]
    return mat, si + 1


def day_matrix(rows, ant):
    """{rf: {date: median mer}} across ALL sources for one antenna."""
    per = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["ant"] == ant:
            per[r["rf"]][r["ts"].date()].append(r["mer"])
    return {rf: {d: sorted(v)[len(v) // 2] for d, v in dd.items()
                 if len(v) >= 3}
            for rf, dd in per.items()}


def comovement(mat, min_common=4):
    """Pairwise correlations across shared columns.
    -> {(rf1, rf2): (r, n_common)}"""
    out = {}
    rfs = sorted(mat)
    for a, b in itertools.combinations(rfs, 2):
        common = sorted(set(mat[a]) & set(mat[b]))
        if len(common) < min_common:
            continue
        r = pearson([mat[a][c] for c in common], [mat[b][c] for c in common])
        if r is not None:
            out[(a, b)] = (r, len(common))
    return out


def site_split_r(pairs, site_map):
    """Mean r for same-site vs cross-site pairs + permutation p on the
    difference (permute site labels over channels, exact when small)."""
    def split(smap):
        same = [r for (a, b), (r, n) in pairs.items()
                if smap.get(a) == smap.get(b)
                and smap.get(a) is not None]
        cross = [r for (a, b), (r, n) in pairs.items()
                 if smap.get(a) != smap.get(b)
                 and smap.get(a) is not None and smap.get(b) is not None]
        if not same or not cross:
            return None
        return (sum(same) / len(same), len(same),
                sum(cross) / len(cross), len(cross))
    obs = split(site_map)
    if obs is None:
        return None
    ms, ns, mc, nc = obs
    diff = ms - mc
    # permute: reassign which channels carry each site label
    chans = sorted(set(site_map) & set(c for p in pairs for c in p))
    labels = [site_map[c] for c in chans]
    seen, ge, tot = set(), 0, 0
    for perm in itertools.permutations(labels):
        if perm in seen:
            continue
        seen.add(perm)
        s = split(dict(zip(chans, perm)))
        if s is None:
            continue
        tot += 1
        if s[0] - s[2] >= diff - 1e-12:
            ge += 1
    return dict(same_r=round(ms, 3), n_same=ns, cross_r=round(mc, 3),
                n_cross=nc, diff=round(diff, 3),
                p=round(ge / tot, 3) if tot else None, n_perm=tot)


# ------------------------------------------------------------------ main

def main():
    if not HISTORY.exists():
        print("no", HISTORY)
        return 1
    # DC-market site ASSUMPTION (research only — never ships in code):
    #   cluster    = the dominant multi-station site (NW-DC / Tenleytown)
    #   site-NE    = RF21 (Baltimore-side; MPT decoded here on rabbit ears)
    #   site-SW    = RF31 (separate site SW of the cluster)
    SITES = {7: "cluster", 9: "cluster", 15: "cluster", 34: "cluster",
             35: "cluster", 36: "cluster", 21: "site-NE", 31: "site-SW"}
    for merged in (True, False):
        rows = load(merge=merged)
        tag = "MERGED labels" if merged else "LIVE labels only"
        if not merged:
            rows = [r for r in rows if r["ant"].startswith("Antenna")]
            rows = [dict(r, ant=r["ant"].replace("Antenna ", "")) for r in rows]
        print("=" * 72)
        print("%s  (%d MER rows)" % (tag, len(rows)))
        print("=" * 72)

        # T1 — territory table + day stability
        d = ab_deltas(rows)
        print("\nT1  UHF A-B advantage (hour-balanced, dB;  + = A wins):")
        for rf in sorted(d):
            nd, same, ds = daily_sign_stability(rows, rf)
            print("   RF%-3d %-8s  A-B %+5.1f   day deltas %s (%d/%d sign-stable)"
                  % (rf, SITES.get(rf, "?"), d[rf], ds, same, nd))

        # T2 — site separation
        t2 = site_separation_test(d, SITES, "cluster")
        if t2:
            obs, p, nperm, perfect = t2
            print("\nT2  site separation: off-cluster mean(A-B) - cluster "
                  "mean(A-B) = %+.2f dB" % obs)
            print("    exact permutation p = %.3f (%d assignments)  "
                  "perfect sign separation: %s" % (p, nperm, perfect))

        # T3 — frequency confound
        off = [rf for rf in d if SITES.get(rf, "cluster") != "cluster"]
        conf = freq_window_confound(d, off)
        print("\nT3  frequency-window confound: a contiguous RF window "
              "reproduces the off-cluster set: %s" % conf)

        # T4 — sweep co-movement per antenna
        print("\nT4  sweep-to-sweep co-movement (scan rows):")
        for ant in ("A", "B"):
            mat, nsw = sweep_matrix(rows, ant)
            pairs = comovement(mat, min_common=4)
            if not pairs:
                print("   ant %s: <4 shared sweeps — not testable" % ant)
                continue
            print("   ant %s (%d sweeps, %d usable pairs):" % (ant, nsw,
                                                               len(pairs)))
            for (a, b), (r, n) in sorted(pairs.items()):
                rel = ("same-site" if SITES.get(a) == SITES.get(b)
                       else "cross-site")
                print("     RF%d x RF%-3d r=%+.2f (n=%d) %s" % (a, b, r, n, rel))
            s = site_split_r(pairs, SITES)
            if s:
                print("     same-site mean r %+0.2f (n=%d)  cross-site "
                      "%+0.2f (n=%d)  diff %+0.2f  perm p=%s"
                      % (s["same_r"], s["n_same"], s["cross_r"],
                         s["n_cross"], s["diff"], s["p"]))

        # T5 — day-to-day co-movement, all sources
        print("\nT5  day-to-day co-movement (all sources):")
        for ant in ("A", "B"):
            mat = day_matrix(rows, ant)
            pairs = comovement(mat, min_common=4)
            uhf_pairs = {k: v for k, v in pairs.items()
                         if k[0] >= 14 and k[1] >= 14}
            if not uhf_pairs:
                print("   ant %s: not testable" % ant)
                continue
            s = site_split_r(uhf_pairs, SITES)
            print("   ant %s: %d UHF pairs  %s" % (ant, len(uhf_pairs),
                  s if s else "(no split possible)"))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
