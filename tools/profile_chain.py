#!/usr/bin/env python3
"""Where the CPU goes in tv_live.py's chain, block by block, on a capture.

  python tools/profile_chain.py CAPTURE.cs16 [--rf 36] [--rate 8e6]

Runs the real `LiveTVTopBlock` (every STVT_* environment lever honoured, so set them as you
would for tv_live) from the capture, with GNU Radio's per-block performance counters, and
prints: seconds of air, wall time, process CPU, and each block's CPU with its share. GNU
Radio runs one thread per block, so the block with the largest number is what sets the
pace of the whole chain - that is the one to optimise.

The counters count only work() time; the scheduler's own overhead shows up in the process
CPU total and not in any row. Set no STVT_* variable to measure the defaults.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# the counters are configured when the first block is made, so before any gnuradio import
os.environ.setdefault("GR_CONF_PERFCOUNTERS_ON", "True")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from gnuradio import gr  # noqa: E402

import chain_replay  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--rf", type=int, default=36)
    ap.add_argument("--rate", type=float, default=None, help="capture sample rate (default: the chain's native)")
    ap.add_argument("--ts", default=None, help="keep the decoded TS here (default: a temp file)")
    a = ap.parse_args()
    ts = a.ts or os.path.join(tempfile.gettempdir(), "stvt_profile.ts")
    tb = chain_replay.build(a.capture, ts, rf=a.rf, native_rate=a.rate)
    wall, cpu = chain_replay.run(tb)
    air = tb.air_seconds
    levers = {k: v for k, v in os.environ.items() if k.startswith("STVT_")}
    print(f"levers : {levers if levers else 'defaults'}")
    print(f"air    : {air:.1f} s;  wall {wall:.1f} s -> {air / max(wall, 1e-9):.2f}x real time;  "
          f"process CPU {cpu:.1f} s = {cpu / max(wall, 1e-9):.1f} cores")
    tps = float(gr.high_res_timer_tps())                   # counters are in high-res timer ticks
    rows = []
    for name, blk in tb.named_blocks:
        try:
            rows.append((name, blk.pc_work_time_total() / tps))
        except AttributeError:
            rows.append((name, None))                      # hier / python blocks have no counter
    counted = [r for r in rows if r[1] is not None]
    if not counted or sum(r[1] for r in counted) == 0:
        print("no performance counters: this GNU Radio build has them off, or GR_CONF_PERFCOUNTERS_ON was not seen")
        return 1
    tot = sum(r[1] for r in counted)
    print(f"blocks : {tot:.1f} s of work in {len(counted)} blocks;  the pace-setter is the first row")
    for name, t in sorted(rows, key=lambda r: -(r[1] if r[1] is not None else -1)):
        if t is None:
            print(f"  {name:36s}  (no counter)")
        else:
            print(f"  {name:36s}  {t:6.2f} s  {100 * t / tot:5.1f}%  {100 * t / air:5.1f}% of a core")
    ts_bytes = os.path.getsize(ts) if os.path.exists(ts) else 0
    print(f"output : {ts_bytes // 188} TS packets ({ts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
