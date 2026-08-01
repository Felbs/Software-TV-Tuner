"""av_bench.py — A/V-sync test apparatus + player experiment.

Requires a decode chain already writing live.ts. For each player mode it
samples mpv's A/V offset (and lag behind the live edge) every 5 s for
--secs, then reports the drift curve: start offset, end offset, and
slope in seconds-per-minute. A player is judged healthy when |offset|
stays <0.15 s AND slope ~ 0 for the whole window.

Modes:
  pipe  tail -> ffmpeg -> mpv stdin  (legacy; cannot seek, cannot heal)
  file  tv_watch.py                  (file-native + resync watchdog)
"""
import sys
import argparse, json, os, statistics, subprocess, time
from pathlib import Path

PY = sys.executable
HERE = Path(__file__).resolve().parent
IPC = r"\\.\pipe\mpv-tvtuna-super"
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
MUXBPS = 19_392_658 / 8

def ipc_get(prop):
    try:
        with open(IPC, "r+b", buffering=0) as p:
            p.write(json.dumps({"command": ["get_property", prop],
                                "request_id": 3}).encode() + b"\n")
            t0 = time.time(); buf = b""
            while time.time() - t0 < 2:
                buf += p.read(4096)
                for line in buf.split(b"\n"):
                    if not line.strip(): continue
                    try: r = json.loads(line)
                    except ValueError: continue
                    if r.get("request_id") == 3:
                        return r.get("data")
    except OSError:
        return None
    return None

def kill_players():
    # kill-ok: player/pipeline consumer (mpv/ffmpeg/tail), not the SDR holder
    subprocess.run(["taskkill", "/F", "/IM", "mpv.exe"], capture_output=True)
    # kill-ok: player/pipeline consumer (mpv/ffmpeg/tail), not the SDR holder
    subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe"], capture_output=True)
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'play_marginal|tv_watch' } | "
                    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign); loop also sweeps mpv/ffmpeg players
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue }"], capture_output=True)

def launch(mode, prog):
    env = os.environ.copy()
    env["PATH"] = r"C:\ffmpeg\bin;" + env.get("PATH", "")
    env["STVT_PLAY_IPC"] = IPC
    if mode == "pipe":
        return subprocess.Popen([PY, "-u", str(HERE / "play_marginal.py"),
                                 str(prog), "--tail-mb", "15", "--strong",
                                 "--cc"], env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.STDOUT)
    return subprocess.Popen([PY, "-u", str(HERE / "tv_watch.py"), str(prog)],
                            env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.STDOUT)

def bench(mode, prog, secs):
    kill_players(); time.sleep(2)
    p = launch(mode, prog)
    time.sleep(20)
    t0 = time.time()
    pts = []           # (t, avsync, behind)
    while time.time() - t0 < secs:
        av = ipc_get("avsync")
        pos = ipc_get("time-pos")
        behind = (LIVE.stat().st_size / MUXBPS - pos
                  if isinstance(pos, (int, float)) else None)
        if isinstance(av, (int, float)):
            pts.append((time.time() - t0, av, behind))
        time.sleep(5)
    p.terminate()
    if len(pts) < 6:
        print(f"  {mode}: INSUFFICIENT DATA ({len(pts)} samples)")
        return
    ts = [a for a, _, _ in pts]; avs = [b for _, b, _ in pts]
    n = len(pts)
    mt, ma = sum(ts)/n, sum(avs)/n
    slope = (sum((t-mt)*(a-ma) for t, a in zip(ts, avs)) /
             max(1e-9, sum((t-mt)**2 for t in ts))) * 60
    behinds = [c for _, _, c in pts if isinstance(c, (int, float))]
    print(f"  {mode}: n={n}  av start={avs[0]:+.3f}s  end={avs[-1]:+.3f}s  "
          f"median={statistics.median(avs):+.3f}s  worst={max(avs, key=abs):+.3f}s  "
          f"drift={slope:+.3f} s/min"
          + (f"  behind-live med={statistics.median(behinds):.1f}s"
             if behinds else ""))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--program", type=int, default=3)
    ap.add_argument("--secs", type=int, default=240)
    ap.add_argument("--modes", default="pipe,file")
    args = ap.parse_args()
    print(f"A/V sync bench ({args.secs}s per mode, program {args.program})")
    for mode in args.modes.split(","):
        bench(mode.strip(), args.program, args.secs)
    kill_players()

if __name__ == "__main__":
    main()
