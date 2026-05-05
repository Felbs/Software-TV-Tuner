"""One-shot SDR diagnostic — print what driver, antennas, and sample
rates the SoapySDR-attached SDR actually advertises.

Run when tv_live appears to start cleanly but produces no decoded TS
output (i.e. zero PN511 hits in the field-sync checker). Most common
cause: antenna name / sample-rate that the host SDR's driver silently
rejects or rounds. This tool surfaces the actual values.

Usage:
    python3 tools/probe_sdr.py
"""
from __future__ import annotations
import sys

try:
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX
except ImportError:
    print("[probe] SoapySDR Python bindings not installed.")
    print("[probe] Linux: sudo apt-get install -y python3-soapysdr")
    sys.exit(2)


def main() -> int:
    print("[probe] enumerating SoapySDR devices...")
    devs = SoapySDR.Device.enumerate()
    if not devs:
        print("[probe] NO devices found. Check USB / driver / permissions.")
        return 1
    for i, d in enumerate(devs):
        print(f"  device {i}: {dict(d)}")

    print("\n[probe] opening driver=sdrplay (the only one this project decodes)...")
    try:
        sdr = SoapySDR.Device("driver=sdrplay")
    except Exception as e:
        print(f"[probe] open failed: {e}")
        print("[probe] is the SDRplay daemon running? "
              "(Linux: sudo systemctl status sdrplay)")
        return 1

    print(f"[probe] hardware: {sdr.getHardwareKey()}")
    print(f"[probe] driver:   {sdr.getDriverKey()}")

    ants = sdr.listAntennas(SOAPY_SDR_RX, 0)
    print(f"[probe] antennas: {ants}")
    print(f"[probe] current antenna: {sdr.getAntenna(SOAPY_SDR_RX, 0)!r}")

    rates = sdr.getSampleRateRange(SOAPY_SDR_RX, 0)
    rates_str = ", ".join(f"[{r.minimum()/1e6:.3f}–{r.maximum()/1e6:.3f}] MS/s"
                          for r in rates)
    print(f"[probe] sample-rate ranges: {rates_str}")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, 8_000_000)
    actual = sdr.getSampleRate(SOAPY_SDR_RX, 0)
    print(f"[probe] requested 8.000 MS/s -> got {actual/1e6:.6f} MS/s "
          f"({'EXACT' if abs(actual - 8e6) < 1 else 'SNAPPED'})")

    # Try every antenna name the user might have configured.
    for candidate in ("Antenna A", "A", "Antenna B", "B", "Antenna C", "C"):
        if candidate in ants:
            sdr.setAntenna(SOAPY_SDR_RX, 0, candidate)
            got = sdr.getAntenna(SOAPY_SDR_RX, 0)
            mark = "OK " if got == candidate else "BAD"
            print(f"[probe] {mark} setAntenna({candidate!r}) -> {got!r}")

    print(f"[probe] gain elements: {sdr.listGains(SOAPY_SDR_RX, 0)}")
    print(f"[probe] full gain range: "
          f"[{sdr.getGainRange(SOAPY_SDR_RX, 0).minimum()}, "
          f"{sdr.getGainRange(SOAPY_SDR_RX, 0).maximum()}] dB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
