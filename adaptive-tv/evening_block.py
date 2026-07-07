"""evening_block.py — the 17:00 self-executing evening science block.

    17:00  bench panel, beacon oracle's first real sounding (~30 s)
    17:01  BO harvest hunt on breathing RF21 (25 evals, ~35 min)
    ~17:40 evening cube until 22:30 (the map's final gap, 3 antennas)

Run any time; sleeps until showtime.
"""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil

HERE = Path(__file__).parent
PY = sys.executable

while datetime.now().strftime("%H:%M") < "17:00":
    time.sleep(30)

print("17:00 — evening block begins", flush=True)
for p in psutil.process_iter(["name", "cmdline"]):
    try:
        if p.info["name"] == "python.exe" and any(
                x in a for a in (p.info["cmdline"] or [])
                for x in ("tv_tuna_panel", "tv_live", "tv_watch")):
            p.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
for name in ("mpv.exe", "vlc.exe"):
    subprocess.run(["taskkill", "/IM", name, "/F"],
                   capture_output=True)
time.sleep(3)

print("— oracle sounding —", flush=True)
subprocess.run([PY, "-u", str(HERE / "beacon_oracle.py")], timeout=300)

print("— BO harvest hunt, RF21 breathing window —", flush=True)
subprocess.run([PY, "-u", str(HERE / "bo_harvest.py"),
                "--rf", "21", "--ant", "Antenna B", "--rfg", "2",
                "--evals", "25"])

print("— evening cube until 22:30 —", flush=True)
subprocess.run([PY, "-u", str(HERE / "overnight_cube.py"), "22:30"])
print("evening block complete — say bedtime for the night shift", flush=True)
