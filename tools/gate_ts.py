#!/usr/bin/env python3
"""GATE: tv_live.py's chain on a capture, decoded bytes against an oracle.

  python tools/gate_ts.py CAPTURE.cs16 --oracle CAPTURE.oracle.ts              # compare
  python tools/gate_ts.py CAPTURE.cs16 --oracle CAPTURE.oracle.ts --make-oracle  # record

The oracle is the TS the chain wrote from the same capture BEFORE a change; afterwards the
gate says whether the change altered a single decoded byte. Every STVT_* lever is honoured,
so a lever is gated by running once without it (--make-oracle) and once with it. A change
that only moves arithmetic (a cheaper filter, a vectorised loop) must pass this gate with
nothing to explain; a change meant to decode MORE must show where the extra packets came
from - this prints that too.

Exit 0 = identical (or every oracle packet reproduced in order with extra trailing ones);
exit 1 = differs, with how: fewer packets, a shifted start, or corrupted payloads are
different bugs. Captures and oracles never go in the repository.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import chain_replay  # noqa: E402


def tei(pkt):
    return (pkt[1] & 0x80) != 0


def describe(packets):
    """Count, TEI, nulls, PIDs and continuity-counter errors. The CC errors are the decode-quality
    number: a real mux carries plenty of NULL packets of its own, so the null count alone says
    little (it misled a first reading of a lever sweep), while every RS-uncorrectable packet
    that tei_scrub turned into a NULL leaves a hole in its PID's counter."""
    n = len(packets)
    bad = sum(1 for p in packets if tei(p))
    nulls = 0
    last = {}
    cc_err = 0
    for p in packets:
        pid = (p[1] & 0x1F) << 8 | p[2]
        if pid == 0x1FFF:
            nulls += 1
            continue
        if not (p[3] & 0x10):                      # no payload: counter does not advance
            continue
        cc = p[3] & 0x0F
        if pid in last and (last[pid] + 1) & 0x0F != cc:
            cc_err += 1
        last[pid] = cc
    return f"{n} packets, {bad} with TEI, {nulls} null, {len(last)} PIDs, {cc_err} CC errors"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--oracle", required=True)
    ap.add_argument("--make-oracle", action="store_true", help="write the oracle from this run instead of comparing")
    ap.add_argument("--rf", type=int, default=36)
    ap.add_argument("--rate", type=float, default=None)
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "stvt_gate.ts"))
    a = ap.parse_args()
    out = a.oracle if a.make_oracle else a.out
    tb = chain_replay.build(a.capture, out, rf=a.rf, native_rate=a.rate)
    wall, cpu = chain_replay.run(tb)
    levers = {k: v for k, v in os.environ.items() if k.startswith("STVT_")}
    ours = chain_replay.ts_packets(out)
    sha = hashlib.sha256(Path(out).read_bytes()).hexdigest()
    print(f"levers : {levers if levers else 'defaults'}")
    print(f"air    : {tb.air_seconds:.1f} s;  wall {wall:.1f} s -> {tb.air_seconds / max(wall, 1e-9):.2f}x;  "
          f"CPU {cpu:.1f} s")
    print(f"ours   : {describe(ours)}  sha256 {sha[:16]}...")
    if a.make_oracle:
        print(f"ORACLE WRITTEN: {a.oracle}")
        return 0
    theirs = chain_replay.ts_packets(a.oracle)
    print(f"oracle : {describe(theirs)}  sha256 "
          f"{hashlib.sha256(Path(a.oracle).read_bytes()).hexdigest()[:16]}...")
    if ours == theirs:
        print("GATE PASS - byte-identical to the oracle")
        return 0
    if len(ours) > len(theirs) and ours[:len(theirs)] == theirs:
        print(f"GATE PASS - every oracle packet reproduced in order, +{len(ours) - len(theirs)} trailing")
        return 0
    # how do they differ?
    so, st = set(ours), set(theirs)
    print(f"common : {len(so & st)} distinct packets;  only ours {len(so - st)};  only oracle {len(st - so)}")
    k = next((i for i, p in enumerate(theirs) if ours and p == ours[0]), None)
    if k is None:
        print("align  : our first packet does not appear in the oracle")
    else:
        same = 0
        for x, y in zip(ours, theirs[k:]):
            if x != y:
                break
            same += 1
        print(f"align  : our first packet is the oracle's #{k}; {same} in a row match from there, "
              f"then packet #{same} differs")
    print("GATE FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
