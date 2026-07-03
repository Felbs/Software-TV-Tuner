"""tv_watch.py — file-native live player with a resync watchdog.

SOLO MODE (default): a background extractor tails the growing mux file
and continuously remuxes JUST the requested program (-c copy) into its
own growing file; mpv plays that. The track menu shows only the current
station — its video, its English/Spanish audio, one caption track —
instead of the entire transmitter (8 audios / 6 CCs of mux confusion).
Seekability is preserved, so the watchdog can always hop back to the
live edge: one motion that resets A/V sync, un-sticks captions, and
rides out stalls. (Benched vs the old stdin pipe: A/V drift +0.009 vs
+3.355 s/min.)

Usage: python tv_watch.py [program] [marginal] [mux]
  marginal  add show-all/no-skip decode flags (cliff-edge forced video)
  mux       legacy mode: play the whole mux file directly (all tracks)
"""
import json, os, subprocess, sys, threading, time
from pathlib import Path

MPV = r"C:\Program Files\MPV Player\mpv.exe"
FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
SOLO = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live_solo.ts")
IPC = r"\\.\pipe\mpv-tvtuna-super"
MUXBPS = 19_392_658 / 8

def ipc(command, req=None):
    msg = {"command": command}
    if req is not None: msg["request_id"] = req
    try:
        with open(IPC, "r+b", buffering=0) as p:
            p.write(json.dumps(msg).encode() + b"\n")
            if req is None: return True
            t0 = time.time(); buf = b""
            while time.time() - t0 < 3:
                buf += p.read(4096)
                for line in buf.split(b"\n"):
                    if not line.strip(): continue
                    try: r = json.loads(line)
                    except ValueError: continue
                    if r.get("request_id") == req:
                        return r.get("data")
    except OSError:
        return None
    return None

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


class Extractor:
    """tail(live.ts) -> ffmpeg -map 0:p:PROG -c copy -> live_solo.ts"""
    def __init__(self, prog):
        self.prog = prog
        self.stop = threading.Event()
        self.ff = None

    def start(self):
        if SOLO.exists():
            try: SOLO.unlink()
            except OSError: pass
        self.ff = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "error",
             "-fflags", "+genpts+igndts+nobuffer+discardcorrupt",
             "-err_detect", "ignore_err",
             "-analyzeduration", "3000000", "-probesize", "5000000",
             "-f", "mpegts", "-i", "-",
             "-map", f"0:p:{self.prog}", "-c", "copy",
             "-max_interleave_delta", "0",
             "-f", "mpegts", "-y", str(SOLO)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        threading.Thread(target=self._tail, daemon=True).start()

    def _tail(self):
        with open(LIVE, "rb") as f:
            size = LIVE.stat().st_size
            start = max(0, size - 15 * 1024 * 1024)
            f.seek((start // 188) * 188)
            idle = 0
            while not self.stop.is_set() and self.ff.poll() is None:
                chunk = f.read(188 * 1024)
                if not chunk:
                    idle += 1
                    time.sleep(0.25)
                    if idle > 240:      # 60 s of no growth: chain likely bounced
                        break
                    continue
                idle = 0
                try:
                    self.ff.stdin.write(chunk)
                except OSError:
                    break
        try: self.ff.stdin.close()
        except Exception: pass

    def alive(self):
        return self.ff is not None and self.ff.poll() is None

    def kill(self):
        self.stop.set()
        if self.ff and self.ff.poll() is None:
            self.ff.terminate()
            try: self.ff.wait(timeout=5)
            except Exception: self.ff.kill()


def solo_edge():
    try: return SOLO.stat().st_size / (MUXBPS / 4)   # rough: solo ~ 1/4 mux
    except OSError: return 0.0

def seek_live_solo():
    ipc(["set_property", "pause", False])
    # duration of a growing TS is unreliable; seek by percent near the end
    ipc(["seek", "98", "absolute-percent+keyframes"])

def main():
    args = sys.argv[1:]
    prog = int(args[0]) if args and args[0].isdigit() else 3
    marginal = "marginal" in args
    muxmode = "mux" in args

    while not LIVE.exists() or LIVE.stat().st_size < 25_000_000:
        time.sleep(2)

    ex = None
    if not muxmode:
        ex = Extractor(prog)
        ex.start()
        t0 = time.time()
        while (not SOLO.exists() or SOLO.stat().st_size < 3_000_000):
            time.sleep(1)
            if time.time() - t0 > 45:
                log("extractor produced nothing in 45s — falling back to mux mode")
                ex.kill(); ex = None; muxmode = True
                break
    target = LIVE if muxmode else SOLO
    log(f"playing {'MUX' if muxmode else f'SOLO program {prog}'} file: {target.name}")

    cmd = [MPV, str(target),
           f"--input-ipc-server={IPC}",
           "--force-seekable=yes", "--keep-open=yes", "--osc=yes",
           "--cache=yes", "--demuxer-readahead-secs=5",
           "--hwdec=no", "--video-sync=audio",
           "--alang=eng,en",
           "--demuxer-lavf-o=err_detect=ignore_err",
           "--vd-lavc-o=error_concealment=3,err_detect=ignore_err",
           "--sub-create-cc-track=yes",
           f"--title=TV Tuna — program {prog}" + (" (solo)" if not muxmode else ""),
           ]
    if marginal:
        cmd += ["--vd-lavc-show-all=yes", "--vd-lavc-skipframe=none",
                "--framedrop=no"]
    mpv = subprocess.Popen(cmd)
    time.sleep(6)

    if muxmode:
        # mux mode: pick tracks by program id once the list settles
        deadline = time.time() + 30
        prev = -1
        while time.time() < deadline:
            tracks = ipc(["get_property", "track-list"], req=5)
            n = len(tracks) if isinstance(tracks, list) else 0
            if n and n == prev: break
            prev = n; time.sleep(2)
        tracks = ipc(["get_property", "track-list"], req=5) or []
        vid = aud = sub = None
        for t in tracks:
            if t.get("program-id") == prog:
                if t.get("type") == "video" and vid is None: vid = t["id"]
                if t.get("type") == "audio" and (aud is None or
                                                 str(t.get("lang", "")).startswith("en")):
                    aud = t["id"]
                if t.get("type") == "sub" and sub is None: sub = t["id"]
        if vid: ipc(["set_property", "vid", vid])
        if aud: ipc(["set_property", "aid", aud])
        if sub: ipc(["set_property", "sid", sub])
        log(f"mux tracks selected vid={vid} aid={aud} sid={sub}")
    else:
        ipc(["set_property", "sid", 1])   # the only CC track = this program's
    seek_live_solo()

    last_pos, stall = None, 0
    last_cc = time.time()
    while mpv.poll() is None:
        time.sleep(5)
        if ex is not None and not ex.alive():
            log("extractor died — restarting it")
            ex.kill()
            ex = Extractor(prog); ex.start()
            time.sleep(3)
        pos = ipc(["get_property", "time-pos"], req=5)
        eof = ipc(["get_property", "eof-reached"], req=5)
        paused = ipc(["get_property", "pause"], req=5)
        if not isinstance(pos, (int, float)):
            continue
        stalled = last_pos is not None and pos <= last_pos + 0.5
        last_pos = pos
        stall = stall + 1 if (eof or paused or stalled) else 0
        if stall >= 2:
            log(f"RESYNC (stall={stall} eof={eof})")
            seek_live_solo()
            stall = 0
        if time.time() - last_cc > 480:
            cur = ipc(["get_property", "sid"], req=5)
            if cur:
                ipc(["set_property", "sid", "no"]); time.sleep(0.4)
                ipc(["set_property", "sid", cur])
            last_cc = time.time()
    if ex: ex.kill()
    log("mpv exited")

if __name__ == "__main__":
    main()
