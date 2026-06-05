"""STVT remote — interactive shell that drives the chain from the EPG.

What you can type at the prompt:
  4.1            tune virtual channel 4.1
  r 4.1          record the show currently airing on 4.1 (auto-stops
                 at the show's EIT end time, or 60 min if no EIT)
  r 4.1 90       record 4.1 for 90 minutes
  r mux          multi-record EVERY program of the currently-tuned
                 RF multiplex simultaneously. One chain, N parallel
                 ffmpeg -c copy outputs into dated files.
  g              redisplay the EPG grid
  g 34           redisplay only the RF 34 mux
  s              list recordings currently in progress
  k <pid>        kill a recording (use pid from 's')
  q              quit (chain + any recordings keep running)

Recordings go to:  tools/data/recordings/YYYYMMDD/<virt>_<callsign>_<title>.ts

The remote works with whatever chain is already running. If no chain
is running, it'll spawn one when you tune.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
TV_TUNER_PY = TOOLS / "tv_tuner.py"
LIVE_TS = REPO / "tools" / "data" / "tv_live" / "live.ts"
REC_DIR = REPO / "tools" / "data" / "recordings"
REMOTE_LOG_DIR = REPO / "tools" / "data" / "remote_logs"
EPG_PY = TOOLS / "stvt_epg.py"
SCAN_PATH = Path(os.path.expanduser("~")) / ".tv_tuner" / "scan.json"

PY = sys.executable
FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
FFPROBE = r"C:\ffmpeg\bin\ffprobe.exe" if sys.platform == "win32" else "ffprobe"

CHAIN_DEFAULTS = {
    "STVT_EQ": "long", "STVT_RS": "stock", "STVT_VITERBI": "soft",
    "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1",
    "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
    "STVT_IFGR": "45", "STVT_RFGAIN_SEL": "3",
    "STVT_ANTENNA": "Antenna A", "STVT_SUB_MARGIN": "10",
}

# CREATE_NO_WINDOW for subprocess.run calls (no flashing consoles).
NO_WIN = {}
if sys.platform == "win32":
    NO_WIN["creationflags"] = 0x08000000


# ----------------- helpers -----------------

def run(cmd, timeout=30, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, **NO_WIN, **kw)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def safe_filename(s: str, maxlen: int = 60) -> str:
    bad = '<>:"/\\|?*\0'
    out = "".join("_" if c in bad else c for c in s).strip(" .")
    return out[:maxlen] or "untitled"


GPS_OFFSET = 315_964_800
LEAP = 18
def gps_to_unix(g: int) -> int:
    return int(g + GPS_OFFSET - LEAP)


# ----------------- scan / EPG load -----------------

def load_channels_with_events() -> list[dict]:
    """Same shape as stvt_epg's loader, but reused here so we don't
    shell out for every command."""
    if not SCAN_PATH.exists():
        return []
    try:
        data = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for c in data.get("channels", []):
        if c.get("not_detected"):
            continue
        callsign = c.get("callsign")
        if not callsign or callsign in ("?", "None"):
            continue
        psip = c.get("psip") or {}
        psip_chs = psip.get("channels") or []
        psip_events = psip.get("events") or {}
        psip_by_pnum = {p.get("program_number"): p for p in psip_chs
                        if p.get("program_number")}
        for prog in c.get("programs") or []:
            pnum = prog.get("program_num") or prog.get("program_id")
            if not pnum:
                continue
            match = psip_by_pnum.get(pnum) or {}
            major = match.get("major")
            minor = match.get("minor", 1)
            virtual = f"{major}.{minor}" if major else f"{c['rf']}.{pnum}"
            short = match.get("short_name") or callsign
            evs = []
            for e in psip_events.get(str(pnum), []):
                evs.append({
                    "title":      e.get("title") or "(untitled)",
                    "start_unix": gps_to_unix(e.get("start_gps", 0)),
                    "length":     e.get("length_sec") or 0,
                    "rating":     e.get("rating") or "",
                })
            evs.sort(key=lambda x: x["start_unix"])
            out.append({
                "rf":       c["rf"],
                "program":  pnum,
                "virtual":  str(virtual),
                "callsign": short,
                "network":  c.get("network_hint") or "",
                "events":   evs,
            })
    def sk(r):
        try:
            mj, _, mn = r["virtual"].partition(".")
            return (int(mj), int(mn or "1"))
        except ValueError:
            return (9999, 0)
    out.sort(key=sk)
    return out


def event_at(ch: dict, when_unix: int) -> dict | None:
    for e in ch["events"]:
        if e["start_unix"] <= when_unix < e["start_unix"] + e["length"]:
            return e
    return None


# ----------------- chain control -----------------

def running_tv_tuner_rf_prog() -> tuple[int | None, int | None]:
    if sys.platform != "win32":
        out = subprocess.run(["pgrep", "-af", "tv_tuner.py"],
                             capture_output=True, text=True).stdout
    else:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, **NO_WIN,
        ).stdout
    for line in out.splitlines():
        if "tv_tuner.py" not in line:
            continue
        toks = line.split()
        rf = prog = None
        for i, t in enumerate(toks):
            if t == "--rf" and i + 1 < len(toks):
                try:
                    rf = int(toks[i + 1])
                except ValueError:
                    pass
            elif t in ("--program-id", "--program") and i + 1 < len(toks):
                try:
                    prog = int(toks[i + 1])
                except ValueError:
                    pass
        if rf is not None:
            return rf, prog
    return None, None


def running_chain_rf() -> int | None:
    """RF of whatever tv_live.py is currently running, if any."""
    if sys.platform != "win32":
        out = subprocess.run(["pgrep", "-af", "tv_live.py"],
                             capture_output=True, text=True).stdout
    else:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, **NO_WIN,
        ).stdout
    for line in out.splitlines():
        if "tv_live.py" not in line:
            continue
        m = re.search(r"--rf\s+(\d+)", line)
        if m:
            return int(m.group(1))
    return None


def kill_chain_and_player():
    if sys.platform == "win32":
        for img in ("ffmpeg.exe", "vlc.exe", "ffplay.exe"):
            subprocess.run(["taskkill", "/F", "/IM", img],
                           capture_output=True, **NO_WIN)
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, **NO_WIN,
        ).stdout
        for line in out.splitlines():
            if "tv_tuner.py" in line or "tv_live.py" in line:
                try:
                    pid = int(line.strip().rsplit(",", 1)[-1])
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, **NO_WIN)
                except (ValueError, IndexError):
                    pass
    else:
        subprocess.run(["pkill", "-f", "tv_tuner.py"], capture_output=True)
        subprocess.run(["pkill", "-f", "tv_live.py"],  capture_output=True)
        subprocess.run(["pkill", "-f", "ffmpeg"],      capture_output=True)
        subprocess.run(["pkill", "-x", "vlc"],         capture_output=True)


def chain_env() -> dict:
    env = os.environ.copy()
    for k, v in CHAIN_DEFAULTS.items():
        env.setdefault(k, v)
    sdr_api = r"C:\Program Files\SDRplay\API\x64"
    if os.path.isdir(sdr_api):
        env["PATH"] = sdr_api + os.pathsep + env.get("PATH", "")
    return env


def spawn_tv_tuner(rf: int, program: int) -> Path:
    """Spawn tv_tuner.py for RF/program. Log all output to a per-spawn
    file under data/remote_logs/ so we can see why a tune failed."""
    REMOTE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = REMOTE_LOG_DIR / f"tune_{stamp}_rf{rf}_p{program}.log"
    log_fh = open(log_path, "w", encoding="utf-8")
    cmd = [PY, "-u", str(TV_TUNER_PY),
           "--rf", str(rf), "--program-id", str(program), "--player", "vlc"]
    if sys.platform == "win32":
        DET = 0x00000008 | 0x00000200 | 0x08000000
        subprocess.Popen(cmd, env=chain_env(), creationflags=DET,
                         stdout=log_fh, stderr=subprocess.STDOUT)
    else:
        subprocess.Popen(cmd, env=chain_env(), start_new_session=True,
                         stdout=log_fh, stderr=subprocess.STDOUT)
    return log_path


def ensure_chain_on_rf(rf: int) -> bool:
    """Make sure tv_live.py is producing on the requested RF. Returns True
    when live.ts is observed past the lock threshold."""
    cur = running_chain_rf()
    if cur != rf:
        kill_chain_and_player()
        time.sleep(2)
        # Use tv_tuner for proper player + chain spawn; user can ignore VLC
        # if they're only recording.
        spawn_tv_tuner(rf, 1)
    # Wait for live.ts to appear & lock.
    deadline = time.time() + 60
    while time.time() < deadline:
        if LIVE_TS.exists() and LIVE_TS.stat().st_size > 40_000_000:
            return True
        time.sleep(2)
    return False


# ----------------- recording -----------------

def all_programs_in_mux() -> list[int]:
    """Look at current live.ts and return list of program numbers."""
    rc = run([FFPROBE, "-v", "error", "-of", "json",
              "-show_programs", str(LIVE_TS)], timeout=15)
    if rc.returncode != 0:
        return []
    try:
        data = json.loads(rc.stdout)
    except json.JSONDecodeError:
        return []
    progs = []
    for p in data.get("programs", []):
        n = p.get("program_num")
        if n is not None:
            progs.append(n)
    return progs


def make_rec_path(virtual: str, callsign: str, title: str | None) -> Path:
    day = time.strftime("%Y%m%d")
    stamp = time.strftime("%H%M%S")
    out_dir = REC_DIR / day
    out_dir.mkdir(parents=True, exist_ok=True)
    title_part = "_" + safe_filename(title) if title else ""
    fname = f"{stamp}_v{virtual}_{safe_filename(callsign, 12)}{title_part}.ts"
    return out_dir / fname


# In-process registry of active recordings.
RECORDINGS: list[dict] = []


def start_recording(virtual: str, rf: int, program: int, duration_s: int,
                    title: str | None, callsign: str) -> bool:
    if not ensure_chain_on_rf(rf):
        print(f"  [rec] couldn't lock RF {rf}")
        return False
    out_path = make_rec_path(virtual, callsign, title)
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "warning",
        "-fflags", "+genpts+igndts+discardcorrupt",
        "-err_detect", "ignore_err",
        "-thread_queue_size", "4096",
        "-f", "mpegts", "-i", str(LIVE_TS),
        "-map", f"0:p:{program}",
        "-c", "copy", "-copy_unknown",
        "-t", str(duration_s),
        "-f", "mpegts", str(out_path),
    ]
    if sys.platform == "win32":
        DET = 0x00000008 | 0x00000200 | 0x08000000
        p = subprocess.Popen(cmd, creationflags=DET,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    else:
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    RECORDINGS.append({
        "pid":       p.pid,
        "proc":      p,
        "virtual":   virtual,
        "callsign":  callsign,
        "title":     title or "(no title)",
        "out_path":  out_path,
        "start":     time.time(),
        "duration":  duration_s,
    })
    print(f"  [rec] PID {p.pid}  v{virtual} {callsign}  "
          f"{duration_s // 60}m  -> {out_path.name}")
    return True


def start_recording_mux(channels: list[dict], rf: int,
                        duration_s: int) -> int:
    """Multi-record EVERY program of the current mux. Returns how many
    recordings were started."""
    if not ensure_chain_on_rf(rf):
        print(f"  [mux] couldn't lock RF {rf}")
        return 0
    progs = all_programs_in_mux()
    if not progs:
        print("  [mux] no programs found in live.ts")
        return 0
    # Build a virtual/callsign lookup for the file names.
    info = {c["program"]: c for c in channels if c["rf"] == rf}
    started = 0
    for pnum in progs:
        ci = info.get(pnum, {"virtual": f"{rf}.{pnum}",
                              "callsign": "?"})
        title = None
        ev = event_at(ci, int(time.time())) if ci.get("events") else None
        if ev:
            title = ev["title"]
        ok = start_recording(ci["virtual"], rf, pnum, duration_s,
                             title, ci.get("callsign", "?"))
        if ok:
            started += 1
    return started


def list_recordings():
    if not RECORDINGS:
        print("  (no recordings in progress)")
        return
    for r in list(RECORDINGS):
        alive = r["proc"].poll() is None
        if not alive:
            RECORDINGS.remove(r)
            continue
        elapsed = int(time.time() - r["start"])
        remain  = max(0, r["duration"] - elapsed)
        sz = r["out_path"].stat().st_size / 1e6 if r["out_path"].exists() else 0
        print(f"  PID {r['pid']:>6}  v{r['virtual']:<5} {r['callsign']:<8}  "
              f"{elapsed//60:>3}m elapsed / {remain//60:>3}m left  "
              f"{sz:>6.1f} MB  {r['title'][:40]}")


def kill_recording(pid: int):
    for r in list(RECORDINGS):
        if r["pid"] == pid:
            try:
                r["proc"].terminate()
                r["proc"].wait(timeout=3)
            except subprocess.TimeoutExpired:
                r["proc"].kill()
            RECORDINGS.remove(r)
            print(f"  [rec] stopped {pid}")
            return
    print(f"  no recording with pid {pid}")


# ----------------- EPG print (shells out to stvt_epg.py) -----------------

def print_epg(rf: int | None = None):
    cmd = [PY, str(EPG_PY), "--no-color"]
    if rf:
        cmd += ["--rf", str(rf)]
    subprocess.run(cmd)


# ----------------- input parsing -----------------

VIRT_RE = re.compile(r"^\d+(?:\.\d+)?$")


def find_channel(channels: list[dict], virtual: str) -> dict | None:
    for c in channels:
        if c["virtual"] == virtual:
            return c
    # Allow major-only ("4" matches "4.1")
    if "." not in virtual:
        for c in channels:
            mj, _, _ = c["virtual"].partition(".")
            if mj == virtual:
                return c
    return None


# ----------------- main loop -----------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-print-epg", action="store_true",
                    help="Don't print the EPG on startup")
    args = ap.parse_args()

    REC_DIR.mkdir(parents=True, exist_ok=True)
    channels = load_channels_with_events()
    if not channels:
        print("(no scan data in ~/.tv_tuner/scan.json — run "
              "tv_tuner.py --scan first)")
        return 1

    print("STVT remote. Type 'h' for help, 'q' to quit.")
    if not args.no_print_epg:
        print_epg()

    while True:
        try:
            ans = input("\nremote> ").strip()
        except EOFError:
            break
        if not ans:
            continue
        low = ans.lower()
        if low in ("q", "quit", "exit"):
            break
        if low in ("h", "help", "?"):
            print(__doc__)
            continue
        if low == "g":
            print_epg(); continue
        if low.startswith("g "):
            try:
                rf = int(low.split()[1])
                print_epg(rf=rf)
            except (IndexError, ValueError):
                print("  usage: g [rf]")
            continue
        if low == "s":
            list_recordings(); continue
        if low.startswith("k "):
            try:
                pid = int(low.split()[1])
                kill_recording(pid)
            except (IndexError, ValueError):
                print("  usage: k <pid>")
            continue
        # Record commands
        if low.startswith("r "):
            parts = low.split()
            tgt = parts[1] if len(parts) >= 2 else ""
            if tgt == "mux":
                # Multi-record every program of the currently-tuned mux
                rf = running_chain_rf()
                if rf is None:
                    print("  no chain running — tune a channel first")
                    continue
                dur = int(parts[2]) * 60 if len(parts) >= 3 else 3600
                n = start_recording_mux(channels, rf, dur)
                print(f"  [mux] started {n} parallel recordings on RF {rf}")
                continue
            ch = find_channel(channels, tgt)
            if ch is None:
                print(f"  unknown channel {tgt!r}")
                continue
            # Duration: explicit minutes arg, else current event's remaining
            # time + 5 min cushion, else 60 min.
            now = int(time.time())
            ev = event_at(ch, now)
            if len(parts) >= 3:
                try:
                    duration_s = int(parts[2]) * 60
                except ValueError:
                    duration_s = 3600
            elif ev:
                remain = (ev["start_unix"] + ev["length"]) - now
                duration_s = max(60, remain + 300)
            else:
                duration_s = 3600
            title = ev["title"] if ev else None
            start_recording(ch["virtual"], ch["rf"], ch["program"],
                            duration_s, title, ch["callsign"])
            continue
        if low in ("d", "diag", "diagnose"):
            # Show what's running + recent log
            cur_rf = running_chain_rf()
            tt_rf, tt_prog = running_tv_tuner_rf_prog()
            sz = LIVE_TS.stat().st_size / 1e6 if LIVE_TS.exists() else 0
            print(f"  chain RF        : {cur_rf}")
            print(f"  tv_tuner RF/prog: {tt_rf} / {tt_prog}")
            print(f"  live.ts         : {sz:.1f} MB")
            logs = sorted(REMOTE_LOG_DIR.glob("tune_*.log"))[-1:] \
                if REMOTE_LOG_DIR.exists() else []
            if logs:
                print(f"  last spawn log  : {logs[0]}")
                tail = logs[0].read_text(encoding="utf-8",
                                          errors="replace").splitlines()[-8:]
                for line in tail:
                    print(f"    | {line}")
            continue
        # Tune by virtual channel
        if VIRT_RE.match(ans):
            ch = find_channel(channels, ans)
            if ch is None:
                print(f"  channel {ans!r} not in scan")
                continue
            print(f"  tuning v{ch['virtual']} {ch['callsign']} "
                  f"(RF {ch['rf']} program {ch['program']})...")
            kill_chain_and_player()
            time.sleep(2)
            log_path = spawn_tv_tuner(ch["rf"], ch["program"])
            print(f"  spawn log: {log_path}  (type 'd' if it doesn't come up)")
            continue
        print(f"  unknown command {ans!r} — try 'h' for help")

    print("[remote] bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
