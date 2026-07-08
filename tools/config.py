"""Project-wide configuration constants.

GR-first SDR Agent. Nothing here imports gr/Soapy/numpy/etc. — kept
dependency-free so it loads in any context.
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
DATA_DIR = PROJECT_ROOT / "data"
MODELS_DIR = PROJECT_ROOT / "models"
TRAINING_DATA_DIR = PROJECT_ROOT / "training_data"

# ────────────────────────────────────────────────────────────
# SDR
# ────────────────────────────────────────────────────────────
# SDRplay RSPdx — 8 MS/s native, sweet spot for ATSC + ham.
DEFAULT_SAMPLE_RATE = 8_000_000
SCAN_SAMPLE_RATE = 8_000_000
ADSB_SAMPLE_RATE = 2_400_000
RECORDING_SAMPLE_RATE = 48_000

# Discone is effective above 25 MHz; HF below that is mostly noise.
ANTENNA_RX_MIN = 25_000_000
ANTENNA_RX_MAX = 1_700_000_000

SDR_DEVICE = {
    "driver": "sdrplay",
    "antenna": "Antenna C",     # discone default; UHF TV switches to A
    "lna_state": 4,
    "if_gain_reduction": 40,
}

# ────────────────────────────────────────────────────────────
# ATSC TV — proven recipe (validated 2026-05-01)
# ────────────────────────────────────────────────────────────
# **ANTENNA RULE (do not change):** Antenna A = TV (ALL ATSC, both VHF-hi
# and UHF). Antenna C = everything else (HF/MW/FM/scanner). The discone
# rooftop feed is wired to Antenna A; switching to C breaks TV reception
# even on UHF. Gain recipe varies BY BAND (UHF vs VHF-hi) but the
# antenna assignment is fixed.
ATSC_ANTENNA = "Antenna A"
ATSC_IF_GAIN_REDUCTION = 59          # UHF default — VHF-hi may need lower
ATSC_RFGAIN_SEL = 5                  # UHF default — VHF-hi may need higher
ATSC_COMBO = "fpll_a002_tau20"       # alpha=0.002, AFC tau=20us
ATSC_DEFAULT_RF_CHANNEL = 34         # WRC NBC HD in DC market
ATSC_LIVE_TCP_PORT = 5559

# Per-band gain recipes for the channel scanner. UHF stations are usually
# stronger; VHF-hi (RF 7-13) needs more LNA boost since lower-frequency
# signals attenuate less in the discone but the SDRplay front-end is
# tuned for UHF by default. RF channel ranges per US OTA ATSC spec:
#   VHF-Lo:  2-6   (rare in 2026 — most stations vacated)
#   VHF-Hi:  7-13
#   UHF:     14-51 (post-repack max; 37 reserved)
ATSC_BAND_RECIPES = {
    "vhf_lo": {"rf_lo": 2,  "rf_hi": 6,  "ifgr": 40, "rfgain_sel": 9},
    "vhf_hi": {"rf_lo": 7,  "rf_hi": 13, "ifgr": 45, "rfgain_sel": 9},
    "uhf":    {"rf_lo": 14, "rf_hi": 51, "ifgr": 59, "rfgain_sel": 5},
}


def atsc_recipe_for_rf(rf: int) -> dict:
    """Return the gain recipe for a given RF channel."""
    for band in ATSC_BAND_RECIPES.values():
        if band["rf_lo"] <= rf <= band["rf_hi"]:
            return band
    return ATSC_BAND_RECIPES["uhf"]  # default safe fallback

# ────────────────────────────────────────────────────────────
# Daemon
# ────────────────────────────────────────────────────────────
TICK_INTERVAL_SEC = 600
TARGETS_PER_TICK = 20
VOICE_HUNT_PER_TICK = 4
VOICE_HUNT_WAIT_SEC = 20

# Active learning
RETRAIN_AFTER_N_NEW_SAMPLES = 50
RETRAIN_VAL_ACC_FLOOR = 0.85
RETRAIN_BACKUP_KEEP = 5

# Meter park (915 MHz ISM)
METER_PARK_FREQS_HZ = (910_500_000, 915_000_000, 917_400_000)
ISM_CAPTURE_SEC = 60.0

# WWV time-sync attempt (HF, propagation-dependent)
WWV_FREQS_HZ = (2_500_000, 5_000_000, 10_000_000, 15_000_000, 20_000_000)

# ────────────────────────────────────────────────────────────
# Dashboard
# ────────────────────────────────────────────────────────────
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 5555

# ────────────────────────────────────────────────────────────
# Geo (used by APT pass scheduler + propagation grayline)
# Set STVT_LAT / STVT_LON / STVT_GRID env vars to your own
# coordinates (or edit here). Defaults are a metro-area-level
# placeholder, deliberately NOT anyone's home: precision beyond
# ~city level is never needed by any tool in this repo.
# ────────────────────────────────────────────────────────────
import os as _os
LATITUDE = float(_os.environ.get("STVT_LAT", "38.9"))
LONGITUDE = float(_os.environ.get("STVT_LON", "-77.0"))
GRID_SQUARE = _os.environ.get("STVT_GRID", "FM18")
