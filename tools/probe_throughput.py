"""Stream-throughput diagnostic: open the SDR, set up a real RX stream
identical to tv_live's, pull samples for N seconds, and report:

    - samples actually delivered  vs.  samples expected at the
      requested rate (catches dropped buffers / under-runs).
    - first-/last-second power level and DC offset (catches frontend
      mute or silent saturation).
    - any flags returned per buffer (catches OVERFLOW signaling).

Use when SoapySDRUtil --probe and probe_sdr.py both look correct but
the GNU Radio flowgraph still produces zero PN511 hits — i.e. the
short bursts work but real-time streaming doesn't (a classic
WSL2 / USB-over-IP failure mode).

    python3 tools/probe_throughput.py [--rf 36] [--seconds 5]
"""
from __future__ import annotations
import argparse
import sys
import time

try:
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32, SOAPY_SDR_OVERFLOW
    import numpy as np
except ImportError as e:
    print(f"[probe-tp] missing dep: {e}")
    sys.exit(2)


ATSC_RF_BASE_HZ = {
    2:  57_000_000,  3:  63_000_000,  4:  69_000_000,  5:  79_000_000,
    6:  85_000_000,  7: 177_000_000,  8: 183_000_000,  9: 189_000_000,
    10: 195_000_000, 11: 201_000_000, 12: 207_000_000, 13: 213_000_000,
}


def rf_to_hz(rf: int) -> int:
    if rf in ATSC_RF_BASE_HZ:
        return ATSC_RF_BASE_HZ[rf]
    if 14 <= rf <= 51:
        return 473_000_000 + (rf - 14) * 6_000_000
    raise ValueError(f"invalid RF channel: {rf}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=36,
                    help="RF channel to tune (default: 36 = 605 MHz Fox 5 DC)")
    ap.add_argument("--seconds", type=float, default=5.0,
                    help="streaming duration (seconds)")
    ap.add_argument("--sample-rate", type=float, default=8_000_000)
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--ifgr", type=float, default=59)
    ap.add_argument("--rfgain-sel", type=int, default=5)
    ap.add_argument("--soapy-args", default="driver=sdrplay",
                    help="SoapySDR device specifier. Default 'driver=sdrplay'. "
                         "For SoapyRemote (e.g. WSL2 -> Windows host), use "
                         "'driver=remote,remote=<host>:55132,"
                         "remote:driver=sdrplay'.")
    args = ap.parse_args()

    freq = rf_to_hz(args.rf)
    print(f"[probe-tp] RF {args.rf} -> {freq/1e6:.3f} MHz "
          f"(center, equivalent to channel center)")
    print(f"[probe-tp] SoapySDR device: {args.soapy_args}")

    sdr = SoapySDR.Device(args.soapy_args)
    sdr.setSampleRate(SOAPY_SDR_RX, 0, args.sample_rate)
    sdr.setAntenna(SOAPY_SDR_RX, 0, args.antenna)
    try:
        sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", args.ifgr)
    except Exception as e:
        print(f"[probe-tp] WARN: setGain(IFGR) failed: {e}")
    try:
        sdr.writeSetting("rfgain_sel", str(args.rfgain_sel))
    except Exception as e:
        print(f"[probe-tp] WARN: writeSetting(rfgain_sel) failed: {e}")
    sdr.setFrequency(SOAPY_SDR_RX, 0, freq)
    actual_rate = sdr.getSampleRate(SOAPY_SDR_RX, 0)
    print(f"[probe-tp] sample rate: requested {args.sample_rate/1e6:.3f} -> "
          f"got {actual_rate/1e6:.6f} MS/s")

    rx = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    sdr.activateStream(rx)
    buf = np.zeros(8192, np.complex64)
    expected = int(actual_rate * args.seconds)
    print(f"[probe-tp] streaming {args.seconds:.1f}s; expecting "
          f"~{expected:,} samples at {actual_rate/1e6:.3f} MS/s")

    total = 0
    overflows = 0
    timeouts = 0
    errors = 0
    head_pwr = None
    head_dc  = None
    deadline = time.time() + args.seconds
    while time.time() < deadline:
        sr = sdr.readStream(rx, [buf], len(buf), timeoutUs=int(0.5e6))
        if sr.ret > 0:
            total += sr.ret
            if head_pwr is None:
                samples = buf[:sr.ret]
                head_pwr = float(np.mean(np.abs(samples) ** 2))
                head_dc  = float(np.mean(samples).real)
            if sr.flags & SOAPY_SDR_OVERFLOW:
                overflows += 1
        elif sr.ret == -1:  # timeout
            timeouts += 1
        else:
            errors += 1
            print(f"[probe-tp] readStream error code {sr.ret} "
                  f"flags={sr.flags}")

    sdr.deactivateStream(rx)
    sdr.closeStream(rx)

    delivered_pct = 100.0 * total / max(expected, 1)
    print()
    print(f"[probe-tp] samples delivered: {total:,} "
          f"({delivered_pct:.1f}% of expected)")
    print(f"[probe-tp] overflow events:   {overflows}")
    print(f"[probe-tp] read timeouts:     {timeouts}")
    print(f"[probe-tp] read errors:       {errors}")
    if head_pwr is not None:
        print(f"[probe-tp] first-buffer mean |x|²: {head_pwr:.6e}")
        print(f"[probe-tp] first-buffer DC offset: {head_dc:+.4f}")

    # Diagnosis hints.
    print()
    if delivered_pct < 90:
        print("[probe-tp] ⚠  significant under-delivery — USB/host can't keep")
        print("[probe-tp]    up at the requested rate. On WSL2 this is the")
        print("[probe-tp]    usbipd-over-TCP bottleneck; lower the rate")
        print("[probe-tp]    (try 6 MS/s) or run native Linux.")
    elif overflows > 0:
        print("[probe-tp] ⚠  driver reported overflow events — the host process")
        print("[probe-tp]    isn't draining buffers fast enough.")
    elif head_pwr is not None and head_pwr < 1e-7:
        print("[probe-tp] ⚠  near-zero signal power — antenna unplugged, wrong")
        print("[probe-tp]    antenna port, or RF gain too low.")
    else:
        print("[probe-tp] ✓  stream looks healthy at the SoapySDR level.")
        print("[probe-tp]    If tv_live still doesn't decode, the issue is")
        print("[probe-tp]    inside GNU Radio (block versions, sample-rate")
        print("[probe-tp]    handoff, or buffer alignment), not the SDR feed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
