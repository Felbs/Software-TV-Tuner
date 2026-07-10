"""Overnight time-knob trainer: while nobody is watching, sweep every
channel once per interval so every hour-of-day bin accrues samples.

One scan = one sample for EVERY channel in the current hour bin, but a
scan takes the tuner (Law 7) — so this runs only when the panel is
idle, and stands down the moment someone tunes in. Watch-training
(automatic, 60 samples/hr on the watched channel) continues to work on
top; the two compose. See docs/science.md §15.5 for the math.

Usage:
    python time_trainer.py                # sweep hourly until stopped
    python time_trainer.py --until 7      # stop at 07:00 (overnight)
    python time_trainer.py --interval 30  # sweep every 30 min
"""
import argparse
import json
import time
import urllib.request

PANEL = "http://127.0.0.1:8642"


def api(path, post=False):
    req = urllib.request.Request(PANEL + path, data=b"{}" if post else None)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60,
                    help="minutes between sweeps (default 60)")
    ap.add_argument("--until", type=int, default=None,
                    help="stop at this local hour (e.g. 7 = 07:00)")
    args = ap.parse_args()
    print(f"[trainer] sweeping every {args.interval} min"
          + (f" until {args.until:02d}:00" if args.until is not None else ""),
          flush=True)
    sweeps = 0
    while True:
        if args.until is not None and time.localtime().tm_hour == args.until:
            print(f"[trainer] {args.until:02d}:00 reached — "
                  f"{sweeps} sweeps done, goodnight", flush=True)
            return
        try:
            s = api("/api/status")
            if s.get("tuned") or s.get("tuning"):
                # someone is watching — their watch-training has the
                # tuner; skip this cycle quietly
                print(f"[trainer] {time.strftime('%H:%M')} TV in use — "
                      "skipping this sweep", flush=True)
            elif s.get("scan", {}).get("running"):
                print(f"[trainer] {time.strftime('%H:%M')} scan already "
                      "running", flush=True)
            else:
                print(f"[trainer] {time.strftime('%H:%M')} sweep "
                      f"#{sweeps + 1} starting", flush=True)
                api("/api/scan", post=True)
                # wait for the scan to finish (cap: 25 min)
                for _ in range(150):
                    time.sleep(10)
                    if not api("/api/status").get("scan", {}).get("running"):
                        break
                sweeps += 1
                print(f"[trainer] sweep #{sweeps} done — every channel "
                      "gained one sample in this hour's bin", flush=True)
        except Exception as e:  # panel down, network hiccup: retry later
            print(f"[trainer] {time.strftime('%H:%M')} panel unreachable "
                  f"({e}) — will retry", flush=True)
        time.sleep(max(60, args.interval * 60 - 60))


if __name__ == "__main__":
    main()
