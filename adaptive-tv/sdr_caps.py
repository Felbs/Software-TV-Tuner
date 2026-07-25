"""SDR capability detection — the seed of 'any SDR' support.

Enumerates the attached SoapySDR device and reports a normalized capability
profile so the rest of the pipeline only probes knobs that actually exist.
This is where SDRplay-specific assumptions get isolated: instead of the
profiler hardcoding IFGR/rfgain_sel/rfnotch, it asks this module what the
device supports and adapts.

Normalized gain model:
  Every SDR exposes gain differently. SDRplay uses IFGR (a *reduction*, so
  higher = less gain) + rfgain_sel (LNA state). RTL-SDR uses a single 'TUNER'
  gain in dB (higher = more gain). Airspy has LNA/MIX/VGA stages. We map all
  of them to a single fraction 0.0 (min gain) .. 1.0 (max gain) and translate
  per-driver in set_gain_fraction().

Usage:
    python sdr_caps.py            # detect + print capability report
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import SoapySDR
SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
from SoapySDR import SOAPY_SDR_RX


# Per-driver gain translation. fraction 0..1 -> native gain settings.
def _sdrplay_set_gain(sdr, frac):
    # IFGR range 20 (max gain) .. 59 (min gain) — INVERTED.
    ifgr = int(round(59 - frac * (59 - 20)))
    # rfgain_sel 0 (max LNA) .. ~9 (min). Use a coarse 0..4 sweep tied to fraction.
    rfgain = int(round((1 - frac) * 4))
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception:
        pass
    sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", float(ifgr))
    try:
        sdr.writeSetting("rfgain_sel", str(rfgain))
    except Exception:
        pass
    return {"IFGR": ifgr, "rfgain_sel": rfgain}


def _generic_set_gain(sdr, frac):
    # Use overall gain range reported by the driver.
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception:
        pass
    rng = sdr.getGainRange(SOAPY_SDR_RX, 0)
    g = rng.minimum() + frac * (rng.maximum() - rng.minimum())
    sdr.setGain(SOAPY_SDR_RX, 0, g)
    return {"gain_db": round(g, 1)}


class SDRCaps:
    def __init__(self, device_args: dict):
        self.device_args = device_args
        self.driver = device_args.get("driver", "?")
        self.label = device_args.get("label", "?")
        self.serial = device_args.get("serial", "?")
        self.antennas: list[str] = []
        self.sample_rates: list[float] = []
        self.max_sample_rate: float = 0.0
        self.freq_range = (0.0, 0.0)
        self.gain_elements: list[str] = []
        self.settings_keys: list[str] = []
        self.has_rf_notch = False
        self.has_dab_notch = False
        self.has_bias_t = False
        self.gain_setter = _generic_set_gain   # default

    def _make_args(self) -> str:
        # SoapySDR Python wants a "key=val,key=val" STRING, not a dict, and not
        # the 'label' key from enumerate(). driver alone is enough for a single
        # device; serial disambiguates multiple identical units.
        s = f"driver={self.driver}"
        if self.serial and self.serial != "?":
            s += f",serial={self.serial}"
        return s

    def probe(self):
        sdr = SoapySDR.Device(self._make_args())
        self.antennas = list(sdr.listAntennas(SOAPY_SDR_RX, 0))
        try:
            srs = sdr.listSampleRates(SOAPY_SDR_RX, 0)
            self.sample_rates = [float(s) for s in srs]
        except Exception:
            # Continuous range — query the range list instead
            try:
                self.sample_rates = [r.maximum() for r in sdr.getSampleRateRange(SOAPY_SDR_RX, 0)]
            except Exception:
                self.sample_rates = []
        self.max_sample_rate = max(self.sample_rates) if self.sample_rates else 0.0
        try:
            fr = sdr.getFrequencyRange(SOAPY_SDR_RX, 0)
            self.freq_range = (fr[0].minimum(), fr[-1].maximum())
        except Exception:
            self.freq_range = (0.0, 0.0)
        try:
            self.gain_elements = list(sdr.listGains(SOAPY_SDR_RX, 0))
        except Exception:
            self.gain_elements = []
        # Settings keys (driver-specific knobs like rfnotch_ctrl)
        try:
            infos = sdr.getSettingInfo()
            self.settings_keys = [i.key for i in infos]
        except Exception:
            self.settings_keys = []
        self.has_rf_notch  = "rfnotch_ctrl" in self.settings_keys
        self.has_dab_notch = "dabnotch_ctrl" in self.settings_keys
        self.has_bias_t    = any("bias" in k.lower() for k in self.settings_keys)

        # Pick gain translation strategy
        if self.driver == "sdrplay" or "IFGR" in self.gain_elements:
            self.gain_setter = _sdrplay_set_gain
        else:
            self.gain_setter = _generic_set_gain
        return self

    def report(self) -> str:
        lines = [
            f"  driver:      {self.driver}",
            f"  label:       {self.label}",
            f"  serial:      {self.serial}",
            f"  antennas:    {self.antennas}",
            f"  freq range:  {self.freq_range[0]/1e6:.1f} - {self.freq_range[1]/1e6:.1f} MHz",
            f"  max samp:    {self.max_sample_rate/1e6:.1f} MS/s",
            f"  gain elems:  {self.gain_elements or '(single overall gain)'}",
            f"  RF notch:    {'yes (rfnotch_ctrl)' if self.has_rf_notch else 'no'}",
            f"  DAB notch:   {'yes (dabnotch_ctrl)' if self.has_dab_notch else 'no'}",
            f"  bias-T:      {'yes' if self.has_bias_t else 'no'}",
            f"  gain model:  {'SDRplay (IFGR inverted)' if self.gain_setter is _sdrplay_set_gain else 'generic dB'}",
        ]
        return "\n".join(lines)

    def best_tv_antenna(self) -> str:
        """Pick the most TV-appropriate antenna port. RSPdx: 'Antenna A' is the
        full-spectrum port (best for TV). Others: first port."""
        for pref in ("Antenna A", "RX", "LNAW", "ANT"):
            if pref in self.antennas:
                return pref
        return self.antennas[0] if self.antennas else "RX"


def detect_first_sdr() -> SDRCaps | None:
    devs = SoapySDR.Device.enumerate()
    # Prefer a TV-capable driver if multiple present
    priority = ["sdrplay", "airspy", "rtlsdr", "hackrf"]
    def rank(d):
        drv = dict(d).get("driver", "")
        return priority.index(drv) if drv in priority else 99
    devs_sorted = sorted((dict(d) for d in devs), key=rank)
    if not devs_sorted:
        return None
    caps = SDRCaps(devs_sorted[0]).probe()
    return caps


if __name__ == "__main__":
    print("[sdr_caps] enumerating SoapySDR devices...")
    devs = SoapySDR.Device.enumerate()
    if not devs:
        print("  NO devices found. Check USB / driver / PATH.")
        sys.exit(1)
    print(f"  found {len(devs)} device(s):")
    for d in devs:
        print(f"    {dict(d)}")
    print()
    caps = detect_first_sdr()
    print("[sdr_caps] capability report for selected device:")
    print(caps.report())
