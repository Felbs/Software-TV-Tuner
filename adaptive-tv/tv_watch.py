"""tv_watch.py — file-native live player with a resync watchdog.

The tail->ffmpeg->mpv pipe cannot recover from stream damage: video
stalls on a hole, audio keeps going, A/V runs away (measured +2.6 s in
19 s) and no correction is possible because a pipe cannot seek. This
player gives mpv the growing live.ts FILE instead: seekable, so the
watchdog can always jump back to the live edge — one motion that resets
A/V sync, un-sticks captions, and rides out any stall.

  - selects video/audio/cc tracks by PROGRAM ID via mpv's track-list
  - watchdog every 5 s: if paused/eof/stalled or fallen >12 s behind
    the live edge, seek to live-3s and resume
  - IPC on \\.\pipe\mpv-tvtuna-super (same name the supervisors use)

Usage: python tv_watch.py [program] (default 3)
"""
import json, subprocess, sys, time
from pathlib import Path

MPV = r"C:\Program Files\MPV Player\mpv.exe"
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
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

def live_edge_secs():
    return LIVE.stat().st_size / MUXBPS

def seek_live():
    ipc(["set_property", "pause", False])
    ipc(["seek", max(0.0, live_edge_secs() - 3.0), "absolute+keyframes"])

def main():
    prog = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    while not LIVE.exists() or LIVE.stat().st_size < 25_000_000:
        time.sleep(2)
    mpv = subprocess.Popen([
        MPV, str(LIVE),
        f"--input-ipc-server={IPC}",
        "--force-seekable=yes", "--keep-open=yes", "--osc=yes",
        "--cache=yes", "--demuxer-readahead-secs=5",
        "--hwdec=no", "--video-sync=audio",
        "--demuxer-lavf-o=err_detect=ignore_err",
        "--vd-lavc-o=error_concealment=3,err_detect=ignore_err",
        "--sub-create-cc-track=yes",
        f"--title=TV Tuna — program {prog}",
    ] + (["--vd-lavc-show-all=yes", "--vd-lavc-skipframe=none",
          "--framedrop=no"]
         if "marginal" in sys.argv[2:] else []))
    log(f"mpv up, waiting for tracks (program {prog})")
    # track selection by program id
    deadline = time.time() + 30
    picked = False
    while time.time() < deadline and not picked:
        time.sleep(2)
        tracks = ipc(["get_property", "track-list"], req=5)
        if not isinstance(tracks, list): continue
        vid = aud = sub = None
        for t in tracks:
            if t.get("program-id") == prog or prog is None:
                if t.get("type") == "video" and vid is None: vid = t["id"]
                if t.get("type") == "audio" and aud is None: aud = t["id"]
            if t.get("type") == "sub" and sub is None: sub = t["id"]
        if vid:
            ipc(["set_property", "vid", vid])
            if aud: ipc(["set_property", "aid", aud])
            if sub: ipc(["set_property", "sid", sub])
            log(f"tracks: vid={vid} aid={aud} sid={sub} (program {prog})")
            picked = True
    if not picked:
        log("WARN program tracks not found; mpv defaults in effect")
    seek_live()

    # ── watchdog ──────────────────────────────────────────────────
    last_pos, stall = None, 0
    last_cc = time.time()
    while mpv.poll() is None:
        time.sleep(5)
        pos = ipc(["get_property", "time-pos"], req=5)
        eof = ipc(["get_property", "eof-reached"], req=5)
        paused = ipc(["get_property", "pause"], req=5)
        edge = live_edge_secs()
        if not isinstance(pos, (int, float)):
            continue
        behind = edge - pos
        stalled = last_pos is not None and pos <= last_pos + 0.5
        last_pos = pos
        if eof or paused or stalled:
            stall += 1
        else:
            stall = 0
        if stall >= 2 or behind > 12:
            log(f"RESYNC (behind={behind:.1f}s stall={stall} eof={eof})")
            seek_live()
            stall = 0
        if time.time() - last_cc > 480:
            cur = ipc(["get_property", "sid"], req=5)
            if cur:
                ipc(["set_property", "sid", "no"]); time.sleep(0.4)
                ipc(["set_property", "sid", cur])
            last_cc = time.time()
    log("mpv exited")

if __name__ == "__main__":
    main()
