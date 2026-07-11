#!/usr/bin/env python
"""scan_planner.py — predictive scan ordering + dwell budgeting
(PROTOTYPE 2026-07-11 — imported by NOTHING, wired into nothing).

Research: lab/cross_channel_speed_research.md. The scan's phase 2 burns
most of its wall clock on channels the rig's own memory already calls
hopeless for the CURRENT antenna: the 7/11 discone sweep spent ~4.6 min
re-proving that 7 UHF carriers are "pilot, no field sync" — twice each
(the cold-start retry) — when every prior discone look said the same.

This module plans phase 2 instead of just running it in RF order:

  * rank candidates by expected productivity (history cell -> transfer
    prior -> unknown), productive channels first so the guide becomes
    usable fastest;
  * budget attempts: a (rf, antenna) pair that history confidently calls
    dead gets a PROBE (single attempt — the chain's own early-verdict
    ladder exits in 9-15 s) instead of the full 2-3 attempt ladder;
  * in-sweep streak rule: after `STREAK_K` consecutive dead verdicts in
    one band-regime on this antenna within THIS sweep, the rest of that
    regime is demoted to probes (catches a regime-dead antenna on its
    very first sweep, when no per-channel history exists yet);
  * NO SILENT SKIPS (project law): every candidate stays in the plan;
    probes still run and still record verdicts; every probed pair is
    promoted back to a FULL look every FULL_LOOK_EVERY-th sweep or when
    its last full look is older than FULL_LOOK_DAYS or when a probe
    result contradicts the prediction (self-correcting).

Universality: no city names, no channel lists, no antenna names in code.
Antennas are opaque strings; "dead" is a learned per-(rf, ant) statistic;
band regimes are the physics split (VHF < RF14 <= UHF) already validated
in lab/antenna_fingerprint_research.md.

Where it WOULD plug in (NOT wired): tv_tuner.run_scan() between phase 1
(hot list ready) and phase 2 — `plan()` eats the hot list + history and
returns the ordered attempt budget; scan_one_rf_with_retry() honors
entry["retries"]. The verdict log it learns from needs run_scan to stamp
the sweep antenna at scan-dict top level (today only LOCKED records
carry "antenna" — dead verdicts are unattributable from scan.json alone).

CLI:
    python scan_planner.py selftest    # synthetic checks (no files)
    python scan_planner.py replay      # offline replay on the real
                                       # history + saved scan JSONs
"""
import csv
import glob
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
HISTORY = HERE / "lab" / "quality_history.csv"

# ---- decode physics / validated constants --------------------------------
CLIFF_DB = 15.2            # below this, television does not exist
MAE_SOLID = 1.6            # validated LOO error of the transfer prior
HALF_LIFE_DAYS = 14.0      # recency law
MIN_BIN_N = 3
MIN_CELL_N = 12            # planner cells may be thinner than prior cells:
MIN_CELL_DAYS = 2          # a scan writes ~1 row per sweep

# ---- planner policy -------------------------------------------------------
DEAD_MARGIN_DB = 1.0       # est < CLIFF - margin  => MER-dead
DEAD_VERDICTS_MIN = 2      # dead verdicts on >=2 distinct days => verdict-dead
PRODUCTIVE_DB = CLIFF_DB + MAE_SOLID   # est >= 16.8 => expect a lock
FULL_LOOK_EVERY = 5        # every Nth sweep, probes are promoted to full
FULL_LOOK_DAYS = 7.0       # ...or when the last full look is older than this
STREAK_K = 2               # consecutive same-regime dead verdicts in-sweep

# ---- timing model (from tools/tv_tuner.py code paths, validated against
#      the 2026-07-11 bedtime sweeps' wall clock — see research doc) --------
ATTEMPT_COST_S = {
    "locked":       26.0,   # growth ~8 + dwell 8 + probe/psip ~6 + 4 release
    "mer_floor":    13.5,   # 9.5 s verdict + 4 s SDR release
    "no_pilot":     14.0,
    "pilot_no_fs":  19.5,   # 15.5 s verdict + 4 s
    "no_growth":    29.0,   # full 25 s window + 4 s
    "weak_no_lock": 27.0,   # growth + dwell + failed probe + 4 s
}
CURRENT_ATTEMPTS = {        # what scan_one_rf_with_retry(retries=2) does now
    "locked": 1, "mer_floor": 2, "no_pilot": 2, "pilot_no_fs": 2,
    "no_growth": 3, "weak_no_lock": 3,
}
DEAD_VERDICT_CLASSES = frozenset(
    ("mer_floor", "no_pilot", "pilot_no_fs", "no_growth", "weak_no_lock"))


def regime(rf):
    return "VHF" if rf < 14 else "UHF"


# ========================================================================
# evidence
# ========================================================================

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


def load_history(path=HISTORY):
    """quality_history.csv -> row dicts (opaque antenna labels kept)."""
    rows = []
    if not Path(path).exists():
        return rows
    for r in csv.DictReader(open(path, newline="", encoding="utf-8",
                                 errors="ignore")):
        if not r.get("mer"):
            continue
        try:
            rows.append(dict(ts=datetime.fromisoformat(r["ts"]),
                             rf=int(r["rf"]), ant=r["ant"],
                             mer=float(r["mer"])))
        except (ValueError, KeyError):
            continue
    return rows


def cell_estimate(rf, ant, rows, now=None):
    """Recency-weighted hour-balanced MER for one (rf, ant), or None.
    -> (est_db, n, n_days)"""
    now = now or datetime.now()
    hb, days = defaultdict(list), set()
    for r in rows:
        if r["rf"] == rf and r["ant"] == ant and r.get("mer") is not None:
            hb[r["ts"].hour].append((r["mer"], _w(r["ts"], now)))
            days.add(r["ts"].date())
    meds = [_wmed(v) for v in hb.values() if len(v) >= MIN_BIN_N]
    n = sum(len(v) for v in hb.values())
    if not meds or n < MIN_CELL_N or len(days) < MIN_CELL_DAYS:
        return None
    m = sorted(meds)
    est = (m[len(m) // 2] if len(m) % 2
           else 0.5 * (m[len(m) // 2 - 1] + m[len(m) // 2]))
    return round(est, 1), n, len(days)


def classify_verdict(lock, reason):
    """Map a scan result (lock flag + reason string) to a verdict class."""
    if lock:
        return "locked"
    reason = (reason or "").lower()
    if reason.startswith("mer floor"):
        return "mer_floor"
    if reason.startswith("no pilot"):
        return "no_pilot"
    if reason.startswith("pilot, no field sync"):
        return "pilot_no_fs"
    if reason.startswith("no live.ts growth"):
        return "no_growth"
    if reason.startswith("weak signal"):
        return "weak_no_lock"
    return None      # not a phase-2 verdict (phase-1 reject, spawn fail...)


def harvest_scan_verdicts(paths=None):
    """Saved scan JSONs -> [{ts, ant, rf, verdict}]. The sweep antenna is
    inferred from the locked records (dead records carry no antenna field
    today — the wire-in fix is one line in run_scan). Sweeps with zero
    locks are skipped: their antenna is honestly unknowable."""
    if paths is None:
        paths = sorted(glob.glob(os.path.join(
            os.path.expanduser("~"), ".tv_tuner", "scan*.json")))
    out = []
    for p in paths:
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "scanned_at" not in d:
            continue
        ts = datetime.fromisoformat(d["scanned_at"])
        ants = [c.get("antenna") for c in d.get("channels", [])
                if c.get("lock") and c.get("antenna")
                and c.get("antenna") != "?"]
        ant = d.get("antenna") or (ants[0] if ants else None)
        if not ant:
            continue
        for c in d.get("channels", []):
            if not c.get("hot") and not c.get("lock"):
                continue
            v = classify_verdict(c.get("lock"), c.get("reason"))
            if v:
                out.append(dict(ts=ts, ant=ant, rf=c["rf"], verdict=v,
                                mer=c.get("mer_med")))
    return out


def dead_verdict_evidence(rf, ant, verdicts, now=None):
    """(n_dead_since_last_lock, n_distinct_days) for one (rf, ant)."""
    now = now or datetime.now()
    hist = sorted((v for v in verdicts
                   if v["rf"] == rf and v["ant"] == ant),
                  key=lambda v: v["ts"])
    dead, days = 0, set()
    for v in hist:
        if v["verdict"] == "locked":
            dead, days = 0, set()          # a lock resets the evidence
        elif v["verdict"] in DEAD_VERDICT_CLASSES:
            dead += 1
            days.add(v["ts"].date())
    return dead, len(days)


# ========================================================================
# classification + plan
# ========================================================================

def classify(rf, ant, rows, verdicts, now=None, prior_fn=None):
    """One (rf, ant) -> dict(tier, est, why, src).
    tier: 'productive' | 'likely' | 'unknown' | 'doubtful'
    src (evidence quality, used for ordering — real measurements of THIS
    pair always outrank transferred estimates): 'history' | 'verdicts' |
    'prior-solid' | 'prior-thin' | 'prior-fallback' | 'none'"""
    now = now or datetime.now()
    cell = cell_estimate(rf, ant, rows, now)
    if cell:
        est, n, nd = cell
        if est >= PRODUCTIVE_DB:
            return dict(tier="productive", est=est, src="history",
                        why="history %.1f dB (n=%d/%dd)" % (est, n, nd))
        if est < CLIFF_DB - DEAD_MARGIN_DB:
            return dict(tier="doubtful", est=est, src="history",
                        why="history %.1f dB < cliff-%.0f (n=%d/%dd)"
                            % (est, DEAD_MARGIN_DB, n, nd))
        return dict(tier="likely", est=est, src="history",
                    why="history %.1f dB near cliff (n=%d/%dd)" % (est, n, nd))
    dead, ndays = dead_verdict_evidence(rf, ant, verdicts, now)
    if dead >= DEAD_VERDICTS_MIN and ndays >= 2:
        return dict(tier="doubtful", est=None, src="verdicts",
                    why="%d dead scan verdicts over %d days, no lock since"
                        % (dead, ndays))
    if prior_fn:
        p = prior_fn(rf, ant)
        if p and p.get("mer_estimate") is not None:
            est, mae = p["mer_estimate"], p.get("expected_mae_db", MAE_SOLID)
            src = "prior-" + (p.get("confidence") or "thin")
            if est - mae >= PRODUCTIVE_DB and p.get("confidence") == "solid":
                return dict(tier="productive", est=est, src=src,
                            why="prior %.1f+/-%.1f dB (%s)" % (est, mae,
                                                               p.get("basis")))
            if est + mae < CLIFF_DB:
                return dict(tier="doubtful", est=est, src=src,
                            why="prior %.1f+/-%.1f dB below cliff"
                                % (est, mae))
            return dict(tier="likely", est=est, src=src,
                        why="prior %.1f+/-%.1f dB (%s)"
                            % (est, mae, p.get("confidence")))
    return dict(tier="unknown", est=None, src="none",
                why="no evidence — full look")


def make_prior_fn(rows, now=None):
    """Adapt time_knob_prior (same directory) when available; the planner
    stays functional without it."""
    try:
        sys.path.insert(0, str(HERE))
        import time_knob_prior as tkp
        cells = tkp._cells(
            [r for r in rows if r.get("mer") is not None], now)
        return lambda rf, ant: tkp.prior(rf, ant, rows, now,
                                         _cells_cache=cells)
    except Exception:
        return None


def plan(candidates, ant, rows, verdicts, now=None, sweep_index=0,
         last_full_look=None, prior_fn=None):
    """Ordered phase-2 plan.

    candidates      [rf, ...] phase-1 hot list
    ant             opaque antenna label of THIS sweep
    rows            MER history rows (load_history schema)
    verdicts        scan verdict log (harvest_scan_verdicts schema)
    sweep_index     caller's monotone sweep counter (full-look cadence)
    last_full_look  {(rf, ant): datetime} caller-owned state; None = never

    -> [ {rf, action ('full'|'probe'), retries, tier, est, why,
          budget_s (worst-case)} ]  ordered: productive desc-est, likely,
    unknown, doubtful last. Every candidate appears exactly once."""
    now = now or datetime.now()
    last_full_look = last_full_look or {}
    entries = []
    for rf in candidates:
        c = classify(rf, ant, rows, verdicts, now, prior_fn=prior_fn)
        action, retries = "full", 2
        if c["tier"] == "doubtful":
            action, retries = "probe", 0
            # self-correction: periodic promotion back to a full look
            lfl = last_full_look.get((rf, ant))
            due_age = lfl is None or (now - lfl).total_seconds() \
                > FULL_LOOK_DAYS * 86400
            due_cadence = sweep_index % FULL_LOOK_EVERY == 0
            if due_age or due_cadence:
                action, retries = "full", 2
                c["why"] += " [FULL-LOOK due: %s]" % (
                    "age" if due_age else "cadence")
        entries.append(dict(rf=rf, action=action, retries=retries,
                            tier=c["tier"], est=c["est"], why=c["why"],
                            src=c["src"], budget_s=_worst_case_s(action)))
    order = {"productive": 0, "likely": 1, "unknown": 2, "doubtful": 3}
    # within a tier, measured evidence for THIS pair outranks transferred
    # estimates (the 7/11 discone replay: a 20.8 dB FALLBACK-prior mirage
    # must not queue ahead of a channel with real 16.2 dB history)
    src_rank = {"history": 0, "verdicts": 0, "prior-solid": 1,
                "prior-thin": 2, "prior-fallback": 3, "none": 4}
    entries.sort(key=lambda e: (order[e["tier"]], src_rank[e["src"]],
                                -(e["est"] if e["est"] is not None else -99),
                                e["rf"]))
    return entries


def _worst_case_s(action):
    if action == "probe":
        return ATTEMPT_COST_S["no_growth"]          # single worst attempt
    return ATTEMPT_COST_S["no_growth"] * 3          # full ladder worst case


def apply_streak_rule(entries, results_so_far):
    """In-sweep adaptation (pure function the scan loop would call before
    each channel): if the last STREAK_K completed candidates in the SAME
    regime all returned dead verdicts (and none locked), demote the
    remaining not-yet-run channels of that regime to probes — unless they
    are 'productive' (real history beats a streak) or FULL-LOOK-due.
    Returns a NEW entries list; never removes a channel."""
    by_reg = defaultdict(list)
    for r in results_so_far:
        by_reg[regime(r["rf"])].append(r["verdict"])
    demote = set()
    for reg, vs in by_reg.items():
        tail = vs[-STREAK_K:]
        if len(tail) >= STREAK_K and all(
                v in DEAD_VERDICT_CLASSES for v in tail) \
                and "locked" not in vs:
            demote.add(reg)
    out = []
    for e in entries:
        if (regime(e["rf"]) in demote and e["action"] == "full"
                and e["tier"] in ("likely", "unknown")
                and "FULL-LOOK due" not in e["why"]):
            e = dict(e, action="probe", retries=0,
                     why=e["why"] + " [in-sweep streak: regime looks dead "
                                    "on this antenna]",
                     budget_s=_worst_case_s("probe"))
        out.append(e)
    return out


# ========================================================================
# offline cost model + replay
# ========================================================================

def sweep_cost_s(outcomes, policy="current", planned=None):
    """Seconds of phase 2 for a sweep.
    outcomes: [{rf, verdict}] what the radio actually said (truth).
    policy 'current': attempts = CURRENT_ATTEMPTS[verdict].
    policy 'planned': attempts = 1 for probes, CURRENT otherwise;
    planned = {rf: action}."""
    tot = 0.0
    for o in outcomes:
        v = o["verdict"]
        att = CURRENT_ATTEMPTS[v]
        if policy == "planned" and planned and \
                planned.get(o["rf"]) == "probe" and v != "locked":
            att = 1
        # a lock always ends the ladder after its (single) attempt
        tot += ATTEMPT_COST_S[v] * (1 if v == "locked" else att)
    return tot


def time_to_first_k_locks(outcomes_in_order, k):
    """Wall seconds (current attempt policy) until k channels have locked,
    given a scan order. Measures 'guide becomes usable' latency."""
    t, locks = 0.0, 0
    for o in outcomes_in_order:
        v = o["verdict"]
        t += ATTEMPT_COST_S[v] * (1 if v == "locked" else
                                  CURRENT_ATTEMPTS[v])
        if v == "locked":
            locks += 1
            if locks >= k:
                return t
    return None


def replay_sweep(scan_json_path, rows, verdicts, cold=True,
                 sweep_index=1):
    """Replay one saved sweep offline. cold=True uses only evidence from
    BEFORE the sweep (what a wired planner would have known); cold=False
    is steady state (what TOMORROW's identical sweep would cost).
    Returns a report dict or None."""
    try:
        d = json.load(open(scan_json_path, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ts = datetime.fromisoformat(d["scanned_at"])
    ants = [c.get("antenna") for c in d.get("channels", [])
            if c.get("lock") and c.get("antenna") and c["antenna"] != "?"]
    if not ants:
        return None
    ant = ants[0]
    outcomes = []
    for c in d.get("channels", []):
        if not c.get("hot") and not c.get("lock"):
            continue
        v = classify_verdict(c.get("lock"), c.get("reason"))
        if v:
            outcomes.append(dict(rf=c["rf"], verdict=v))
    if not outcomes:
        return None
    cutoff = ts if cold else ts + timedelta(seconds=1)
    rows_b = [r for r in rows if r["ts"] < cutoff]
    verd_b = [v for v in verdicts if v["ts"] < cutoff
              or (not cold and v["ts"] == ts)]
    prior_fn = make_prior_fn(rows_b, now=ts)
    # every historical sweep ran the FULL ladder (planner not wired yet),
    # so any verdict in the log counts as that pair's last full look
    lfl = {}
    for v in verd_b:
        k = (v["rf"], v["ant"])
        if k not in lfl or v["ts"] > lfl[k]:
            lfl[k] = v["ts"]
    p = plan([o["rf"] for o in outcomes], ant, rows_b, verd_b, now=ts,
             sweep_index=sweep_index, last_full_look=lfl,
             prior_fn=prior_fn)
    # simulate the in-sweep streak rule over the planned order
    done, final_action = [], {}
    pending = list(p)
    while pending:
        pending = apply_streak_rule(pending, done)
        e = pending.pop(0)
        truth = next(o for o in outcomes if o["rf"] == e["rf"])
        final_action[e["rf"]] = e["action"]
        done.append(dict(rf=e["rf"], verdict=truth["verdict"]))
    base = sweep_cost_s(outcomes, "current")
    plan_s = sweep_cost_s(outcomes, "planned", final_action)
    n_locks = sum(1 for o in outcomes if o["verdict"] == "locked")
    k = max(1, int(math.ceil(n_locks * 0.8)))
    ttk_base = time_to_first_k_locks(outcomes, k)
    order = {e["rf"]: i for i, e in enumerate(p)}
    ttk_plan = time_to_first_k_locks(
        sorted(outcomes, key=lambda o: order[o["rf"]]), k)
    return dict(path=str(scan_json_path), ant=ant, ts=ts.isoformat(),
                n_candidates=len(outcomes), n_locks=n_locks,
                baseline_s=round(base), planned_s=round(plan_s),
                saved_s=round(base - plan_s),
                ttk80_base_s=None if ttk_base is None else round(ttk_base),
                ttk80_plan_s=None if ttk_plan is None else round(ttk_plan),
                plan=[(e["rf"], final_action[e["rf"]], e["tier"], e["why"])
                      for e in p])


# ========================================================================
# self-test
# ========================================================================

def selftest():
    ok = 0

    def chk(cond, msg):
        nonlocal ok
        assert cond, "FAIL: " + msg
        ok += 1
        print("  ok -", msg)

    now = datetime(2026, 7, 11, 12, 0)

    def cell_rows(rf, ant, mer, n_per=6):
        return [dict(ts=now - timedelta(days=d, hours=12 - h), rf=rf,
                     ant=ant, mer=mer + 0.05 * (i % 3))
                for d in (1, 2) for h in (0, 5, 9) for i in range(n_per)]

    # rig: opaque antenna "pX" healthy on RF34/36, dead-cold on RF8
    rows = (cell_rows(34, "pX", 19.0) + cell_rows(36, "pX", 18.0)
            + cell_rows(8, "pX", 10.0) + cell_rows(15, "pX", 15.4))
    verd = []
    for dback in (1, 3):
        for rf in (33, 27):
            verd.append(dict(ts=now - timedelta(days=dback), ant="pX",
                             rf=rf, verdict="pilot_no_fs", mer=None))

    # 1. fresh install: everything unknown, full ladder, nothing skipped
    p = plan([34, 8], "pNew", [], [], now=now)
    chk(all(e["action"] == "full" and e["tier"] == "unknown" for e in p),
        "no evidence -> every channel gets the full look")

    # 2. tiers from history: productive / doubtful / likely
    p = plan([34, 36, 8, 15], "pX", rows, [], now=now, sweep_index=1)
    tiers = {e["rf"]: e["tier"] for e in p}
    chk(tiers[34] == "productive" and tiers[36] == "productive",
        "healthy history -> productive")
    chk(tiers[8] == "doubtful", "10 dB history -> doubtful")
    chk(tiers[15] == "likely", "near-cliff history -> likely (full look)")

    # 3. ordering: productive by est desc, doubtful last
    chk([e["rf"] for e in p] == [34, 36, 15, 8],
        "order: 19.0, 18.0, near-cliff, doubtful")

    # 4. doubtful -> probe with retries=0 (but never removed). A pair
    #    that has NEVER had a full look is promoted (age rule), so give
    #    it a recent one.
    p4 = plan([34, 36, 8, 15], "pX", rows, [], now=now, sweep_index=1,
              last_full_look={(8, "pX"): now - timedelta(days=1)})
    e8 = next(e for e in p4 if e["rf"] == 8)
    chk(e8["action"] == "probe" and e8["retries"] == 0,
        "doubtful channel is probed, not skipped")

    # 5. verdict-dead without any MER rows
    p = plan([33], "pX", rows, verd, now=now, sweep_index=1)
    chk(p[0]["tier"] == "doubtful" and "verdicts" in p[0]["why"],
        "2 dead verdicts on 2 days -> doubtful (no MER needed)")

    # 6. a lock RESETS dead-verdict evidence
    verd2 = verd + [dict(ts=now - timedelta(hours=5), ant="pX", rf=33,
                         verdict="locked", mer=17.0)]
    p = plan([33], "pX", rows, verd2, now=now, sweep_index=1)
    chk(p[0]["tier"] != "doubtful", "a lock resets dead evidence")

    # 7. FULL-LOOK promotions: cadence and age — no silent permanent skip
    p = plan([8], "pX", rows, [], now=now, sweep_index=FULL_LOOK_EVERY)
    chk(p[0]["action"] == "full" and "FULL-LOOK" in p[0]["why"],
        "every %dth sweep the doubtful channel gets a full look"
        % FULL_LOOK_EVERY)
    lfl = {(8, "pX"): now - timedelta(days=FULL_LOOK_DAYS + 1)}
    p = plan([8], "pX", rows, [], now=now, sweep_index=1,
             last_full_look=lfl)
    chk(p[0]["action"] == "full", "stale full-look date -> promoted")
    lfl = {(8, "pX"): now - timedelta(days=1)}
    p = plan([8], "pX", rows, [], now=now, sweep_index=1,
             last_full_look=lfl)
    chk(p[0]["action"] == "probe", "fresh full-look date -> probe again")

    # 8. streak rule: 2 same-regime dead verdicts demote the regime's
    #    remaining unknowns, but never a productive channel
    entries = plan([30, 32, 34], "pY",
                   cell_rows(34, "pY", 19.0), [], now=now, sweep_index=1)
    done = [dict(rf=27, verdict="pilot_no_fs"),
            dict(rf=29, verdict="pilot_no_fs")]
    adapted = apply_streak_rule(entries, done)
    a = {e["rf"]: e["action"] for e in adapted}
    chk(a[30] == "probe" and a[32] == "probe",
        "UHF dead streak demotes remaining unknown UHF to probes")
    chk(a[34] == "full", "streak never demotes a productive channel")
    done_vhf = [dict(rf=7, verdict="mer_floor"),
                dict(rf=9, verdict="mer_floor")]
    a2 = {e["rf"]: e["action"]
          for e in apply_streak_rule(entries, done_vhf)}
    chk(a2[30] == "full", "VHF streak does not demote UHF candidates")

    # 9. cost model: probe saves exactly the retry attempts
    outc = [dict(rf=33, verdict="pilot_no_fs"),
            dict(rf=34, verdict="locked")]
    base = sweep_cost_s(outc, "current")
    planned = sweep_cost_s(outc, "planned", {33: "probe", 34: "full"})
    chk(abs((base - planned) - ATTEMPT_COST_S["pilot_no_fs"]) < 0.01,
        "probe on a pilot-no-fs channel saves one 19.5 s attempt")
    chk(sweep_cost_s([dict(rf=34, verdict="locked")], "planned",
                     {34: "probe"}) == ATTEMPT_COST_S["locked"],
        "a probe that LOCKS costs the same as a full lock (no harm)")

    print("selftest: %d checks passed" % ok)


# ========================================================================
# replay CLI
# ========================================================================

def replay():
    rows = load_history()
    verdicts = harvest_scan_verdicts()
    if not rows:
        print("no history at %s — synthetic selftest only" % HISTORY)
        return
    print("history: %d MER rows; verdict log: %d entries from saved "
          "scan JSONs\n" % (len(rows), len(verdicts)))
    paths = sorted(glob.glob(os.path.join(
        os.path.expanduser("~"), ".tv_tuner", "scan*.json")))
    for p in paths:
        for cold in (True, False):
            r = replay_sweep(p, rows, verdicts, cold=cold, sweep_index=1)
            if not r:
                if cold:
                    print("%s: not replayable (no attributable antenna "
                          "or no phase-2 outcomes)" % Path(p).name)
                break
            mode = "COLD (evidence before sweep)" if cold else \
                   "WARM (steady state — tomorrow's sweep)"
            print("%s  [%s on %s]  %s" % (Path(p).name, r["ts"], r["ant"],
                                          mode))
            print("   phase-2 baseline %ds -> planned %ds  (saved %ds); "
                  "time-to-80%%-locks %s -> %s"
                  % (r["baseline_s"], r["planned_s"], r["saved_s"],
                     r["ttk80_base_s"], r["ttk80_plan_s"]))
            for rf, action, tier, why in r["plan"]:
                print("     RF%-3d %-5s %-10s %s" % (rf, action, tier, why))
            print()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if cmd == "selftest":
        selftest()
    elif cmd == "replay":
        replay()
    else:
        print(__doc__)
