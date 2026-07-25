"""endurance_watch.py — N-minute settled-health gate for the persistent
retune build. Counts OsO markers appended to the chain log during the
window (offset-based, so history doesn't pollute), samples /api/status
sparsely (1 per 30 s — ts_metrics reads ~48 MB of live.ts per call, and
hammering it steals matched-filter cycles), reports MER / loss at end.
"""
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

CHAIN_LOG = Path(r"Z:\src\adaptive-tv\lab\panel_chain.log")
PANEL = "http://127.0.0.1:8642"
MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0


def api(path):
    with urllib.request.urlopen(PANEL + path, timeout=10) as r:
        return json.loads(r.read())


ofs = CHAIN_LOG.stat().st_size
t0 = time.time()
samples = []
print(f"endurance window {MINUTES} min, chain log offset {ofs}", flush=True)
while time.time() - t0 < MINUTES * 60:
    time.sleep(30)
    try:
        s = api("/api/status")
        samples.append({"t": round(time.time() - t0),
                        "mer": s.get("mer_db"), "loss": s.get("loss_pct"),
                        "hdrs": s.get("hdrs_s"), "gaps": s.get("gaps_min")})
        print(f"  t+{samples[-1]['t']:4d}s mer={s.get('mer_db')} "
              f"loss={s.get('loss_pct')}% hdrs={s.get('hdrs_s')} "
              f"gaps={s.get('gaps_min')}", flush=True)
    except Exception as e:
        print(f"  status read failed: {e}", flush=True)
with open(CHAIN_LOG, "r", errors="ignore") as f:
    f.seek(ofs)
    window = f.read()
oso = window.count("OsO")
losses = [x["loss"] for x in samples if x["loss"] is not None]
mers = [x["mer"] for x in samples if x["mer"] is not None]
result = {"minutes": MINUTES, "oso": oso,
          "loss_max": max(losses) if losses else None,
          "loss_last": losses[-1] if losses else None,
          "mer_min": min(mers) if mers else None,
          "samples": samples}
print("RESULT " + json.dumps(result), flush=True)
