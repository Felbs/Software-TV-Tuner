"""FRONTIER PATROL — the generalized night hunter.

The discone campaign (bedtime_discone.py) proved the thesis: a
(channel, antenna) pair written off by old data can decode TODAY,
because the decoder keeps improving and the sky keeps moving. This
tool lets the model pick its own conquests: every pair whose learned
MER sits near the decode cliff is a frontier — probe them for real
while the tuner is idle, log any CRACKED, and feed every attempt back
into the history.

Target selection (per antenna, from quality_history.csv):
  frontier = recency-weighted median MER in [CLIFF-2.5, CLIFF+1.5]
  plus pairs unseen on an antenna whose fingerprint prior lands in the
  band (time_knob_prior, if available). Capped, rotated fairly.

Usage:
    python frontier_patrol.py --until 7          # patrol until 07:00
    python frontier_patrol.py --list             # just show targets
"""
import argparse
import io
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import time_knob as tkn  # noqa: E402

try:
    import time_knob_prior as tkp
except ImportError:
    tkp = None

PANEL = "http://127.0.0.1:8642"
LOGF = HERE / "lab" / "frontier_patrol.log"
CLIFF = 15.2
BAND = (CLIFF - 2.5, CLIFF + 1.5)
MAX_TARGETS = 6
SCAN_JSON = Path.home() / ".tv_tuner" / "scan.json"


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def api(path, body=None):
    req = urllib.request.Request(
        PANEL + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def known_programs():
    """rf -> (program_num, virtual, name) from the last scan(s)."""
    out = {}
    try:
        scan = json.loads(SCAN_JSON.read_text(encoding="utf-8"))
        for ch in scan.get("channels", []):
            if not ch.get("lock"):
                continue
            progs = ch.get("programs") or []
            if progs:
                p = progs[0]
                out[ch["rf"]] = (p.get("program_num") or 1,
                                 str(ch.get("virtual") or ch["rf"]),
                                 p.get("service_name") or f"RF{ch['rf']}")
    except (OSError, ValueError):
        pass
    return out


def pick_targets(rows):
    """Frontier (rf, ant) pairs, most promising first."""
    ants = sorted({r["ant"] for r in rows if r["ant"].startswith("Antenna")})
    rfs = sorted({r["rf"] for r in rows})
    progs = known_programs()
    targets = []
    for ant in ants:
        for rf in rfs:
            if rf not in progs:
                continue        # nothing tunable known for this RF
            sub = [r for r in rows if r["rf"] == rf and r["ant"] == ant
                   and r["mer"] is not None]
            if len(sub) >= 3:
                mers = sorted(r["mer"] for r in sub[-200:])
                est, basis = mers[len(mers) // 2], f"history n={len(sub)}"
            elif tkp is not None:
                try:
                    p = tkp.prior(rf, ant, rows)
                    est, basis = p["mer_estimate"], f"prior({p['basis']})"
                except Exception:
                    continue
            else:
                continue
            if est is not None and BAND[0] <= est <= BAND[1]:
                targets.append({"rf": rf, "ant": ant, "est": round(est, 1),
                                "basis": basis,
                                "prog": progs[rf][0],
                                "virt": progs[rf][1],
                                "name": progs[rf][2]})
    # most promising first: closest to (just below) the cliff decodes
    # are the likeliest conquests
    targets.sort(key=lambda t: abs(t["est"] - CLIFF))
    return targets[:MAX_TARGETS]


def probe(t, hold_s=150):
    api("/api/tune", {"rf": t["rf"], "prog": t["prog"], "virt": t["virt"],
                      "name": f"NIGHT patrol {t['name']}",
                      "antenna": t["ant"]})
    t0 = time.time()
    while time.time() - t0 < 120:
        time.sleep(5)
        s = api("/api/status")
        if s.get("tuned"):
            break
        if (s.get("stage") or "").startswith("RADIO FAILED"):
            log(f"RF{t['rf']}@{t['ant']}: radio failed")
            return False
    time.sleep(hold_s)
    s = api("/api/status")
    hdrs = s.get("hdrs_s") or 0
    log(f"RF{t['rf']}@{t['ant']} (est {t['est']}, {t['basis']}): "
        f"MER={s.get('mer_db')} loss={s.get('loss_pct')}% hdrs/s={hdrs}")
    if hdrs and hdrs > 0.5:
        log(f"*** CRACKED — RF{t['rf']} decodes on {t['ant']} *** "
            "soaking 10 min")
        time.sleep(600)
        s2 = api("/api/status")
        log(f"soak: MER={s2.get('mer_db')} loss={s2.get('loss_pct')}% "
            f"hdrs/s={s2.get('hdrs_s')}")
        api("/api/stop", {})
        return True
    api("/api/stop", {})
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", type=int, default=7)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    rows = tkn.load()
    targets = pick_targets(rows)
    log(f"frontier targets: {[(t['rf'], t['ant'], t['est']) for t in targets]}")
    if args.list or not targets:
        return
    cracked, i = 0, 0
    while True:
        now = datetime.now()
        if now.hour == args.until:
            log(f"{args.until:02d}:00 — patrol over. cracks: {cracked}")
            api("/api/stop", {})
            return
        try:
            s = api("/api/status")
            if (s.get("tuned") or s.get("tuning")) and \
                    not (s.get("name") or "").startswith("NIGHT"):
                log("user is watching — standing down 10 min")
                time.sleep(600)
                continue
            if s.get("scan", {}).get("running"):
                time.sleep(120)
                continue
            in_dawn = (now.hour == 4 and now.minute >= 30) or \
                now.hour in (5, 6)
            if probe(targets[i % len(targets)]):
                cracked += 1
            i += 1
            time.sleep(600 if in_dawn else 1500)
        except Exception as e:
            log(f"panel unreachable ({e}) — retry in 5 min")
            time.sleep(300)


if __name__ == "__main__":
    main()
