"""Standing drizzle logger: one file-read per minute, appends timestamped
loss-rate + MER of the live panel chain to lab/drizzle_watch.csv.
Purpose: timestamp interference waves (suspected duty-cycling appliance /
AC compressor) so ON/OFF windows can be correlated with household events.
Respects the don't-hammer law: reads the log tail only, never probes the TS.
"""
import re, time, sys, io
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
LOG = HERE / "lab" / "panel_chain.log"
OUT = HERE / "lab" / "drizzle_watch.csv"

RE_RS = re.compile(r"last5s: pkts=(\d+) era_dec=\d+ era_ok=\d+ bad=(\d+)")
RE_FS = re.compile(r"fs_err_rms=([0-9.eE+-]+)")

if not OUT.exists():
    OUT.write_text("time,bad_pct,pkts_per_min,mer_med,note\n")

pos = LOG.stat().st_size if LOG.exists() else 0
while True:
    time.sleep(60)
    try:
        size = LOG.stat().st_size
        if size < pos:          # chain restarted, log rotated
            pos = 0
        with open(LOG, "r", errors="ignore") as f:
            f.seek(pos)
            chunk = f.read()
            pos = f.tell()
        rs = RE_RS.findall(chunk)
        pkts = sum(int(p) for p, _ in rs)
        bad = sum(int(b) for _, b in rs)
        import math
        mers = [20 * math.log10(5.0 / float(e)) for e in RE_FS.findall(chunk)
                if float(e) > 0]
        mers.sort()
        mer = f"{mers[len(mers)//2]:.2f}" if mers else ""
        note = "" if rs else "chain-silent"
        bp = f"{100*bad/pkts:.4f}" if pkts else ""
        with open(OUT, "a") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M},{bp},{pkts},{mer},{note}\n")
    except Exception as e:
        with open(OUT, "a") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M},,,,err:{e}\n")
