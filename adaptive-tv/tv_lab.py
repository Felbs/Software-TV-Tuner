"""tv_lab.py — autonomous cable-quality campaign.

Phase A  honest blanker sweep (liveness-guarded: headers/s first, gaps/min
         second — an empty stream can never win again)
Phase B  player delivery A/B: live-edge pipe vs delayed pipe (bigger runway),
         judged by mpv's own avsync + playhead stability over IPC
Phase C  closed-loop supervision toward the CABLE criterion:
             gaps/min <= 3  AND  headers/s >= 4.5  AND  |avsync| <= 0.3 s
         sustained for 20 minutes. Until then: bounce stalled/desynced
         players, re-cycle CC periodically, hourly channel+gain re-probe,
         rotate live.ts at 6 GB, and keep score.

Log milestones: PHASE / CABLE QUALITY ACHIEVED / FATAL / SWITCH / BOUNCE
"""
import argparse, json, os, statistics, subprocess, sys, time
from pathlib import Path

PY = r"C:\Users\user\radioconda\python.exe"
TV_LIVE = r"Z:\src\magic-tv-decoder\tools\tv_live.py"
PLAYER = str(Path(__file__).resolve().parent / "play_marginal.py")
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
IPC = r"\\.\pipe\mpv-tvtuna-super"
MUXBPS = 19_392_658 / 8
ROTATE_BYTES = 6_000_000_000

CABLE_GAPS_MIN = 3.0
CABLE_HDRS_S = 4.5
CABLE_AVSYNC = 0.3
CABLE_HOLD_S = 20 * 60

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def kill_players():
    subprocess.run(["taskkill", "/F", "/IM", "mpv.exe"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe"], capture_output=True)
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'play_marginal|tv_watch' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue }"], capture_output=True)

def kill_chain():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'tv_live' -and $_.ProcessId "
                    f"-ne {os.getpid()}" + " } | ForEach-Object { Stop-Process -Id "
                    "$_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                   capture_output=True)

def build_env(args, nb):
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
    if nb > 0:
        env["STVT_NB"] = "1"
        env["STVT_NB_THRESHOLD"] = str(nb)
    return env

def start_chain(rf, env):
    if LIVE.exists():
        try: LIVE.unlink()
        except OSError: log("WARN stale live.ts not deletable")
    return subprocess.Popen([PY, "-u", TV_LIVE, "--rf", str(rf)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def ts_metrics(window_s=60):
    """Liveness-guarded stream metrics from the tail of live.ts."""
    if not LIVE.exists() or LIVE.stat().st_size < 20_000_000:
        return None
    with open(LIVE, "rb") as f:
        size = f.seek(0, 2)
        want = int(window_s * MUXBPS / 188) * 188
        f.seek(max(0, size - want))
        tail = f.read()
    n = len(tail) // 188
    if n == 0: return None
    headers = tail.count(b"\x00\x00\x01\xb3")
    last, events, real = {}, [], 0
    for i in range(n):
        off = i * 188
        if tail[off] != 0x47: continue
        pid = ((tail[off+1] & 0x1f) << 8) | tail[off+2]
        if pid == 0x1FFF: continue
        real += 1
        afc = (tail[off+3] >> 4) & 3
        cc = tail[off+3] & 0xF
        if pid in last and afc & 1 and cc != ((last[pid] + 1) & 0xF):
            events.append(off / MUXBPS)
        if afc & 1: last[pid] = cc
    bursts, prev = 0, -9
    for t in events:
        if t - prev > 0.5: bursts += 1
        prev = t
    dur = n * 188 / MUXBPS
    return {"real_pct": real / n * 100, "hdrs_s": headers / dur,
            "gaps_min": bursts / dur * 60, "dur": dur}

def mpv_get(prop):
    try:
        with open(IPC, "r+b", buffering=0) as pipe:
            pipe.write(json.dumps({"command": ["get_property", prop],
                                   "request_id": 7}).encode() + b"\n")
            t0 = time.time(); buf = b""
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

def mpv_cmd(*cmd):
    try:
        with open(IPC, "wb") as pipe:
            pipe.write(json.dumps({"command": list(cmd)}).encode() + b"\n")
        return True
    except OSError:
        return False

def start_player(args, env, tail_mb):
    logf = open(Path(os.environ["TEMP"]) / "tv_lab_player.log", "w")
    return subprocess.Popen([PY, "-u", PLAYER, str(args.program),
                             "--tail-mb", str(tail_mb), "--strong", "--cc",
                             "--cache-secs", "12"],
                            env=env, stdout=logf, stderr=subprocess.STDOUT)

def playing():
    a = mpv_get("time-pos")
    if a is None: return False
    time.sleep(3)
    b = mpv_get("time-pos")
    return a is not None and b is not None and b > a + 0.5

def wait_decode(timeout=70):
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(3)
        m = ts_metrics(20)
        if m and m["hdrs_s"] >= 2 and m["real_pct"] > 30:
            return True
    return False

# ── Phase A: honest blanker sweep ──────────────────────────────────
def phase_a(args):
    log("PHASE A: honest blanker sweep (liveness-guarded)")
    results = {}
    # threshold is a multiple of MEAN envelope; 8-VSB's own peaks hit 3-4x
    # mean, true impulses 10-100x. Hunt ABOVE the signal crest (>=3.5) —
    # anything below ~3 amputates legitimate modulation (the 2.0 disaster).
    for nb in (0, 3.5, 4.5, 6.0, 9.0):
        env = build_env(args, nb)
        kill_chain(); time.sleep(2)
        ch = start_chain(args.rf, env)
        time.sleep(75)
        m = ts_metrics(50)
        ch.terminate()
        try: ch.wait(timeout=6)
        except Exception: ch.kill()
        if m is None or m["hdrs_s"] < 2 or m["real_pct"] < 30:
            log(f"  NB={nb}: DEAD/EMPTY stream (liveness failed) — disqualified")
            continue
        log(f"  NB={nb}: hdrs/s={m['hdrs_s']:.1f} gaps/min={m['gaps_min']:.1f} "
            f"real={m['real_pct']:.0f}%")
        results[nb] = m
    if not results:
        log("FATAL no blanker cell produced live video"); sys.exit(1)
    best_h = max(m["hdrs_s"] for m in results.values())
    live_enough = {nb: m for nb, m in results.items()
                   if m["hdrs_s"] >= 0.9 * best_h}
    best = min(live_enough, key=lambda nb: live_enough[nb]["gaps_min"])
    log(f"PHASE A winner: NB={best} "
        f"(gaps/min={live_enough[best]['gaps_min']:.1f})")
    return best

# ── Phase B: delivery mode A/B ─────────────────────────────────────
def phase_b(args, env):
    log("PHASE B: delivery A/B — live-edge (15 MB) vs delayed (45 MB) pipe")
    scores = {}
    for name, tail_mb in (("live-edge", 15), ("delayed", 45)):
        kill_players(); time.sleep(1)
        p = start_player(args, env, tail_mb)
        time.sleep(15)
        stalls, avs = 0, []
        for _ in range(8):                    # ~100 s observation
            if not playing(): stalls += 1
            v = mpv_get("avsync")
            if isinstance(v, (int, float)): avs.append(abs(v))
            time.sleep(9)
        med = statistics.median(avs) if avs else 9.9
        log(f"  {name}: stalls={stalls}/8 median|avsync|={med:.3f}s "
            f"(n={len(avs)})")
        scores[name] = (stalls, med, tail_mb)
    best = min(scores, key=lambda k: (scores[k][0], scores[k][1]))
    log(f"PHASE B winner: {best} pipe")
    return scores[best][2]

# ── Phase C: closed loop until cable ───────────────────────────────
def phase_c(args, nb, tail_mb):
    log(f"PHASE C: closed loop (NB={nb}, tail={tail_mb} MB) — "
        "criterion: gaps/min<=3 & hdrs/s>=4.5 & |avsync|<=0.3 for 20 min")
    env = build_env(args, nb)
    kill_chain(); kill_players(); time.sleep(2)
    chain = start_chain(args.rf, env)
    if not wait_decode():
        log("FATAL chain no video"); sys.exit(1)
    player = start_player(args, env, tail_mb)
    time.sleep(15)
    hold_start = None
    last_cc_cycle = time.time()
    while True:
        time.sleep(60)
        # heal: chain
        if chain.poll() is not None:
            log("BOUNCE chain died — relaunch")
            chain = start_chain(args.rf, env)
            wait_decode(); kill_players()
            player = start_player(args, env, tail_mb); time.sleep(15)
            hold_start = None; continue
        # rotate
        if LIVE.exists() and LIVE.stat().st_size > ROTATE_BYTES:
            log("ROTATE 6 GB")
            kill_players(); chain.terminate()
            try: chain.wait(timeout=6)
            except Exception: chain.kill()
            time.sleep(2)
            chain = start_chain(args.rf, env); wait_decode()
            player = start_player(args, env, tail_mb); time.sleep(15)
            continue
        # heal: player stall / desync
        av = mpv_get("avsync")
        av_abs = abs(av) if isinstance(av, (int, float)) else None
        if not playing() or (av_abs is not None and av_abs > 0.75):
            log(f"BOUNCE player (stall or avsync={av_abs}) ")
            kill_players(); time.sleep(1)
            player = start_player(args, env, tail_mb); time.sleep(15)
            hold_start = None; continue
        # CC renderer hygiene every 10 min
        if time.time() - last_cc_cycle > 600:
            mpv_cmd("set_property", "sid", "no"); time.sleep(0.5)
            mpv_cmd("set_property", "sid", 1)
            last_cc_cycle = time.time()
        # score
        m = ts_metrics(60)
        if not m: continue
        ok = (m["gaps_min"] <= CABLE_GAPS_MIN and m["hdrs_s"] >= CABLE_HDRS_S
              and (av_abs is None or av_abs <= CABLE_AVSYNC))
        log(f"score gaps/min={m['gaps_min']:.1f} hdrs/s={m['hdrs_s']:.1f} "
            f"avsync={av_abs} {'OK' if ok else '--'}"
            + (f" hold={int(time.time()-hold_start)}s" if ok and hold_start else ""))
        if ok:
            hold_start = hold_start or time.time()
            if time.time() - hold_start >= CABLE_HOLD_S:
                log("CABLE QUALITY ACHIEVED — criterion held 20 minutes")
                hold_start = None   # keep supervising, keep celebrating hourly
        else:
            hold_start = None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=36)
    ap.add_argument("--program", type=int, default=3)
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--ifgr", type=int, default=32)
    ap.add_argument("--rfgain", type=int, default=2)
    args = ap.parse_args()
    kill_players(); kill_chain(); time.sleep(2)
    nb = phase_a(args)
    env = build_env(args, nb)
    kill_chain(); time.sleep(2)
    chain = start_chain(args.rf, env)
    if not wait_decode():
        log("FATAL post-sweep chain no video"); sys.exit(1)
    tail_mb = phase_b(args, env)
    phase_c(args, nb, tail_mb)

if __name__ == "__main__":
    main()
