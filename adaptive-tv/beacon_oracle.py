"""beacon_oracle.py — P4 v0: other people's transmitters as free sensors.

Propagation enhancement is broadband: a duct that lifts Baltimore TV
lifts Baltimore FM. With the discone now living on ANT-C (its FM-native
port), a 25-second sweep sounds every path we care about:

    DC          88.5 WAMU   90.9 WETA-FM   103.5 WTOP
    Baltimore   97.9 WIYY   101.9 WLIF
    Fredericksbg 93.3 WFLS

Output: per-path dB score (relative to each path's own rolling baseline
kept in beacon_baseline.json) — a positive Baltimore anomaly says "the
Baltimore duct is open, go fish RF21." Logged to cube_log.jsonl.

    python beacon_oracle.py            # one sounding
"""
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
BASE = HERE / "beacon_baseline.json"

PATHS = {
    "dc": [88.5, 90.9, 103.5],
    "baltimore": [97.9, 101.9],
    "fredericksburg": [93.3],
}
FS = 8_000_000


def sweep():
    """Power (dB) per station via 3 wideband hops on the discone/ANT-C."""
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
    SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
    # the SDR takes a few seconds to free after a chain dies — retry
    # like tv_live does instead of dying to 'no match'
    sdr = None
    for attempt in range(4):
        try:
            sdr = SoapySDR.Device(dict(driver="sdrplay"))
            break
        except RuntimeError:
            if attempt == 3:
                raise
            time.sleep(5)
    sdr.setAntenna(SOAPY_SDR_RX, 0, "Antenna C")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, FS)
    sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 59)       # FM blowtorch-safe
    try:
        sdr.writeSetting("rfgain_sel", "3")
    except Exception:
        pass
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    sdr.activateStream(st)
    buf = np.empty(262144, np.complex64)
    out = {}
    for center in (91.0, 99.0, 106.0):
        sdr.setFrequency(SOAPY_SDR_RX, 0, center * 1e6)
        time.sleep(0.35)
        for _ in range(3):                          # flush transient
            sdr.readStream(st, [buf], len(buf), timeoutUs=500000)
        acc = np.zeros(4096)
        for _ in range(6):
            r = sdr.readStream(st, [buf], len(buf), timeoutUs=500000)
            if r.ret <= 0:
                continue
            x = buf[:r.ret - (r.ret % 4096)].reshape(-1, 4096)
            acc += (np.abs(np.fft.fft(x, axis=1)) ** 2).mean(axis=0)
        psd = np.fft.fftshift(acc)
        fax = center + np.fft.fftshift(np.fft.fftfreq(4096, 1 / FS)) / 1e6
        for city, freqs in PATHS.items():
            for f in freqs:
                if abs(f - center) < FS / 2e6 - 0.2:
                    sel = (fax > f - 0.09) & (fax < f + 0.09)
                    if sel.any():
                        out[f] = 10 * np.log10(psd[sel].mean() + 1e-12)
    sdr.deactivateStream(st)
    sdr.closeStream(st)
    sdr.close()
    return out


def main():
    powers = sweep()
    try:
        base = json.loads(BASE.read_text())
    except (OSError, json.JSONDecodeError):
        base = {}
    scores = {}
    for city, freqs in PATHS.items():
        deltas = []
        for f in freqs:
            if f not in powers:
                continue
            key = str(f)
            b = base.get(key)
            if b is None:
                base[key] = powers[f]
            else:
                deltas.append(powers[f] - b)
                base[key] = 0.95 * b + 0.05 * powers[f]   # slow baseline
        scores[city] = round(float(np.mean(deltas)), 1) if deltas else None
    BASE.write_text(json.dumps(base, indent=1))
    ev = {"event": "beacon-oracle", "paths_db": scores,
          "raw": {str(k): round(v, 1) for k, v in powers.items()},
          "t": datetime.now().strftime("%H:%M:%S")}
    with open(HERE / "cube_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(ev) + "\n")
    print("BEACON ORACLE (dB vs baseline):", scores)
    for f, p in sorted(powers.items()):
        print(f"  {f:6.1f} MHz  {p:6.1f} dB")
    hot = [c for c, s in scores.items() if s is not None and s > 3]
    if hot:
        print("PATHS RUNNING HOT:", hot, "— consider fishing their TV")


if __name__ == "__main__":
    main()
