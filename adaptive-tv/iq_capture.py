"""iq_capture.py — record raw IQ from the SDR to disk (hypothesis H4
foundation: offline multi-pass decode, deep signal autopsies).

Fixed gain (no AGC) so amplitude dynamics in the file are the CHANNEL's,
not the servo's. Reports read continuity — a capture with drops is
labeled, never silently trusted (liveness law applied to recording).

Usage:
  python iq_capture.py --rf 7 --secs 4
  python iq_capture.py --freq 177e6 --secs 2 --rate 8e6
Output: lab/iq_rf<N>_<stamp>.ciq (interleaved int16 I,Q) + .json sidecar.
"""
import argparse
import json
import time
from pathlib import Path

import numpy as np
import SoapySDR
from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16

SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)

HERE = Path(__file__).resolve().parent
LAB = HERE / "lab"


def center_hz(rf):
    lo = (174 + (rf - 7) * 6) if rf < 14 else (470 + (rf - 14) * 6)
    return (lo + 3.0) * 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=None)
    ap.add_argument("--freq", type=float, default=None)
    ap.add_argument("--secs", type=float, default=4.0)
    ap.add_argument("--rate", type=float, default=8e6)
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--rfgain", default="3")
    ap.add_argument("--ifgr", type=float, default=40.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    freq = args.freq if args.freq else center_hz(args.rf)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    tag = f"rf{args.rf}" if args.rf else f"{freq/1e6:.0f}MHz"
    out = Path(args.out) if args.out else LAB / f"iq_{tag}_{stamp}.ciq"

    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, args.rate)
    sdr.setFrequency(SOAPY_SDR_RX, 0, freq)
    sdr.setAntenna(SOAPY_SDR_RX, 0, args.antenna)
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)   # AGC OFF — fixed gain
    except Exception:
        pass
    sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", args.ifgr)
    try:
        sdr.writeSetting("rfgain_sel", str(args.rfgain))
        if (args.rf and args.rf < 14) or freq < 240e6:
            sdr.writeSetting("dabnotch_ctrl", "false")   # never notch VHF
    except Exception:
        pass

    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16)
    sdr.activateStream(st)
    n_want = int(args.secs * args.rate)
    buf = np.empty(2 * 65536, np.int16)          # interleaved I,Q
    got = timeouts = shorts = 0
    t0 = time.time()
    t_first = None      # clock starts at FIRST sample — stream-activation
                        # latency is not a drop (metric fix 2026-07-05)
    # RAM capture (2026-07-07): the old write-per-read loop let any disk
    # stall overflow the SDR ring — the source of the chronic ~0.5-1%
    # sample drops that masqueraded as RF damage in every specimen.
    # Buffer the whole capture in RAM (<=60 s ~ 1.9 GB), write ONCE.
    if args.secs <= 60:
        ram = np.empty(2 * n_want, np.int16)
        while got < n_want and time.time() - t0 < args.secs * 3 + 10:
            r = sdr.readStream(st, [buf], 65536, timeoutUs=500000)
            if r.ret > 0:
                if t_first is None:
                    t_first = time.time()
                n = min(r.ret, n_want - got)
                ram[2 * got:2 * (got + n)] = buf[:2 * n]
                got += n
            elif r.ret == SoapySDR.SOAPY_SDR_TIMEOUT:
                timeouts += 1
            else:
                shorts += 1
        sdr.deactivateStream(st)
        sdr.closeStream(st)
        with open(out, "wb") as f:
            f.write(ram[:2 * got].tobytes())
    else:
        with open(out, "wb") as f:
            while got < n_want and time.time() - t0 < args.secs * 3 + 10:
                r = sdr.readStream(st, [buf], 65536, timeoutUs=500000)
                if r.ret > 0:
                    if t_first is None:
                        t_first = time.time()
                    n = min(r.ret, n_want - got)
                    f.write(buf[:2 * n].tobytes())
                    got += n
                elif r.ret == SoapySDR.SOAPY_SDR_TIMEOUT:
                    timeouts += 1
                else:
                    shorts += 1
        sdr.deactivateStream(st)
        sdr.closeStream(st)

    elapsed = (time.time() - t_first) if t_first else 1.0
    continuity = got / max(1.0, elapsed * args.rate)
    meta = {"file": out.name, "center_hz": freq, "rate": args.rate,
            "rf": args.rf, "antenna": args.antenna,
            "rfgain_sel": args.rfgain, "ifgr": args.ifgr,
            "samples": got, "secs": round(got / args.rate, 3),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeouts": timeouts, "errs": shorts,
            "continuity_pct": round(100 * min(1.0, continuity), 1),
            "format": "interleaved int16 I,Q"}
    out.with_suffix(".json").write_text(json.dumps(meta, indent=1),
                                        encoding="utf-8")
    print(json.dumps(meta, indent=1))
    if meta["continuity_pct"] < 97:
        print("WARNING: capture has drops — dynamics analysis unreliable")


if __name__ == "__main__":
    main()
