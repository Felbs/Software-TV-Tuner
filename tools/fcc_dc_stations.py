"""DC/Baltimore DMA TV station table. Used as a label fallback when
gr-dtv-derived TS streams have no parseable PSIP (a known limitation
of the gr-dtv reference receiver).

Each row: (rf, virtual, callsign, network, city, subchannels)
  rf          : actual broadcast RF channel
  virtual     : "x.y" primary virtual channel
  callsign    : FCC callsign
  network     : major network on the .1 subchannel
  city        : city of license
  subchannels : optional list of (virtual, name) for .2/.3/etc. tuples

If a station moves RF channels, edit this file.
"""
from __future__ import annotations

# Format: rf, virtual, callsign, network, city, subchannels
DC_DMA_STATIONS = [
    # ── VHF-Hi (RF 7-13) ──────────────────────────────────────
    (7,  "7.1",  "WJLA", "ABC",       "Washington, DC", []),
    (9,  "9.1",  "WUSA", "CBS",       "Washington, DC", []),
    (11, "11.1", "WBAL", "NBC",       "Baltimore, MD",  []),  # weak from Dale City
    # ── UHF (RF 14-36) ────────────────────────────────────────
    (15, "14.1", "WFDC", "Univision", "Washington, DC",
        [("14.2", "GREAT"), ("14.3", "GRIT"), ("14.4", "UniMas")]),
    (16, "45.1", "WBFF", "Fox",       "Baltimore, MD",  []),  # weak
    (21, "50.1", "WDCW", "CW",        "Washington, DC",
        [("50.2", "Antenna")]),
    (22, "22.1", "WMPT", "PBS Maryland", "Annapolis, MD",
        [("22.2", "MPT-2"), ("22.3", "MPTKIDS"), ("22.4", "NHK-WLD")]),
    (25, "25.1", "WDVM", "Independent", "Hagerstown, MD", []),
    (27, "26.1", "WETA", "PBS",       "Arlington, VA",
        [("26.2", "WETA UK"), ("26.3", "KIDS"), ("26.4", "WORLD"), ("26.5", "METRO")]),
    (31, "66.1", "WPXW", "Ion",       "Manassas, VA",
        [("66.2", "Bounce"), ("66.3", "CourtTV"), ("66.4", "Laff"),
         ("66.5", "IONPlus"), ("66.6", "BUSTED"), ("66.7", "GameSho"), ("66.8", "HSN")]),
    (34, "4.1",  "WRC",  "NBC",       "Washington, DC",
        [("4.2", "COZI"), ("4.3", "CRIMES"), ("4.4", "Oxygen")]),
    (35, "20.1", "WDCA", "MyNetTV",   "Washington, DC",
        [("20.2", "MOVIES"), ("20.3", "HEROES"), ("20.4", "FOXWX")]),
    (36, "5.1",  "WTTG", "Fox",       "Washington, DC",
        [("5.2", "BUZZR"), ("5.3", "START")]),
    # ── UHF post-repack (RF 37-51) ────────────────────────────
    (38, "2.1",  "WMAR", "ABC",       "Baltimore, MD",  []),  # weak
    (44, "44.1", "WZDC", "Telemundo", "Washington, DC",
        [("44.2", "XITOS")]),
    (54, "54.1", "WNUV", "CW",        "Baltimore, MD",  []),  # weak
    (58, "58.1", "WIAV", "Subscription", "Arlington, VA",
        [("58.5", "24/7MMT")]),
    # Maryland Public TV satellites:
    (32, "67.1", "WMPB", "PBS Maryland", "Baltimore, MD", []),
]


def lookup(rf: int) -> dict | None:
    """Return station info for a given RF channel, or None if unknown."""
    for entry in DC_DMA_STATIONS:
        rf_, virtual, callsign, network, city, subs = entry
        if rf_ == rf:
            return {
                "rf": rf_,
                "virtual": virtual,
                "callsign": callsign,
                "network": network,
                "city": city,
                "label": f"{virtual} {callsign} {network}",
                "subchannels": subs,
            }
    return None


def all_stations() -> list[dict]:
    return [lookup(e[0]) for e in DC_DMA_STATIONS]
