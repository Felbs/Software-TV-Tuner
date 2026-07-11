#!/usr/bin/env python
"""antenna_id.py — antenna auto-identification from spectral fingerprints.

"Plug in any antenna and go": every full channel scan already measures the
antenna+cable system's gain-vs-frequency response (phase-1 power sniff:
per-frequency RMS level, pilot SNR, pilot sharpness). That response is a
FINGERPRINT — an attic panel, a VHF-deaf UHF yagi and a discone produce
sharply different spectral SHAPES (validated 2026-07-11,
lab/antenna_fingerprint_research.md: band-level fingerprints are real and
day-stable while ABSOLUTE levels swing ±3-7 dB with propagation).

This module turns that into identity:

  signature_from_scan(scan_dict)   scan.json -> signature (shape, not level)
  similarity(a, b)                 0..1 score + per-component detail
  observe_scan(scan_dict)          match against lab/antenna_profiles.json,
                                   update the store, return an event:
        RECOGNIZED  same antenna as before on this port (refresh, EMA update)
        MOVED       a KNOWN antenna reappeared on a different port — its
                    accumulated history follows it (the magic moment)
        CHANGED     similar but drifted beyond tolerance (new cable?) —
                    NEVER silently forked: a pending question for the UI
        NEW         no match -> auto-create a profile
        ADOPTED     bootstrap: first signature ever seen on this port with
                    no better match -> adopt without resetting learning
  resolve_pending(port, action)    answer a CHANGED question: update | fork
  set_name(pid, name)              user-friendly label ("shed directional")
  rows_for_profile(pid, rows)      quality_history rows inside the profile's
                                   port/time spans -> Knob-of-Time history
                                   that follows the PHYSICAL antenna
  identify_sweep(port, env)        standalone ~60 s sweep (sdr_sweep.py over
                                   the market's scan frequencies) when you
                                   want an ID without a full scan

Universality laws: no antenna names or characteristics in code — profiles
are data; works from zero on a fresh install; nothing destroyed (CHANGED
asks, forks archive nothing, epochs are append-only elsewhere).

The epoch side effect (time_knob.mark_new_antenna) is the CALLER's job —
events carry needs_epoch=True so the panel can reuse its existing
new-antenna machinery. This module never touches the learning stores.

CLI:
    python antenna_id.py selftest
    python antenna_id.py matrix FILE [FILE ...]   pairwise similarity table
    python antenna_id.py observe [SCANFILE]       run one observation
    python antenna_id.py report                   print the profile ledger
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILES = HERE / "lab" / "antenna_profiles.json"
TOOLS = Path(r"Z:\src\magic-tv-decoder\tools")
SCAN_JSON = Path(os.path.expanduser("~")) / ".tv_tuner" / "scan.json"

# similarity thresholds (calibrated 2026-07-11 on real saved sweeps:
# same-antenna repeat scans score ~0.93; every cross-antenna pair —
# shed-directional vs discone vs June-era fixture rig — scored <= 0.50;
# see lab/antenna_id_report.md for the full matrix)
T_RECOGNIZED = 0.75   # >= : same antenna
T_CHANGED = 0.55      # >= : same family, drifted (cable? aim?) -> ASK
T_VACANT = 0.70       # >= vs the port's enrolled vacant print: empty socket
MIN_COMMON = 8        # min shared frequencies for a meaningful score
STRONG_PILOT_DB = 30.0  # the scanner's own strict carrier gate
EMA = 0.25            # how fast a recognized profile's reference adapts
# spread -> similarity knees (dB of median-absolute-deviation between two
# sweeps' shape vectors at which similarity drops to 0.5). Same antenna
# 31 min apart measured 0.5 dB level / 1.4 dB pilot spread; different
# antennas measured 1.8-6.2 / 3.8-9.2 dB.
KNEE_LEVEL_DB = 2.0
KNEE_PILOT_DB = 4.0

# ---------------------------------------------------------------------------
# signatures
# ---------------------------------------------------------------------------

def rf_to_mhz(rf):
    """NA ATSC channel center frequency. Strict-carrier scan records drop
    freq_mhz (they come from the lock-test result), so identity must be
    reconstructable from the RF number alone."""
    rf = int(rf)
    if rf <= 4:
        return 57.0 + 6 * (rf - 2)
    if rf <= 6:
        return 79.0 + 6 * (rf - 5)
    if rf <= 13:
        return 177.0 + 6 * (rf - 7)
    return 473.0 + 6 * (rf - 14)


def signature_from_scan(scan):
    """scan.json dict -> signature, or None if the sweep is unusable.
    Uses every scanned frequency (dead channels are the quiet references
    that anchor the shape). All stored quantities are RELATIVE — level is
    referenced to the sweep's own noise floor, pilot SNR is a ratio by
    construction — so absolute diurnal drift cancels."""
    chans = []
    for c in scan.get("channels", []):
        r = c.get("rms_dbfs")
        if r is None or r <= -150:
            continue
        f = c.get("freq_mhz") or (rf_to_mhz(c["rf"]) if c.get("rf") else None)
        if not f:
            continue
        chans.append((round(float(f), 1), float(r),
                      max(-5.0, min(70.0, float(c.get("pilot_snr_db")
                                               or -5.0)))))
    if len(chans) < MIN_COMMON:
        return None
    chans.sort()
    freqs = [c[0] for c in chans]
    rms = [c[1] for c in chans]
    pilot = [c[2] for c in chans]
    floor = scan.get("noise_floor_dbfs")
    if floor is None:
        floor = sorted(rms)[len(rms) // 2]
    resp = [round(r - floor, 2) for r in rms]
    # wedged-radio guard: a firmware-stuck SDR returns a flat noise ramp;
    # refuse to fingerprint it (fingerprinting noise poisons the ledger)
    if max(resp) - min(resp) < 4.0 and max(pilot) < STRONG_PILOT_DB:
        return None
    return {
        "v": 2,
        "freqs_mhz": freqs,
        "resp": resp,                       # dB above own noise floor
        "pilot": [round(p, 1) for p in pilot],
        "carriers": [f for f, p in zip(freqs, pilot)
                     if p >= STRONG_PILOT_DB],
        "meta": {"scanned_at": scan.get("scanned_at"),
                 "noise_floor_dbfs": floor,
                 "n_freqs": len(freqs)},
    }


def signature_from_sweep(rows, scanned_at=None):
    """sdr_sweep.py output rows -> signature (same shape as from_scan)."""
    chans = [{"freq_mhz": r["freq_hz"] / 1e6,
              "rms_dbfs": r.get("rms_dbfs", -999),
              "pilot_snr_db": r.get("pilot_snr_db")}
             for r in rows if r.get("samples", 0) > 0]
    return signature_from_scan(
        {"channels": chans,
         "scanned_at": scanned_at or
         datetime.now().replace(microsecond=0).isoformat()})


def _mad(vals):
    """Median absolute deviation around the median — a shape-mismatch
    spread that ignores any constant offset (an inline amp shifts every
    frequency equally; the antenna is the RESIDUAL)."""
    med = sorted(vals)[len(vals) // 2]
    dev = sorted(abs(v - med) for v in vals)
    return dev[len(dev) // 2]


def _knee(spread, knee):
    """dB spread -> 0..1 similarity; 0.5 exactly at the knee."""
    return 1.0 / (1.0 + (spread / knee) ** 2)


def _carrierness(p):
    """Continuous carrier membership (no gate flicker): 0 below 20 dB
    pilot SNR, 1 above 40."""
    return min(1.0, max(0.0, (p - 20.0) / 20.0))


def similarity(sa, sb):
    """(score 0..1, detail dict). Three shape comparisons over the shared
    frequency grid, all restricted to INFORMATIVE frequencies (dead
    channels agree trivially between any two sweeps and must not vote):
      s_level   spread of the floor-referenced level difference
      s_pilot   spread of the pilot-SNR difference
      s_carrier union-weighted agreement of continuous carrier strength
                (which stations exist and how solidly — the DC market's
                towers are shared, so the WEIGHT is what discriminates)."""
    if not sa or not sb:
        return 0.0, {"error": "missing signature"}
    ia = {f: i for i, f in enumerate(sa["freqs_mhz"])}
    ib = {f: i for i, f in enumerate(sb["freqs_mhz"])}
    common = [f for f in sb["freqs_mhz"] if f in ia]
    if len(common) < MIN_COMMON:
        return 0.0, {"error": "too few shared frequencies",
                     "n_common": len(common)}
    ra = {f: sa["resp"][ia[f]] for f in common}
    rb = {f: sb["resp"][ib[f]] for f in common}
    pa = {f: sa["pilot"][ia[f]] for f in common}
    pb = {f: sb["pilot"][ib[f]] for f in common}
    det = {"n_common": len(common)}
    lv = [f for f in common if ra[f] > 4.0 or rb[f] > 4.0]
    if len(lv) >= 4:
        det["level_spread_db"] = round(_mad([ra[f] - rb[f] for f in lv]), 2)
        s_level = _knee(det["level_spread_db"], KNEE_LEVEL_DB)
    else:
        s_level = 0.5   # both sweeps flat above floor: no level evidence
    pv = [f for f in common if pa[f] >= 15.0 or pb[f] >= 15.0]
    if len(pv) >= 4:
        det["pilot_spread_db"] = round(_mad([pa[f] - pb[f] for f in pv]), 2)
        s_pilot = _knee(det["pilot_spread_db"], KNEE_PILOT_DB)
    else:
        s_pilot = 0.5
    num = sum(abs(_carrierness(pa[f]) - _carrierness(pb[f])) for f in common)
    den = sum(max(_carrierness(pa[f]), _carrierness(pb[f])) for f in common)
    s_carrier = 1.0 - num / den if den > 1e-9 else 0.5
    det.update(s_level=round(s_level, 3), s_pilot=round(s_pilot, 3),
               s_carrier=round(s_carrier, 3))
    score = 0.30 * s_level + 0.30 * s_pilot + 0.40 * s_carrier
    return round(score, 3), det


def _ema_merge(ref, new):
    """Fold a fresh signature into a profile's reference (recognized
    antenna: slow-adapt so cable aging / seasonal drift tracks without
    letting one weird sweep hijack the identity)."""
    iref = {f: i for i, f in enumerate(ref["freqs_mhz"])}
    out_f, out_r, out_p = [], [], []
    for j, f in enumerate(new["freqs_mhz"]):
        if f in iref:
            i = iref[f]
            out_r.append(round((1 - EMA) * ref["resp"][i]
                               + EMA * new["resp"][j], 2))
            out_p.append(round((1 - EMA) * ref["pilot"][i]
                               + EMA * new["pilot"][j], 1))
        else:
            out_r.append(new["resp"][j])
            out_p.append(new["pilot"][j])
        out_f.append(f)
    merged = dict(ref)
    merged.update(freqs_mhz=out_f, resp=out_r, pilot=out_p,
                  carriers=[f for f, p in zip(out_f, out_p)
                            if p >= STRONG_PILOT_DB],
                  meta=new["meta"])
    return merged


# ---------------------------------------------------------------------------
# profile store
# ---------------------------------------------------------------------------

def load_profiles(path=PROFILES):
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(d, dict) and "profiles" in d:
            return d
    except (OSError, ValueError):
        pass
    return {"v": 1, "profiles": {}, "port_current": {}, "pending": {},
            "next_id": 1}


def save_profiles(store, path=PROFILES):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=1)
        os.replace(tmp, p)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def print_id(sig):
    """A stable designation derived from the fingerprint MATH itself
    (2026-07-11, user's naming scheme): hash the enrollment signature's
    quantized shape -> 'HF-XXXXX'. Assigned once at enrollment, stable
    forever (EMA drift never changes an identity, only the signature)."""
    import hashlib
    import base64
    q = ",".join(f"{f:.0f}:{round(r / 2) * 2:+d}"
                 for f, r in zip(sig.get("freqs_mhz", []),
                                 sig.get("resp", [])))
    h = hashlib.sha1(q.encode()).digest()
    return "HF-" + base64.b32encode(h)[:5].decode().upper()


def auto_descriptor(sig):
    """What the math can say about an antenna without being told:
    band tilt (UHF vs VHF relative response) and where it peaks."""
    fs = sig.get("freqs_mhz", [])
    rs = sig.get("resp", [])
    if not fs or not rs:
        return ""
    vhf = [r for f, r in zip(fs, rs) if f < 300]
    uhf = [r for f, r in zip(fs, rs) if f >= 300]
    parts = []
    if vhf and uhf:
        tilt = (sum(uhf) / len(uhf)) - (sum(vhf) / len(vhf))
        if tilt >= 3:
            parts.append(f"UHF-tilted +{tilt:.0f} dB")
        elif tilt <= -3:
            parts.append(f"VHF-tilted {-tilt:.0f} dB")
        else:
            parts.append("broadband")
    peak_f = fs[rs.index(max(rs))]
    parts.append(f"peak {peak_f:.0f} MHz")
    return " · ".join(parts)


def _new_profile(store, sig, port, ts):
    pid = print_id(sig)
    if pid in store["profiles"]:            # hash collision: disambiguate
        pid = "%s-%d" % (pid, store["next_id"])
    store["next_id"] += 1
    store["profiles"][pid] = {
        "name": None, "signature": sig,
        "descriptor": auto_descriptor(sig),
        "first_seen": ts, "last_seen": ts, "match_count": 1,
        "port_history": [{"port": port, "start": ts, "end": None,
                          "confirmed": True}],
        "sightings": [{"ts": ts, "port": port, "score": 1.0}],
    }
    return pid


def _close_span(prof, port, ts):
    for span in prof["port_history"]:
        if span["end"] is None and span["port"] == port:
            span["end"] = ts


def _sight(prof, ts, port, score):
    prof["last_seen"] = ts
    prof["match_count"] += 1
    prof["sightings"].append({"ts": ts, "port": port, "score": score})
    del prof["sightings"][:-200]        # cap, keep the recent story


def mark_vacant(port, sig, store=None, save=True, path=PROFILES):
    """Enroll (or refresh) a port's EMPTY-SOCKET print, so a sweep of a
    disconnected port says 'nothing plugged in' instead of enrolling
    ambient leakage as a ghost antenna."""
    store = store if store is not None else load_profiles(path)
    store.setdefault("vacant", {})[port] = sig
    if save:
        save_profiles(store, path)
    return store


def observe(sig, port, store=None, ts=None, save=True, path=PROFILES):
    """Match a fresh signature against the ledger and update it.
    Returns an event dict:
      {verdict, profile, name, score, detail, needs_epoch, message,
       moved_from (MOVED only), pending (CHANGED only)}
    needs_epoch=True means the PORT's resident physically changed and the
    caller should restart the port's learning epoch (the 🔌 button path).
    """
    ts = ts or datetime.now().replace(microsecond=0).isoformat()
    store = store if store is not None else load_profiles(path)
    if sig is None:
        return {"verdict": "UNUSABLE", "needs_epoch": False,
                "message": "sweep unusable for fingerprinting "
                           "(flat spectrum — radio wedged?)"}
    cur_pid = store["port_current"].get(port)
    if cur_pid and cur_pid not in store["profiles"]:
        # dangling reference (profile deleted out from under the port
        # pointer) — self-heal instead of crashing (the ant-0002 lesson)
        store["port_current"].pop(port, None)
        cur_pid = None
    scores = {}
    details = {}
    for pid, prof in store["profiles"].items():
        s, d = similarity(prof["signature"], sig)
        scores[pid], details[pid] = s, d
    best_pid = max(scores, key=scores.get) if scores else None
    best = scores.get(best_pid, 0.0)
    cur = scores.get(cur_pid, 0.0) if cur_pid else 0.0

    # VACANT competes, never preempts (2026-07-11 round-trip lesson: a
    # disconnected port hears real broadcast leakage, so its vacant
    # print can resemble a weak antenna — the empty-socket verdict is
    # only allowed to win when it BEATS every known antenna decisively)
    vac = (store.get("vacant") or {}).get(port)
    if vac is not None:
        vs, _ = similarity(vac, sig)
        if vs >= T_VACANT and vs >= best + 0.05:
            return {"verdict": "VACANT", "needs_epoch": False,
                    "score": vs,
                    "message": "%s reads as an EMPTY SOCKET "
                               "(%.0f%% vacant-print match, best known "
                               "antenna only %.0f%%) — nothing appears "
                               "to be plugged in"
                               % (port, 100 * vs, 100 * best)}

    def label(pid):
        prof = store["profiles"][pid]
        return prof["name"] or pid

    # 1. the port's resident still matches -> RECOGNIZED
    if cur_pid and cur >= T_RECOGNIZED:
        prof = store["profiles"][cur_pid]
        prof["signature"] = _ema_merge(prof["signature"], sig)
        _sight(prof, ts, port, cur)
        ev = {"verdict": "RECOGNIZED", "profile": cur_pid,
              "name": prof["name"], "score": cur, "detail": details[cur_pid],
              "needs_epoch": False,
              "message": "recognized %s on %s (%.0f%% match)"
                         % (label(cur_pid), port, cur * 100)}
    # 2. a DIFFERENT known profile matches strongly -> the antenna MOVED
    elif best_pid and best_pid != cur_pid and best >= T_RECOGNIZED:
        prof = store["profiles"][best_pid]
        was = [sp["port"] for sp in prof["port_history"]]
        moved_from = was[-1] if was else None
        _close_span(prof, moved_from, ts)
        if cur_pid:
            _close_span(store["profiles"][cur_pid], port, ts)
        prof["port_history"].append({"port": port, "start": ts, "end": None,
                                     "confirmed": False})
        _sight(prof, ts, port, best)
        store["port_current"][port] = best_pid
        ev = {"verdict": "MOVED", "profile": best_pid,
              "name": prof["name"], "score": best,
              "detail": details[best_pid], "moved_from": moved_from,
              "needs_epoch": True,
              "message": "this looks like the antenna you called '%s' "
                         "(%.0f%% match), last seen on %s — its learned "
                         "history can follow it to %s"
                         % (label(best_pid), best * 100,
                            moved_from or "?", port)}
    # 3. gray zone vs the resident -> CHANGED: ask, never silently fork
    elif cur_pid and cur >= T_CHANGED:
        store["pending"][port] = {"ts": ts, "profile": cur_pid,
                                  "score": cur, "signature": sig}
        ev = {"verdict": "CHANGED", "profile": cur_pid,
              "name": store["profiles"][cur_pid]["name"], "score": cur,
              "detail": details[cur_pid], "needs_epoch": False,
              "pending": True,
              "message": "%s on %s looks DIFFERENT (%.0f%% match — new "
                         "cable? re-aimed?) — same antenna, or a new one?"
                         % (label(cur_pid), port, cur * 100)}
    # 4. nothing close -> NEW (or ADOPTED on a virgin port)
    else:
        bootstrap = cur_pid is None
        pid = _new_profile(store, sig, port, ts)
        if cur_pid:
            _close_span(store["profiles"][cur_pid], port, ts)
        store["port_current"][port] = pid
        ev = {"verdict": "ADOPTED" if bootstrap else "NEW", "profile": pid,
              "name": None, "score": best, "detail": details.get(best_pid),
              "needs_epoch": not bootstrap,
              "message": ("first signature for %s captured as %s "
                          "(learning continues)" % (port, pid)) if bootstrap
                         else ("UNKNOWN antenna on %s — new profile %s, "
                               "fresh learning epoch" % (port, pid))}
    if save:
        save_profiles(store, path)
    return ev


def observe_scan(scan=None, path=PROFILES, scan_path=SCAN_JSON):
    """Convenience: scan dict (or scan.json on disk) -> observe().
    The scan's own 'antenna' field says which port was swept."""
    if scan is None:
        scan = json.loads(Path(scan_path).read_text(encoding="utf-8"))
    port = scan.get("antenna")
    if not port:
        return {"verdict": "UNUSABLE", "needs_epoch": False,
                "message": "scan has no antenna/port stamp"}
    ts = scan.get("scanned_at")
    return observe(signature_from_scan(scan), port, ts=ts, path=path)


def resolve_pending(port, action, path=PROFILES):
    """Answer a CHANGED question. action: 'update' folds the new signature
    into the existing profile (same antenna, cable/aim drifted);
    'fork' creates a new profile (different antenna) — caller epochs."""
    store = load_profiles(path)
    q = store["pending"].pop(port, None)
    if not q:
        return {"verdict": "NOOP", "needs_epoch": False,
                "message": "no pending question for %s" % port}
    ts = datetime.now().replace(microsecond=0).isoformat()
    if action == "update":
        prof = store["profiles"][q["profile"]]
        prof["signature"] = _ema_merge(prof["signature"], q["signature"])
        _sight(prof, q["ts"], port, q["score"])
        ev = {"verdict": "UPDATED", "profile": q["profile"],
              "name": prof["name"], "needs_epoch": False,
              "message": "profile %s updated — drift accepted as the same "
                         "antenna" % (prof["name"] or q["profile"])}
    else:
        old = store["profiles"].get(q["profile"])
        if old:
            _close_span(old, port, ts)
        pid = _new_profile(store, q["signature"], port, ts)
        store["port_current"][port] = pid
        ev = {"verdict": "FORKED", "profile": pid, "name": None,
              "needs_epoch": True,
              "message": "new profile %s on %s — fresh learning epoch "
                         "(old profile archived in the ledger)" % (pid, port)}
    save_profiles(store, path)
    return ev


def confirm_attach(port, path=PROFILES):
    """User confirmed a MOVED verdict: mark the profile's newest span on
    this port as confirmed so rows_for_profile() may use it."""
    store = load_profiles(path)
    pid = store["port_current"].get(port)
    if not pid:
        return {"verdict": "NOOP", "message": "no profile on %s" % port}
    for span in reversed(store["profiles"][pid]["port_history"]):
        if span["port"] == port and span["end"] is None:
            span["confirmed"] = True
            break
    save_profiles(store, path)
    prof = store["profiles"][pid]
    return {"verdict": "ATTACHED", "profile": pid, "name": prof["name"],
            "message": "%s's history now follows it onto %s"
                       % (prof["name"] or pid, port)}


def set_name(pid, name, path=PROFILES):
    store = load_profiles(path)
    if pid not in store["profiles"]:
        return {"verdict": "NOOP", "message": "no profile %s" % pid}
    store["profiles"][pid]["name"] = (name or "").strip() or None
    save_profiles(store, path)
    return {"verdict": "NAMED", "profile": pid,
            "message": "profile %s is now '%s'" % (pid, name)}


def rows_for_profile(pid, rows, path=PROFILES, confirmed_only=True):
    """Knob-of-Time linkage: quality_history rows (time_knob.load())
    that fall inside this profile's confirmed port/time spans — the
    physical antenna's accumulated history across every port it lived on."""
    store = load_profiles(path)
    prof = store["profiles"].get(pid)
    if not prof:
        return []
    spans = []
    for sp in prof["port_history"]:
        if confirmed_only and not sp.get("confirmed"):
            continue
        s = datetime.fromisoformat(sp["start"])
        e = datetime.fromisoformat(sp["end"]) if sp["end"] else datetime.max
        spans.append((sp["port"], s, e))
    return [r for r in rows
            if any(r["ant"] == p and s <= r["ts"] <= e for p, s, e in spans)]


# ---------------------------------------------------------------------------
# standalone identify sweep (no full scan needed)
# ---------------------------------------------------------------------------

def identify_freqs(scan_path=SCAN_JSON):
    """The sweep plan: every frequency the last scan measured (carriers
    AND quiet channels — the quiet ones anchor the shape). Falls back to
    the NA broadcast grid via tv_tuner's region table if no scan exists."""
    try:
        d = json.loads(Path(scan_path).read_text(encoding="utf-8"))
        f = sorted({int(c["freq_mhz"] * 1e6) for c in d["channels"]
                    if c.get("freq_mhz")})
        if len(f) >= MIN_COMMON:
            return f
    except (OSError, ValueError, KeyError):
        pass
    sys.path.insert(0, str(TOOLS))
    import tv_tuner
    return sorted({int(f) for _rf, f, _l in tv_tuner.REGIONS[0]["channels"]})


def identify_sweep(port, env=None, dwell_sec=1.5, py=None):
    """Run sdr_sweep.py over the market grid on `port` (~60 s at 1.5 s
    dwell = Welch-averaged PSD, +12 dB pilot sensitivity) and return a
    signature. CALLER must ensure the radio is idle (single-tenant SDR)."""
    freqs = identify_freqs()
    py = py or sys.executable
    if env is None:
        # sdr_sweep needs the SDRplay API DLL dir on PATH — raw environ
        # loads no sdrplay module and enumerates an empty device list
        # (found the hard way 2026-07-11)
        try:
            sys.path.insert(0, str(TOOLS))
            import tv_tuner
            env = tv_tuner.env_with_sdrplay()
        except Exception:
            env = os.environ.copy()
    proc = subprocess.run(
        [py, "-u", str(TOOLS / "sdr_sweep.py"),
         "--dwell-sec", "%.2f" % dwell_sec, "--antenna", port],
        input=json.dumps(freqs).encode(),
        capture_output=True, timeout=60 + int(dwell_sec * len(freqs) * 3),
        env=env)
    if proc.returncode != 0:
        raise RuntimeError("sdr_sweep failed: %s"
                           % proc.stderr.decode(errors="replace")[-300:])
    return signature_from_sweep(json.loads(proc.stdout.decode()))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_scan(fp):
    return json.loads(Path(fp).read_text(encoding="utf-8"))


def matrix(files, out=None):
    """Pairwise similarity table over scan.json-format files."""
    out = out or sys.stdout
    sigs, names = [], []
    for fp in files:
        d = _load_scan(fp)
        s = signature_from_scan(d)
        tag = "%s %s" % (Path(fp).stem,
                         (d.get("scanned_at") or "")[:16])
        if s is None:
            print("  %-42s UNUSABLE (flat/wedged sweep)" % tag, file=out)
            continue
        sigs.append(s)
        names.append(tag)
    if len(sigs) < 2:
        print("need >= 2 usable sweeps", file=out)
        return []
    w = max(len(n) for n in names)
    print("\npairwise similarity — score (level/pilot/carrier components, "
          "spreads in dB):", file=out)
    rows = []
    for i, a in enumerate(sigs):
        for j, b in enumerate(sigs):
            if j <= i:
                continue
            sc, d = similarity(a, b)
            rows.append((names[i], names[j], sc, d))
            verdict = ("SAME" if sc >= T_RECOGNIZED else
                       "DRIFT?" if sc >= T_CHANGED else "DIFFERENT")
            print("  %-*s x %-*s  %.3f %-9s (L%.2f/%s  P%.2f/%s  C%.2f)"
                  % (w, names[i], w, names[j], sc, verdict,
                     d["s_level"], d.get("level_spread_db", "-"),
                     d["s_pilot"], d.get("pilot_spread_db", "-"),
                     d["s_carrier"]), file=out)
    return rows


def report(path=PROFILES, out=None):
    out = out or sys.stdout
    store = load_profiles(path)
    if not store["profiles"]:
        print("no antenna profiles yet — scan or IDENTIFY to enroll one",
              file=out)
        return
    for pid, p in store["profiles"].items():
        port = next((k for k, v in store["port_current"].items()
                     if v == pid), None)
        print("%s  '%s'  %s" % (pid, p["name"] or "(unnamed)",
                                ("LIVE on %s" % port) if port else "off-air"),
              file=out)
        print("   first %s  last %s  matches %d  carriers %s"
              % (p["first_seen"], p["last_seen"], p["match_count"],
                 p["signature"]["carriers"]), file=out)
        for sp in p["port_history"]:
            print("   span %s  %s -> %s%s"
                  % (sp["port"], sp["start"], sp["end"] or "now",
                     "" if sp.get("confirmed") else "  (unconfirmed)"),
                  file=out)
    for port, q in store["pending"].items():
        print("PENDING question on %s: %s at %.0f%% — resolve update|fork"
              % (port, q["profile"], q["score"] * 100), file=out)


# ---------------------------------------------------------------------------
# self-test (pure synthetic, temp store — never touches the real ledger)
# ---------------------------------------------------------------------------

def _synth(carrier_set, tilt=0.0, noise=0.0, seed=1):
    """Synthetic scan: NA-ish grid, carriers where told, optional band
    tilt (dB across the span) and per-freq jitter."""
    import random
    rng = random.Random(seed)
    chans = []
    freqs = [57, 63, 69, 79, 85, 177, 183, 189, 195, 201, 207, 213] + \
            list(range(473, 606, 6))
    for f in freqs:
        fr = f / 600.0
        base = -60 + tilt * fr + rng.uniform(-noise, noise)
        carrier = f in carrier_set
        chans.append({"rf": 0, "freq_mhz": float(f),
                      "rms_dbfs": base + (18 if carrier else 0),
                      "pilot_snr_db": (38 + rng.uniform(-3, 3)) if carrier
                                      else rng.uniform(-2, 8),
                      "lock": carrier})
    return {"scanned_at": "2026-07-11T12:00:00", "antenna": "Antenna A",
            "channels": chans, "noise_floor_dbfs": -60}


def selftest():
    import shutil
    tmp = Path(tempfile.mkdtemp()) / "profiles.json"
    ok = 0

    def chk(cond, msg):
        nonlocal ok
        assert cond, "FAIL: " + msg
        ok += 1
        print("  ok -", msg)

    uhf_yagi = {473, 515, 575, 593, 599, 605}          # VHF-deaf
    vhf_panel = {177, 189, 479, 515}                   # VHF-strong
    s1 = signature_from_scan(_synth(uhf_yagi, seed=1))
    s1b = signature_from_scan(_synth(uhf_yagi, noise=1.5, seed=2))
    s2 = signature_from_scan(_synth(vhf_panel, seed=3))
    sc_same, _ = similarity(s1, s1b)
    sc_diff, _ = similarity(s1, s2)
    chk(sc_same >= T_RECOGNIZED, "same antenna re-swept -> RECOGNIZED zone "
        "(%.2f)" % sc_same)
    chk(sc_diff < T_CHANGED, "different antenna -> NEW zone (%.2f)" % sc_diff)
    chk(signature_from_scan({"channels": []}) is None, "empty scan refused")

    # bootstrap: virgin port adopts without an epoch  (fixed timestamps —
    # the knob-linkage check below depends on span times, not wall clock)
    ev = observe(s1, "Antenna A", path=tmp, ts="2026-07-11T12:00:00")
    chk(ev["verdict"] == "ADOPTED" and not ev["needs_epoch"],
        "bootstrap adopts, never resets learning")
    pid1 = ev["profile"]
    # re-sweep: recognized
    ev = observe(s1b, "Antenna A", path=tmp, ts="2026-07-11T12:10:00")
    chk(ev["verdict"] == "RECOGNIZED" and ev["profile"] == pid1,
        "repeat sweep recognized (%.0f%%)" % (ev["score"] * 100))
    # different antenna appears on the same port: NEW + epoch
    ev = observe(s2, "Antenna A", path=tmp, ts="2026-07-11T13:00:00")
    chk(ev["verdict"] == "NEW" and ev["needs_epoch"],
        "different antenna -> NEW profile + fresh epoch")
    pid2 = ev["profile"]
    # the first antenna reappears on port B: MOVED, history follows
    ev = observe(s1, "Antenna B", path=tmp, ts="2026-07-11T13:05:00")
    chk(ev["verdict"] == "MOVED" and ev["profile"] == pid1
        and ev["moved_from"] == "Antenna A" and ev["needs_epoch"],
        "old antenna on a new port -> MOVED, knowledge follows the metal")
    ev = confirm_attach("Antenna B", path=tmp)
    chk(ev["verdict"] == "ATTACHED", "user confirms the attach")
    # drift: same carriers, warped shape -> CHANGED asks, never forks
    drift = signature_from_scan(_synth(vhf_panel, tilt=9.0, noise=2.0,
                                       seed=5))
    ev = observe(drift, "Antenna A", path=tmp)
    chk(ev["verdict"] in ("CHANGED", "RECOGNIZED"),
        "drifted sweep never silently forks (%s %.2f)"
        % (ev["verdict"], ev["score"]))
    if ev["verdict"] == "CHANGED":
        ev = resolve_pending("Antenna A", "update", path=tmp)
        chk(ev["verdict"] == "UPDATED", "user says same antenna -> updated")
    else:
        chk(True, "(drift landed in RECOGNIZED — EMA absorbed it)")
    # naming + knob linkage
    set_name(pid1, "shed directional", path=tmp)
    store = load_profiles(tmp)
    chk(store["profiles"][pid1]["name"] == "shed directional",
        "friendly name sticks")
    rows = [dict(ts=datetime(2026, 7, 11, 12, 30), ant="Antenna A", rf=7,
                 mer=17.0, loss_pct=None, source="t", date_known=True),
            dict(ts=datetime(2026, 7, 11, 13, 30), ant="Antenna B", rf=7,
                 mer=16.0, loss_pct=None, source="t", date_known=True),
            dict(ts=datetime(2026, 6, 1, 12, 0), ant="Antenna A", rf=7,
                 mer=10.0, loss_pct=None, source="t", date_known=True)]
    got = rows_for_profile(pid1, rows, path=tmp)
    chk(any(r["ant"] == "Antenna B" for r in got)
        and not any(r["ts"].month == 6 for r in got),
        "history follows the antenna across ports, pre-history excluded")
    # fresh-install honesty
    chk(load_profiles(Path(tempfile.gettempdir()) / "nope_xyz.json")
        ["profiles"] == {}, "fresh install -> empty ledger, no crash")
    shutil.rmtree(tmp.parent, ignore_errors=True)
    print("selftest: %d checks passed" % ok)


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else "report"
    if cmd == "selftest":
        selftest()
    elif cmd == "matrix":
        matrix(argv[2:])
    elif cmd == "observe":
        ev = (observe_scan(_load_scan(argv[2])) if len(argv) > 2
              else observe_scan())
        print(json.dumps(ev, indent=1))
    elif cmd == "report":
        report()
    else:
        print(__doc__)


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
    _main(sys.argv)
