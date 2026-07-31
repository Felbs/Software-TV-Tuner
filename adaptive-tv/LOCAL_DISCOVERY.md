# LOCAL_DISCOVERY.md — the tuner figures out where it is by listening

User directive (2026-07-07): this is open-source; anyone anywhere should
download it and get a UI personalized to THEIR airwaves — never the
developer's. No location baked into universal paths, no doxxing; the
rig should *discover* its surroundings from the radio itself.

## Rules (enforced tonight)
1. No coordinates, grid squares, station ids, or city names hardcoded
   in universal code paths. Env-first: STVT_LAT/LON/GRID,
   STVT_RADIOSONDE, STVT_BEACONS. Shipped defaults are labeled
   examples at metro-area coarseness or coarser.
2. Rig-specific lab scripts (overnight campaigns, stress harnesses) may
   carry the dev rig's values — they are experiment notebooks, not the
   product — but must say so in their docstring.
3. Everything the UI states as fact should be MEASURED on the user's
   rig (survival curve, time knob, gains) or derived from their scan.

## The self-discovery ladder (roadmap)
- **L1 — market fingerprint (nearly free):** the scanner already
  extracts PSIP: callsigns + city of license per channel. One scan =
  the user's media market, no internet needed. UI header becomes
  "Your airwaves: 14 stations detected (WXXX, WYYY, …)".
- **L2 — coarse position:** callsign prefix (K/W split at the
  Mississippi), city-of-license strings, and relative pilot strengths
  triangulate to metro-level location. Optional FCC LMS station
  database (public) turns callsigns into tower coordinates + true
  bearings — enabling the flatness/aiming tools to name real azimuths
  ("aim 48° for the RF7 tower") anywhere on the continent.
- **L3 — auto-instruments:** from L2's coarse location, auto-pick the
  nearest radiosonde station (public NOAA/Wyoming list) for the dawn
  forecast, and auto-build the FM beacon list from a 88-108 MHz sweep
  (strongest stable carriers = path-sounders; distant-city fishing
  lists derived from the FM database). Zero configuration, zero
  personal data stored — everything derived from RF, on-device.
- **L4 — the personalized science page:** every education card cites
  the user's OWN numbers (their survival curve fit, their per-hour
  antenna map, their echo profile) — the dev rig's history never ships.

## Privacy stance
All discovery is passive listening + public broadcast metadata,
computed and stored locally. Nothing phones home. The scan map is the
user's; sharing it is their choice.
