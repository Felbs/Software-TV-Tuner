"""dawn_score.py — Physics Ladder P2 v0: score the coming dawn for
radiative-inversion tropo enhancement from live NWS observations.

The dawn window (RF9's 05:30-06:30) is a temperature inversion:
clear skies + light wind + moist air near the surface let the ground
radiate heat away overnight, bending VHF back to earth at sunrise.
Score 0-10 from the latest DCA observation:

    wind      calm is king      (<=5 kt: +4, <=10: +2, else 0)
    sky       clear radiates    (CLR/FEW: +3, SCT: +1.5, else 0)
    dewpoint  moisture gradient (spread <= 5C: +2, <=10: +1, else 0)
    pressure  settled air       (>= 1018 hPa: +1)

Logged to cube_log.jsonl so morning-after ambush results calibrate the
scale — every dawn becomes a (forecast, catch) training pair.

    python dawn_score.py
"""
import json
import urllib.request
from datetime import datetime
from pathlib import Path

OBS_URL = "https://api.weather.gov/stations/KDCA/observations/latest"
HERE = Path(__file__).parent


def c(v):
    return None if v is None else float(v)


def main():
    req = urllib.request.Request(
        OBS_URL, headers={"User-Agent": "tv-tuna-dawn-score"})
    with urllib.request.urlopen(req, timeout=30) as r:
        obs = json.load(r)["properties"]

    temp = c(obs["temperature"]["value"])
    dew = c(obs["dewpoint"]["value"])
    wind_kt = (c(obs["windSpeed"]["value"]) or 0) / 1.852  # km/h -> kt
    press = c(obs["barometricPressure"]["value"])
    press_hpa = press / 100.0 if press else None
    clouds = [l["amount"] for l in obs.get("cloudLayers", [])]
    sky = clouds[0] if clouds else "CLR"

    score = 0.0
    notes = []
    if wind_kt <= 5:
        score += 4; notes.append(f"calm wind {wind_kt:.0f} kt (+4)")
    elif wind_kt <= 10:
        score += 2; notes.append(f"light wind {wind_kt:.0f} kt (+2)")
    else:
        notes.append(f"windy {wind_kt:.0f} kt (+0) — mixing kills inversions")
    if sky in ("CLR", "SKC", "FEW"):
        score += 3; notes.append(f"sky {sky} (+3) — radiative cooling on")
    elif sky == "SCT":
        score += 1.5; notes.append(f"sky {sky} (+1.5)")
    else:
        notes.append(f"sky {sky} (+0) — cloud blanket blocks cooling")
    if temp is not None and dew is not None:
        spread = temp - dew
        if spread <= 5:
            score += 2; notes.append(f"dewpoint spread {spread:.1f}C (+2)")
        elif spread <= 10:
            score += 1; notes.append(f"dewpoint spread {spread:.1f}C (+1)")
        else:
            notes.append(f"dry air, spread {spread:.1f}C (+0)")
    if press_hpa and press_hpa >= 1018:
        score += 1; notes.append(f"high pressure {press_hpa:.0f} hPa (+1)")

    verdict = ("PRIME dawn — expect an enhanced window" if score >= 7 else
               "decent dawn — normal window likely" if score >= 4 else
               "poor dawn — inversion unlikely, temper expectations")
    out = {"event": "dawn-score", "score": round(score, 1),
           "verdict": verdict, "temp_c": temp, "dew_c": dew,
           "wind_kt": round(wind_kt, 1), "sky": sky,
           "press_hpa": round(press_hpa, 1) if press_hpa else None,
           "t": datetime.now().strftime("%H:%M:%S")}
    with open(HERE / "cube_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(out) + "\n")
    print(f"DAWN SCORE {score:.1f}/10 — {verdict}")
    for n in notes:
        print("  ", n)


if __name__ == "__main__":
    main()
