"""dump_front_in.py — freeze the fused front end's INPUT to disk.

The §11.5(a) lock failure is nondeterministic on a deterministic FILE fixture.
There are only two possible homes for that nondeterminism:

  (A) inside atsc_wl_frontend (work-call-boundary state / chunking), or
  (B) upstream — the resampler/matched-filter volk kernels pick different
      summation orders per process (the 7/29 measurement-noise law), so the
      front end's INPUT SAMPLES differ by ~1e-7 between runs.

This script removes (B) from the experiment: it runs the exact tv_replay
upstream chain (scaler -> resampler -> rx filter -> fpll FOLD) once and writes
the fpll's two output planes as raw float32. `front_only.py` then replays those
BYTES into the front end N times. If failures survive => (A). If they vanish
=> (B).

  python lab/wl_watchdog/dump_front_in.py --seconds 3
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))

from gnuradio import gr, blocks, analog                    # noqa: E402
from gnuradio import filter as gr_filter                   # noqa: E402
from gnuradio import atscplus                              # noqa: E402

from tv_replay import (ATSC_SYMBOL_RATE, ATSC_RX_SAMPLE_RATE,   # noqa: E402
                       RESAMP_INTERP, RESAMP_DECIM, atsc_rx_filter)

OUT = REPO / "lab" / "wl_watchdog"


class Dumper(gr.top_block):
    def __init__(self, iq: Path, nsym: int, rdst: Path, idst: Path):
        gr.top_block.__init__(self, "wl front-end input dumper")
        SPS = float(os.environ.get("STVT_SPS", "1.5"))
        output_rate = ATSC_SYMBOL_RATE * SPS

        fsrc = blocks.file_source(gr.sizeof_short, str(iq), False)
        s2c = blocks.interleaved_short_to_complex(False, False, 32767.0)
        scaler = blocks.multiply_const_cc(32768.0)
        resamp = gr_filter.rational_resampler_ccc(interpolation=RESAMP_INTERP,
                                                  decimation=RESAMP_DECIM)
        rxf = atsc_rx_filter(ATSC_RX_SAMPLE_RATE, SPS)
        fpll = atscplus.atsc_fpll_tight(
            output_rate,
            float(os.environ.get("STVT_FPLL_ALPHA", "0.001")),
            float(os.environ.get("STVT_FPLL_AFC_TAU", "25")))
        # head-limit AFTER the fpll so the two planes are exactly co-indexed
        hr = blocks.head(gr.sizeof_float, nsym)
        hi = blocks.head(gr.sizeof_float, nsym)
        sr = blocks.file_sink(gr.sizeof_float, str(rdst)); sr.set_unbuffered(False)
        si = blocks.file_sink(gr.sizeof_float, str(idst)); si.set_unbuffered(False)

        self.connect(fsrc, s2c, scaler, resamp, rxf, fpll)
        self.connect((fpll, 0), hr, sr)
        self.connect((fpll, 1), hi, si)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iq", default=str(REPO / "lab" / "marginal_iq" / "rf34_ctrl.cs16"))
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--prefix", default="front_in")
    a = ap.parse_args()
    if not int(os.environ.get("STVT_FPLL_FOLD", "0")):
        os.environ["STVT_FPLL_FOLD"] = "1"      # the WL path requires FOLD
    rate = ATSC_SYMBOL_RATE * float(os.environ.get("STVT_SPS", "1.5"))
    n = int(a.seconds * rate)
    rdst = OUT / f"{a.prefix}_r.f32"
    idst = OUT / f"{a.prefix}_i.f32"
    print(f"dumping {n} samples/plane ({a.seconds}s @ {rate:.0f}) -> {rdst.name}, {idst.name}")
    tb = Dumper(Path(a.iq).resolve(), n, rdst, idst)
    tb.start(); tb.wait()
    print(f"done: {rdst.stat().st_size} + {idst.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
