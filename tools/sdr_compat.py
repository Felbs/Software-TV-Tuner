"""sdr_compat.py — open WHATEVER SoapySDR radio is actually plugged in.

Born from GitHub issue #2: a PlutoSDR owner on Debian 13 whose radio
probed perfectly in SoapySDRUtil while every STVT tool insisted on
`driver=sdrplay` and reported "SDR not found". A universal TV decoder
should not be single-brand.

Resolution order for the device string:
  1. STVT_SOAPY_ARGS env (unchanged — the SoapyRemote/WSL pattern)
  2. an explicit --soapy-args on the CLI (unchanged)
  3. enumerate(): prefer sdrplay if present (the tuned default on the
     home rigs), otherwise take the first radio found and SAY SO.

Gain translation: STVT's knobs are SDRplay dialect (IFGR = IF gain
REDUCTION 20..59, rfgain_sel = LNA state). On any other radio we map
the IFGR "hotness" onto the device's real gain range:
    hot = (59 - ifgr) / 39          # 0 = coldest, 1 = hottest
    gain = min + hot * (max - min)  # from getGainRange()
It's a starting point, not a calibration — the quality tuners hill-
climb from there exactly as they do on SDRplay.

Antennas: try the requested name, fall back to the device's first
listed antenna (a Pluto has only A_BALANCED and that's fine).
"""
import os

try:
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX
except ImportError:                     # callers print their own advice
    SoapySDR = None
    SOAPY_SDR_RX = 0


def bindings_help():
    """Actionable text for the 'import SoapySDR failed' case — the
    classic Debian/Ubuntu by-hand-install gap where SoapySDRUtil works
    but the Python bindings are not on this interpreter's path."""
    return (
        "SoapySDR Python bindings not importable by THIS python.\n"
        "If `SoapySDRUtil --probe` works, the bindings are likely in\n"
        "/usr/local and not on your PYTHONPATH. Find and add them:\n"
        "  python3 -c \"import sys; print(sys.version_info[:2])\"\n"
        "  find /usr/local/lib -name 'SoapySDR.py' 2>/dev/null\n"
        "  export PYTHONPATH=<that directory>:$PYTHONPATH\n"
        "(add the export to ~/.bashrc once it works)"
    )


def resolve_soapy_args(default: str = "driver=sdrplay",
                       verbose: bool = True) -> str:
    """The device string STVT should open — see module docstring."""
    env = os.environ.get("STVT_SOAPY_ARGS")
    if env:
        return env
    if SoapySDR is None:
        return default
    try:
        devs = [dict(d) for d in SoapySDR.Device.enumerate()]
    except Exception:
        return default
    if not devs:
        return default
    # Sound cards & null devices always enumerate but can never be TV SDRs
    # (audio tops out at 192 kS/s vs ATSC's 8 MS/s). During the SDRplay API's
    # post-close release window the RSP briefly vanishes from enumeration,
    # which used to make this fall back to the MICROPHONE and die un-retried
    # at set_sample_rate (2026-07-26, the cross-mux flip killer). Never pick
    # them; with nothing eligible left, return the default so the caller's
    # open-retry loop waits out the release window instead.
    INELIGIBLE = ("audio", "null")
    devs = [d for d in devs if d.get("driver", "?") not in INELIGIBLE]
    if not devs:
        return default
    drivers = [d.get("driver", "?") for d in devs]
    if "sdrplay" in drivers:
        return "driver=sdrplay"
    chosen = devs[0]
    args = f"driver={chosen.get('driver', '?')}"
    if "serial" in chosen and len(devs) > 1:
        args += f",serial={chosen['serial']}"
    if verbose:
        print(f"[sdr_compat] no SDRplay found; using detected radio: "
              f"{chosen.get('driver')} ({chosen.get('label', 'no label')})."
              f" Override with STVT_SOAPY_ARGS.", flush=True)
    return args


def is_sdrplay(args: str) -> bool:
    return "sdrplay" in (args or "").lower()


def apply_rx_gain(sdr, soapy_args: str, ifgr: float, rfgain_sel) -> str:
    """Set receive gain from STVT's (ifgr, rfgain_sel) knobs on any
    radio. Returns a short description of what was applied."""
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception:
        pass
    if is_sdrplay(soapy_args):
        sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", float(ifgr))
        try:
            sdr.writeSetting("rfgain_sel", str(rfgain_sel))
        except Exception:
            pass
        return f"sdrplay IFGR={ifgr} rfgain_sel={rfgain_sel}"
    # generic radio: translate IFGR hotness onto the real gain range
    hot = max(0.0, min(1.0, (59.0 - float(ifgr)) / 39.0))
    try:
        rng = sdr.getGainRange(SOAPY_SDR_RX, 0)
        lo, hi = float(rng.minimum()), float(rng.maximum())
    except Exception:
        lo, hi = 0.0, 60.0
    gain = lo + hot * (hi - lo)
    sdr.setGain(SOAPY_SDR_RX, 0, gain)
    return (f"generic gain {gain:.1f} dB (range {lo:.0f}..{hi:.0f}, "
            f"from IFGR {ifgr})")


def apply_antenna(sdr, antenna: str) -> str:
    """Try the requested antenna; fall back to whatever the radio has."""
    try:
        sdr.setAntenna(SOAPY_SDR_RX, 0, antenna)
        return antenna
    except Exception:
        pass
    try:
        ants = list(sdr.listAntennas(SOAPY_SDR_RX, 0))
        if ants:
            sdr.setAntenna(SOAPY_SDR_RX, 0, ants[0])
            return f"{ants[0]} (requested '{antenna}' not on this radio)"
    except Exception:
        pass
    return f"(antenna untouched; '{antenna}' not settable)"
