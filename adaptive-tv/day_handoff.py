"""day_handoff.py — at 09:00 bench the TV panel and hand the SDR to the
daylight cube (09:00 -> 17:00). Fills the daytime half of the 24 h
Antenna x Channel x Time map ("daylight is the honest window").
"""
import os
import subprocess
import time
from datetime import datetime

import psutil

PY = r"C:\Users\user\radioconda\python.exe"
HERE = os.path.dirname(os.path.abspath(__file__))

while datetime.now().strftime("%H:%M") < "09:00":
    time.sleep(60)

killed = []
for p in psutil.process_iter(["name", "cmdline"]):
    try:
        if p.info["name"] == "python.exe" and any(
                "tv_tuna_panel" in a or "tv_live" in a or
                "overnight_cube" in a for a in (p.info["cmdline"] or [])):
            if p.pid != os.getpid():
                p.kill()
                killed.append(p.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
print("benched:", killed, flush=True)
time.sleep(3)

env = os.environ.copy()
env["PATH"] = r"C:\Program Files\SDRplay\API\x64" + os.pathsep + env["PATH"]
print("day cube launching until 17:00", flush=True)
r = subprocess.run([PY, "-u", os.path.join(HERE, "overnight_cube.py"),
                    "17:00"], env=env, cwd=HERE)
print("day cube exited", r.returncode, flush=True)
