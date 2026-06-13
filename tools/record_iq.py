#!/usr/bin/env python3
"""record_iq.py — capture raw IQ samples from the SDR to a CF32 file.

Output is a "complex float32" (CF32) file: pairs of float32 I/Q samples,
suitable for replay through tv_live_filesrc.py for repeatable offline
chain testing. No demod; just the SDR's raw output.

Usage:
    python3 tools/record_iq.py --rf 34 --seconds 90 --out /tmp/cap_rf34.cf32

Defaults: 90 seconds @ 8 MS/s = ~5.7 GB. Plenty for chain debugging.
Sample rate / antenna / IFGR / rfgain_sel match tv_live.py defaults so
the replay reproduces the same conditions.
"""
from __future__ import annotations
import argparse
import os
import sys
import time

from gnuradio import gr, blocks, soapy


ATSC_NATIVE_SAMPLE_RATE = 8_000_000


def rf_to_freq_hz(rf_channel: int) -> int:
    """ATSC RF channel → carrier frequency Hz (US table)."""
    if rf_channel == 2:   return 57_000_000
    if rf_channel == 3:   return 63_000_000
    if rf_channel == 4:   return 69_000_000
    if rf_channel == 5:   return 79_000_000
    if rf_channel == 6:   return 85_000_000
    if 7 <= rf_channel <= 13:
        return 177_000_000 + (rf_channel - 7) * 6_000_000
    if 14 <= rf_channel <= 36:
        return 473_000_000 + (rf_channel - 14) * 6_000_000
    raise ValueError(f"unknown RF channel {rf_channel}")


class RecordIQ(gr.top_block):
    def __init__(self, rf: int, out_path: str, ifgr: int, rfgain_sel: int,
                 antenna: str, sample_rate: int, fmt: str = "cf32"):
        super().__init__("record_iq")
        freq = rf_to_freq_hz(rf)
        print(f"[record_iq] RF {rf} = {freq/1e6:.3f} MHz, antenna={antenna}, "
              f"IFGR={ifgr}, rfgain_sel={rfgain_sel}, rate={sample_rate}",
              flush=True)
        src = soapy.source("driver=sdrplay", "fc32", 1, "", "", [""], [""])
        src.set_sample_rate(0, sample_rate)
        src.set_frequency(0, freq)
        src.set_antenna(0, antenna)
        try: src.set_gain_mode(0, False)
        except Exception: pass
        try: src.set_gain(0, "IFGR", float(ifgr))
        except Exception:
            try: src.set_gain(0, float(ifgr))
            except Exception: pass
        try: src.write_setting("rfgain_sel", str(rfgain_sel))
        except Exception: pass

        if fmt == "cs16":
            # Interleaved int16 = 4 B/sample, HALF of CF32's 8 B (more record time
            # per GB of disk). short = fc32 * 32767; tv_replay reads it back with
            # interleaved_short_to_complex(/32767). Round-trip verified exact to the
            # int16 quantum. CAVEAT: clips if |fc32|>1 — keep gains so the SDR isn't
            # near full-scale (IFGR/rfgain tuned for that already).
            c2is = blocks.complex_to_interleaved_short(False, 32767.0)
            sink = blocks.file_sink(gr.sizeof_short, out_path)
            sink.set_unbuffered(False)
            self.connect(src, c2is, sink)
            print("[record_iq] format=cs16 (interleaved int16, 4 B/sample)", flush=True)
        else:
            sink = blocks.file_sink(gr.sizeof_gr_complex, out_path)
            sink.set_unbuffered(False)
            self.connect(src, sink)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rf", type=int, required=True)
    p.add_argument("--seconds", type=int, default=90)
    p.add_argument("--out", required=True, help="Output CF32 file path")
    p.add_argument("--ifgr", type=int, default=59)
    p.add_argument("--rfgain-sel", type=int, default=5)
    p.add_argument("--antenna", default="Antenna A")
    p.add_argument("--sample-rate", type=int, default=ATSC_NATIVE_SAMPLE_RATE)
    p.add_argument("--format", choices=("cf32", "cs16"), default="cf32",
                   help="cf32=complex float32 (8 B/sample, default); "
                        "cs16=interleaved int16 (4 B/sample, half the disk). "
                        "Decode a cs16 file by naming it .cs16 (tv_replay auto-detects).")
    args = p.parse_args()

    bytes_per = 4 if args.format == "cs16" else 8
    est_bytes = args.seconds * args.sample_rate * bytes_per
    print(f"[record_iq] estimated output size: {est_bytes/1e9:.1f} GB "
          f"({args.format})", flush=True)

    tb = RecordIQ(args.rf, args.out, args.ifgr, args.rfgain_sel,
                  args.antenna, args.sample_rate, args.format)
    tb.start()
    print(f"[record_iq] recording for {args.seconds}s...", flush=True)
    t0 = time.time()
    try:
        while time.time() - t0 < args.seconds:
            time.sleep(1)
            if os.path.exists(args.out):
                sz = os.path.getsize(args.out)
                print(f"[record_iq] t={int(time.time()-t0):3d}s  "
                      f"size={sz/1e9:.2f} GB", flush=True)
    finally:
        tb.stop()
        tb.wait()
    sz = os.path.getsize(args.out) if os.path.exists(args.out) else 0
    print(f"[record_iq] DONE — captured {sz/1e9:.2f} GB to {args.out}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
