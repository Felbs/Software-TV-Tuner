"""dawn_score2.py — P2 v1: score the dawn with the ACTUAL atmosphere.

v0 read surface METAR and underpredicted a monster (6.0 on the night
channel 9 ran 3h40m above the cliff): the duct was ALOFT, invisible to
a surface thermometer. v1 reads the Sterling VA radiosonde (station
72403 IAD — the balloon that samples our exact airspace at 00Z/12Z)
and computes the radio refractivity profile:

    N = 77.6 P/T + 3.73e5 e/T^2        (P hPa, T K, e vapor pressure)
    M = N + 157 z                       (z in km — modified refractivity)

    dM/dz < 0        -> DUCT (signals trapped and bent along the layer)
    dN/dz < -79/km   -> ducting equivalently
    -79..-40 N/km    -> superrefraction (extended range)

Score: strongest superrefractive/ducting layer below 3 km, weighted by
being low (VHF couples best to layers < ~1.5 km), combined with the
v0 surface score. Logged to cube_log.jsonl for morning calibration.

    python dawn_score2.py            # latest sounding + latest METAR
"""
import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
UA = {"User-Agent": "tv-tuna-p2 (hobby radio science)"}


def fetch(url, timeout=45):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")
    except urllib.error.URLError as e:
        if "CERTIFICATE" not in str(e):
            raise
        # weather.uwyo.edu ships a self-signed intermediate that python's
        # store rejects; it's public balloon data — accept for this host.
        import ssl
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.read().decode("utf-8", "ignore")


def latest_sounding_times():
    """Most recent 00Z/12Z, newest first."""
    now = datetime.now(timezone.utc)
    slots = []
    for back in range(0, 4):
        t = now - timedelta(hours=12 * back)
        hh = 12 if t.hour >= 12 else 0
        slots.append(t.replace(hour=hh, minute=0, second=0, microsecond=0))
    seen = []
    for s in slots:
        if s not in seen and s <= now:
            seen.append(s)
    return seen


def get_profile():
    """(z_m, P_hPa, T_C, Td_C) lists from Wyoming text for 72403 (IAD)."""
    for t in latest_sounding_times():
        # 2026: Wyoming moved to the WSGI endpoint (old cgi-bin 404s)
        url = ("https://weather.uwyo.edu/wsgi/sounding?"
               f"datetime={t.year}-{t.month:02d}-{t.day:02d}%20"
               f"{t.hour:02d}:00:00&id=72403&type=TEXT:LIST")
        try:
            txt = fetch(url)
        except Exception:
            continue
        rows = []
        for line in txt.splitlines():
            m = re.match(r"\s*(\d{3,4}\.\d)\s+(\d{2,5}\.?\d?)\s+"
                         r"(-?\d+\.\d)\s+(-?\d+\.\d)", line)
            if m:
                p, z, tc, td = map(float, m.groups())
                if 400 <= p <= 1050:
                    rows.append((z, p, tc, td))
        if len(rows) >= 8:
            return t, rows
    return None, []


def vapor_pressure(td_c):
    return 6.112 * pow(2.718281828, (17.67 * td_c) / (td_c + 243.5))


def duct_analysis(rows):
    """Layer-by-layer dN/dz below 3 km; return the juiciest layer."""
    layers = []
    for (z1, p1, t1, d1), (z2, p2, t2, d2) in zip(rows, rows[1:]):
        if z2 - z1 < 10 or z2 > 4000:
            continue
        def N(p, tc, td):
            tk = tc + 273.15
            return 77.6 * p / tk + 3.73e5 * vapor_pressure(td) / (tk * tk)
        dndz = (N(p2, t2, d2) - N(p1, t1, d1)) / ((z2 - z1) / 1000.0)
        inv = t2 > t1                       # temperature inversion layer
        layers.append({"z_lo": z1, "z_hi": z2, "dndz": round(dndz, 1),
                       "inversion": inv})
    ducts = [l for l in layers if l["dndz"] < -79]
    supers = [l for l in layers if -79 <= l["dndz"] < -40]
    invs = [l for l in layers if l["inversion"] and l["z_hi"] < 2500]
    return layers, ducts, supers, invs


def score_aloft(ducts, supers, invs):
    s = 0.0
    notes = []
    for d in ducts:
        w = 4.0 if d["z_hi"] < 1500 else 2.5
        s += w
        notes.append(f"DUCT {d['z_lo']:.0f}-{d['z_hi']:.0f} m "
                     f"(dN/dz {d['dndz']}) +{w}")
    for d in supers[:3]:
        w = 1.5 if d["z_hi"] < 1500 else 0.75
        s += w
        notes.append(f"superrefractive {d['z_lo']:.0f}-{d['z_hi']:.0f} m "
                     f"(dN/dz {d['dndz']}) +{w}")
    if invs and not ducts:
        s += 1.0
        notes.append(f"elevated inversion {invs[0]['z_lo']:.0f} m (+1)")
    return min(s, 8.0), notes


def surface_score():
    """v0's METAR component, condensed (0-4)."""
    try:
        obs = json.loads(fetch(
            "https://api.weather.gov/stations/KDCA/observations/latest"))[
            "properties"]
        wind_kt = (obs["windSpeed"]["value"] or 0) / 1.852
        temp = obs["temperature"]["value"]
        dew = obs["dewpoint"]["value"]
        s = 0.0
        notes = []
        if wind_kt <= 5:
            s += 2; notes.append(f"calm {wind_kt:.0f} kt (+2)")
        elif wind_kt <= 10:
            s += 1; notes.append(f"light wind {wind_kt:.0f} kt (+1)")
        if temp is not None and dew is not None and temp - dew <= 6:
            s += 2; notes.append(f"moist, spread {temp-dew:.1f}C (+2)")
        return s, notes
    except Exception as e:
        return 0.0, [f"(surface fetch failed: {e})"]


def main():
    t, rows = get_profile()
    if not rows:
        print("no sounding available — falling back to v0 surface only")
        aloft, anotes = 0.0, ["(no sounding)"]
    else:
        layers, ducts, supers, invs = duct_analysis(rows)
        aloft, anotes = score_aloft(ducts, supers, invs)
        print(f"sounding 72403 IAD @ {t:%Y-%m-%d %HZ}: "
              f"{len(rows)} levels, {len(ducts)} duct / "
              f"{len(supers)} superrefractive layers below 4 km")
    surf, snotes = surface_score()
    total = round(aloft + surf, 1)
    verdict = ("PRIME — expect an enhanced window" if total >= 7 else
               "decent — normal window likely" if total >= 4 else
               "poor — temper expectations")
    out = {"event": "dawn-score2", "score": total,
           "aloft": round(aloft, 1), "surface": round(surf, 1),
           "verdict": verdict,
           "t": datetime.now().strftime("%H:%M:%S")}
    with open(HERE / "cube_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(out) + "\n")
    print(f"DAWN SCORE v2: {total}/12 (aloft {aloft} + surface {surf}) "
          f"— {verdict}")
    for n in anotes + snotes:
        print("  ", n)


if __name__ == "__main__":
    main()
