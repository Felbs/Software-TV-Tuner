"""tv_up.py — the self-verifying TV supervisor ("the bot").

Launches chain + player and then PROVES playback: queries mpv over IPC and
checks the playhead is actually advancing. No more "process is running" lies.
Retries with diagnostics on failure, then supervises forever:
  - playhead stalled  -> bounce the player (the freeze fix, automated)
  - chain dead        -> relaunch the chain
  - live.ts > 1 GB    -> planned rotation (restart chain+player on a
                         fresh file before the size kills the pipe)

Usage:
    python tv_up.py --rf 36 --program 3 [--ifgr 32 --rfgain 2] [--nb 2.0]
Log lines are grep-friendly: PLAYING VERIFIED / STALL / ROTATE / RETRY / FATAL
"""
import sys
import argparse, json, os, subprocess, sys, time
from pathlib import Path

PY = sys.executable
TV_LIVE = r"Z:\src\magic-tv-decoder\tools\tv_live.py"
PLAYER = str(Path(__file__).resolve().parent / "play_marginal.py")
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
IPC = r"\\.\pipe\mpv-tvtuna-super"
ROTATE_BYTES = 6_000_000_000    # ~40 min at full mux rate; a rotation costs
                                # a ~40 s blackout so don't do it every 7 min

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def mpv_get(prop):
    try:
        with open(IPC, "r+b", buffering=0) as pipe:
            pipe.write(json.dumps({"command": ["get_property", prop],
                                   "request_id": 7}).encode() + b"\n")
            t0 = time.time()
            buf = b""
            while time.time() - t0 < 3:
                buf += pipe.read(4096)
                for line in buf.split(b"\n"):
                    if not line.strip(): continue
                    try: r = json.loads(line)
                    except ValueError: continue
                    if r.get("request_id") == 7:
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
                    "Where-Object { $_.CommandLine -match 'play_marginal' } | "
                    # kill-ok: player/pipeline consumer (mpv/ffmpeg/tail), not the SDR holder
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue }"], capture_output=True)

def kill_chain():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'tv_live' -and $_.ProcessId "
                    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
                    f"-ne {os.getpid()}" + " } | ForEach-Object { Stop-Process -Id "
                    "$_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                   capture_output=True)

def build_env(args):
    env = os.environ.copy()
    env["PATH"] = (r"C:\Program Files\SDRplay\API\x64;C:\ffmpeg\bin;"
                   + env.get("PATH", ""))
    env.update({
        "STVT_ANTENNA": args.antenna, "STVT_IFGR": str(args.ifgr),
        "STVT_RFGAIN_SEL": str(args.rfgain), "STVT_EQ": "long",
        "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1", "STVT_DABNOTCH": "1",
        "STVT_RS": "stock", "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8",
        "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
        "STVT_PLAY_IPC": IPC,
    })
    if args.nb > 0:
        env["STVT_NB"] = "1"
        env["STVT_NB_THRESHOLD"] = str(args.nb)
    return env

def start_chain(args, env):
    if LIVE.exists():
        try: LIVE.unlink()
        except OSError:
            log("WARN could not delete stale live.ts")
    return subprocess.Popen([PY, "-u", TV_LIVE, "--rf", str(args.rf)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def wait_decode(timeout=70):
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(3)
        if LIVE.exists() and LIVE.stat().st_size > 12_000_000:
            with open(LIVE, "rb") as f:
                f.seek(-min(8_000_000, LIVE.stat().st_size), 2)
                if f.read().count(b"\x00\x00\x01\xb3") >= 3:
                    return True
    return False

def start_player(args, env):
    logf = open(Path(os.environ["TEMP"]) / "tv_up_player.log", "w")
    return subprocess.Popen([PY, "-u", PLAYER, str(args.program),
                             "--tail-mb", "15", "--strong", "--cc"],
                            env=env, stdout=logf, stderr=subprocess.STDOUT)

def verify_playing(tries=8):
    """True iff mpv's playhead advances between two samples."""
    for _ in range(tries):
        time.sleep(3)
        a = mpv_get("time-pos")
        if a is None: continue
        time.sleep(3)
        b = mpv_get("time-pos")
        if a is not None and b is not None and b > a + 0.5:
            return True
    return False

def player_diag():
    p = Path(os.environ["TEMP"]) / "tv_up_player.log"
    if p.exists():
        tail = p.read_text(errors="ignore").splitlines()[-6:]
        for ln in tail:
            log(f"  player: {ln.strip()}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=36)
    ap.add_argument("--program", type=int, default=3)
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--ifgr", type=int, default=32)
    ap.add_argument("--rfgain", type=int, default=2)
    ap.add_argument("--nb", type=float, default=2.0,
                    help="impulse blanker threshold, 0=off (default 2.0)")
    args = ap.parse_args()
    env = build_env(args)

    kill_players(); kill_chain(); time.sleep(2)
    chain = start_chain(args, env)
    log(f"chain starting RF{args.rf} (NB={'off' if args.nb<=0 else args.nb})...")
    if not wait_decode():
        log("FATAL chain produced no clean video in 70s"); sys.exit(1)
    log(f"chain decoding ({LIVE.stat().st_size//1_000_000} MB)")

    player = None
    for attempt in range(1, 4):
        kill_players(); time.sleep(1)
        player = start_player(args, env)
        time.sleep(12)
        if verify_playing():
            log(f"PLAYING VERIFIED (attempt {attempt}) — playhead advancing")
            break
        log(f"RETRY attempt {attempt}: playhead not advancing; diagnostics:")
        player_diag()
    else:
        log("FATAL player never verified after 3 attempts"); sys.exit(1)

    # ── supervision loop ──────────────────────────────────────────
    while True:
        time.sleep(30)
        if chain.poll() is not None:
            log("chain DIED — relaunching")
            chain = start_chain(args, env)
            if not wait_decode():
                log("FATAL chain relaunch failed"); sys.exit(1)
        if LIVE.exists() and LIVE.stat().st_size > ROTATE_BYTES:
            log("ROTATE live.ts hit 1 GB — planned restart on fresh file")
            kill_players(); chain.terminate()
            try: chain.wait(timeout=6)
            except Exception: chain.kill()
            time.sleep(2)
            chain = start_chain(args, env)
            if not wait_decode():
                log("FATAL chain restart failed"); sys.exit(1)
            player = start_player(args, env); time.sleep(12)
            log("PLAYING VERIFIED (post-rotate)" if verify_playing()
                else "STALL post-rotate — will retry next cycle")
            continue
        a = mpv_get("time-pos"); time.sleep(3); b = mpv_get("time-pos")
        if a is None or b is None or b <= a + 0.5:
            log("STALL detected — bouncing player")
            kill_players(); time.sleep(1)
            player = start_player(args, env); time.sleep(12)
            log("PLAYING VERIFIED (post-bounce)" if verify_playing()
                else "STALL persists — retrying next cycle")

if __name__ == "__main__":
    main()
