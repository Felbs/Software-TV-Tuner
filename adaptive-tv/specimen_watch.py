"""specimen_watch.py — pulls the Glitch Specimen Recorder's trigger.

Follows a live chain log and drops a TRIGGER file (which iq_ring_sink
inside the chain answers by dumping the surrounding IQ) when the FEC
narration screams:

  STORM   last-5s RS bad-packet count spikes (quiet -> storm edge)
  SYNCLOSS  field-sync telemetry goes silent while the chain runs

Usage:
  python specimen_watch.py --log <chain.log> [--dir <specimen dir>]
                           [--storm 300] [--quiet 80] [--cooldown 45]
"""
import argparse
import re
import time
from pathlib import Path

RE_RS5 = re.compile(r"\(last5s: pkts=(\d+) era_dec=\d+ era_ok=\d+ bad=(\d+)\)")
RE_FS = re.compile(r"fs_err_rms=")

ap = argparse.ArgumentParser()
ap.add_argument("--log", required=True)
ap.add_argument("--dir", default=r"Z:\src\magic-tv-decoder\tools\data\specimens")
ap.add_argument("--storm", type=int, default=300,
                help="last-5s bad packets that count as a storm")
ap.add_argument("--quiet", type=int, default=80,
                help="last-5s bad packets that count as quiet")
ap.add_argument("--cooldown", type=float, default=45.0)
ap.add_argument("--window", type=int, default=0, help="exit after N s (0=forever)")
args = ap.parse_args()

log = Path(args.log)
trig = Path(args.dir) / "TRIGGER"
trig.parent.mkdir(parents=True, exist_ok=True)

off = 0
quiet_count = 0     # arm only after 2 consecutive quiet windows (10 s) —
                    # a fresh chain's startup convergence looks like a
                    # storm (learned from specimen #1, uptime 14 s)
fs_seen = False     # SYNCLOSS needs sync to have existed in this log
last_fs = time.time()
last_trig = 0.0
t0 = time.time()
print(f"watching {log} -> {trig}", flush=True)

def fire(reason):
    global last_trig
    if time.time() - last_trig < args.cooldown:
        return
    # disk guard: museum caps at ~20 GB (~75 specimens)
    if sum(f.stat().st_size for f in trig.parent.glob("*.cs16")) > 20e9:
        print("museum full (20 GB) — trigger suppressed", flush=True)
        return
    last_trig = time.time()
    trig.write_text(reason)
    print(f"[{time.strftime('%H:%M:%S')}] TRIGGER: {reason}", flush=True)

while args.window == 0 or time.time() - t0 < args.window:
    time.sleep(2.0)
    try:
        size = log.stat().st_size
        if size < off:
            off = 0                       # log rotated = NEW chain/channel:
            quiet_count = 0               # re-arm from scratch so channel
            last_fs = time.time()         # hops can't fake a quiet->storm
            fs_seen = False               # and sync must be re-proven
        with open(log, "r", errors="ignore") as f:
            f.seek(off)
            chunk = f.read()
            off = f.tell()
    except OSError:
        continue
    if RE_FS.search(chunk):
        last_fs = time.time()
        fs_seen = True
    elif (chunk and fs_seen and time.time() - last_fs > 8
          and last_fs > t0 + 20):
        # SYNC LOSS requires sync to have EXISTED in THIS chain's log:
        # chain alive + had field-syncs + they stopped. (Pilot-only
        # channels that never synced fired 37 false specimens on 7/06;
        # frozen inter-cycle logs fired 17 before that. Third rule's
        # the charm.)
        fire(f"SYNCLOSS had sync, lost it {time.time()-last_fs:.0f}s ago")
        last_fs = time.time()             # re-arm
    for pk, bad in RE_RS5.findall(chunk):
        bad = int(bad)
        if bad >= args.storm and quiet_count >= 2:
            fire(f"STORM last5s_bad={bad} (from sustained quiet)")
        quiet_count = quiet_count + 1 if bad <= args.quiet else 0
