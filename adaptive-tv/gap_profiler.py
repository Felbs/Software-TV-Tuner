"""gap_profiler.py — WHICH stage kills each packet? (born from the 4-arm
trial's verdict: FS rescue only capped ~19% of gaps → the disease is
multi-site; this instrument hands every gap burst to its guilty stage.)

Runs one chain (flywheel on — proven safe) and follows two truths at once:
  * live.ts, packet by packet: CC-discontinuity bursts, wall-clocked
  * the chain's own telemetry: FPLL rails (max|x| >= 1.2), EQ error
    spikes (fs_err_rms >= 3), and fs_checker events (COAST / REJECT /
    FIELD-LOSS / GAP-ANOMALY), mapped from chain-time to wall-time
Each gap burst is attributed to the nearest guilty event in the prior
0.8 s: FS-site > FPLL-site > EQ-site > UNATTRIBUTED. The summary is the
disease map that decides where the next strike aims.

Usage: python gap_profiler.py [minutes]   (default 100)
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = r"C:\Users\user\radioconda\python.exe"
TOOLS = Path(r"Z:\src\magic-tv-decoder\tools")
LIVE = TOOLS / "data" / "tv_live" / "live.ts"
LOGDIR = HERE / "lab"
OUT = LOGDIR / ("gap_profile_%s.jsonl" % time.strftime("%Y%m%d_%H%M"))
CHAIN_LOG = LOGDIR / "gap_profiler_chain.log"
RF = 34
MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 100.0

RE_FPLL = re.compile(r"\[fpll t=\s*([\d.]+)s\].*?max\|x\|=([\d.]+)")
RE_EQ = re.compile(r"\[eq-long t=\s*([\d.]+)s\].*?fs_err_rms=([\d.]+)")
RE_FST = re.compile(r"\[fs_telem t=\s*([\d.]+)s\] (\S+)")

events = []            # (wall, kind)  kind: FS / RAIL / EQ
events_lock = threading.Lock()
stop = threading.Event()
t_offset = [None]      # wall = t_offset + chain_t


def log(rec):
    rec["iso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)


def chainlog_follower():
    pos = 0
    while not stop.is_set():
        time.sleep(1.0)
        try:
            with open(CHAIN_LOG, "r", errors="ignore") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except OSError:
            continue
        now = time.time()
        for line in chunk.splitlines():
            m = RE_FPLL.search(line)
            if m:
                t, mx = float(m.group(1)), float(m.group(2))
                if t_offset[0] is None:
                    t_offset[0] = now - t
                if mx >= 1.2:
                    with events_lock:
                        events.append((t_offset[0] + t, "RAIL"))
                continue
            m = RE_EQ.search(line)
            if m and t_offset[0] is not None:
                t, err = float(m.group(1)), float(m.group(2))
                if err >= 3.0:
                    with events_lock:
                        events.append((t_offset[0] + t, "EQ"))
                continue
            m = RE_FST.search(line)
            if m and t_offset[0] is not None:
                t, kind = float(m.group(1)), m.group(2)
                if kind.startswith(("COAST", "FIELD-LOSS", "REJECT",
                                    "GAP-ANOMALY", "RE-ACQUIRE")):
                    with events_lock:
                        events.append((t_offset[0] + t, "FS"))


def attribute(gap_wall):
    best = None
    with events_lock:
        cutoff = gap_wall - 0.8
        recent = [(w, k) for (w, k) in events[-400:]
                  if cutoff <= w <= gap_wall + 0.1]
    for pri, kind in ((0, "FS"), (1, "RAIL"), (2, "EQ")):
        if any(k == kind for _, k in recent):
            return kind
    return "NONE"


def main():
    # bench the panel: this instrument owns the radio
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match "
                    "'tv_tuna_panel|tv_live|tv_watch' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue }"], capture_output=True)
    time.sleep(3)
    env = os.environ.copy()
    env["PATH"] = (r"C:\Program Files\SDRplay\API\x64;C:\ffmpeg\bin;"
                   + env.get("PATH", ""))
    env.update({"STVT_ANTENNA": "Antenna A", "STVT_IFGR": "32",
                "STVT_RFGAIN_SEL": "2",
                "STVT_SDR_AGC": "1", "STVT_AGC_SETPOINT": "-20",
                "STVT_EQ": "long", "STVT_VITERBI": "soft",
                "STVT_RFNOTCH": "1", "STVT_DABNOTCH": "1",
                "STVT_RS": "stock", "STVT_SPS": "1.1",
                "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1",
                "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
                "STVT_EQ_TELEM": "1", "ATSCPLUS_FS_TELEM": "1",
                "ATSCPLUS_FS_COAST": "3",
                "STVT_EQ_TAP_CACHE": str(LOGDIR / "tapcache")})
    # fresh stream file: a leftover live.ts from a prior session leaves
    # our read offset beyond the new file's end and blinds the follower
    if LIVE.exists():
        try:
            LIVE.unlink()
        except OSError:
            pass
    lf = open(CHAIN_LOG, "w")
    subprocess.Popen([PY, "-u", str(TOOLS / "tv_live.py"), "--rf", str(RF)],
                     env=env, stdout=lf, stderr=subprocess.STDOUT)
    log({"event": "profiler_start", "rf": RF, "minutes": MINUTES})
    threading.Thread(target=chainlog_follower, daemon=True).start()

    # wait for the stream, then follow it from the live edge
    while not LIVE.exists() or LIVE.stat().st_size < 2_000_000:
        time.sleep(2)
    f = open(LIVE, "rb")
    f.seek(max(0, LIVE.stat().st_size - 188 * 64))
    buf = b""
    cc_prev = {}
    counts = {"FS": 0, "RAIL": 0, "EQ": 0, "NONE": 0}
    last_burst = 0.0
    bursts = 0
    t_end = time.time() + MINUTES * 60
    while time.time() < t_end:
        chunk = f.read(188 * 4096)
        if not chunk:
            time.sleep(0.4)
            continue
        buf += chunk
        # align to sync byte
        start = buf.find(b"\x47")
        if start < 0:
            buf = b""
            continue
        buf = buf[start:]
        n = len(buf) // 188
        now = time.time()
        gap_hit = False
        for i in range(n):
            p = buf[i * 188:(i + 1) * 188]
            if p[0] != 0x47:
                continue
            pid = ((p[1] & 0x1F) << 8) | p[2]
            if pid == 0x1FFF:
                continue
            afc = (p[3] >> 4) & 0x3
            if not (afc & 1):
                continue
            cc = p[3] & 0x0F
            prev = cc_prev.get(pid)
            cc_prev[pid] = cc
            if prev is not None and cc != ((prev + 1) & 0xF) and cc != prev:
                gap_hit = True
        buf = buf[n * 188:]
        if gap_hit and now - last_burst > 0.5:
            last_burst = now
            bursts += 1
            k = attribute(now)
            counts[k] += 1
            log({"gap": bursts, "cause": k})
    stop.set()
    total = max(1, bursts)
    log({"event": "profile_verdict", "bursts": bursts,
         "minutes": MINUTES,
         "gaps_per_min": round(bursts / MINUTES, 1),
         **{f"pct_{k.lower()}": round(100 * v / total)
            for k, v in counts.items()}})
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'tv_live' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue }"], capture_output=True)
    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         "$env:PATH = 'C:\\Program Files\\SDRplay\\API\\x64;' + $env:PATH; "
         "Start-Process -FilePath C:\\Users\\user\\radioconda\\python.exe "
         "-ArgumentList 'Z:\\src\\adaptive-tv\\tv_tuna_panel.py' "
         "-WindowStyle Hidden"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("=== GAP PROFILE COMPLETE ===", flush=True)


if __name__ == "__main__":
    main()
