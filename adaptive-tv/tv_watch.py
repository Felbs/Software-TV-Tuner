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
    """tail(live.ts) -> ffmpeg -map 0:p:PROG -c copy -> live_solo.ts

    Cliff-edge fallback (2026-07-05, learned on RF7): when a stream rides
    the cliff, damaged AC-3 headers make copy-mode ffmpeg refuse to write
    ANYTHING ('sample rate not set') — audio wreckage blanks the video
    too. Mode ladder: full program -> video-only (silent TV beats a
    black screen). tv_watch's monitor loop restarts a dead extractor,
    so each restart tries the ladder afresh as the stream matures."""
    def __init__(self, prog, mode="full"):
        self.prog = prog
        self.mode = mode          # "full" or "video"
        self.stop = threading.Event()
        self.ff = None

    def _english_first_maps(self):
        """Order the program's streams video, ENGLISH audio, other audio,
        subs — mpegts copy can shed language tags, so downstream players
        default to track #1; make track #1 English at the source
        (2026-07-05: NBC solo was defaulting to the Spanish SAP)."""
        try:
            pr = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json",
                 "-probesize", "20000000", "-analyzeduration", "10000000",
                 "-show_programs", str(LIVE)],
                capture_output=True, text=True, timeout=30)
            progs = json.loads(pr.stdout or "{}").get("programs", [])
            mine = next((p for p in progs
                         if p.get("program_id") == self.prog), None)
            if not mine:
                return None
            # Map by PID ("id" field), NEVER by index: the probe indexes
            # the whole file, but the extractor demuxes a tail through a
            # pipe where indexes renumber by discovery order — index maps
            # once stitched True Crimes video to Telemundo audio. PIDs
            # live in the packets themselves; same in every context.
            vids, auds, subs = [], [], []
            for s in mine.get("streams", []):
                pid_s, ct = s.get("id"), s.get("codec_type")
                if not pid_s:
                    return None       # no PIDs = can't map safely
                pid = int(str(pid_s), 16) if str(pid_s).startswith("0x") \
                    else int(pid_s)
                lang = ((s.get("tags") or {}).get("language") or "").lower()
                if ct == "video":
                    vids.append(pid)
                elif ct == "audio":
                    auds.append((0 if lang.startswith("en") else 1, pid))
                elif ct == "subtitle":
                    subs.append(pid)
            if not vids or not auds:
                return None
            auds.sort()
            maps = []
            for pid in vids[:1] + [a[1] for a in auds] + subs:
                maps += ["-map", f"0:i:{pid}"]
            return maps
        except Exception:
            return None

    def start(self):
        if SOLO.exists():
            try: SOLO.unlink()
            except OSError: pass
        if self.mode == "full":
            maps = self._english_first_maps() or ["-map", f"0:p:{self.prog}"]
        else:
            maps = ["-map", f"0:p:{self.prog}:v:0", "-an"]
        self.ff = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "error",
             "-fflags", "+genpts+igndts+nobuffer+discardcorrupt",
             "-err_detect", "ignore_err",
             "-analyzeduration", "10000000", "-probesize", "20000000",
             "-f", "mpegts", "-i", "-"]
            + maps +
            ["-c", "copy",
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
    video_only = False
    if not muxmode:
        # extraction ladder: full program -> video-only -> mux fallback
        for mode in ("full", "video"):
            ex = Extractor(prog, mode=mode)
            ex.start()
            t0 = time.time()
            ok = False
            while time.time() - t0 < 45:
                time.sleep(1)
                if SOLO.exists() and SOLO.stat().st_size > 3_000_000:
                    ok = True
                    break
            if ok:
                video_only = (mode == "video")
                if video_only:
                    log("audio too damaged for extraction — VIDEO-ONLY mode "
                        "(silent TV beats a black screen)")
                break
            log(f"extractor mode '{mode}' produced nothing in 45s")
            ex.kill(); ex = None
        if ex is None:
            log("all extraction modes failed — falling back to mux mode")
            muxmode = True
    target = LIVE if muxmode else SOLO
    log(f"playing {'MUX' if muxmode else f'SOLO program {prog}'} file: {target.name}")

    cmd = [MPV, str(target),
           f"--input-ipc-server={IPC}",
           "--force-seekable=yes", "--keep-open=yes", "--osc=yes",
           # 2026-07-07: mpv opens NO window until its first decoded
           # frame — on cliff streams that's "nothing ever popped up".
           # Always show a window; black beats invisible.
           "--force-window=yes",
           "--cache=yes", "--demuxer-readahead-secs=5",
           "--hwdec=no", "--video-sync=audio",
           "--alang=eng,en",
           # broadcast AC-3 dialnorm runs quiet — start hot and let the
           # user go to 200% with the volume keys if a station needs it
           "--volume=130", "--volume-max=200",
           # +discardcorrupt: never hand the 608 caption decoder the
           # half-frames before the first keyframe (they render as stuck
           # "HDHDHD…" garbage until the sub track is cycled)
           "--demuxer-lavf-o=err_detect=ignore_err,fflags=+discardcorrupt",
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
        # Mux mode must NEVER hand the viewer a different tenant of the
        # multiplex (2026-07-04: asked for NBC, got TeleXitos). Ask the
        # stream's own program table (PMT) which stream indexes belong
        # to the requested program, then select mpv tracks by ff-index —
        # exact identity, not heuristics.
        want_v, want_a, want_s = None, [], []
        # Retry the probe as the stream matures: a judgment made on a
        # young/thin stream is not a verdict (2026-07-05: PMT check said
        # "no program" at t+30s, provable at t+3min once data thickened).
        for probe_try in range(3):
            try:
                pr = subprocess.run(
                    ["ffprobe", "-v", "error", "-print_format", "json",
                     "-probesize", "20000000", "-analyzeduration", "10000000",
                     "-show_programs", str(LIVE)],
                    capture_output=True, text=True, timeout=45)
                progs = json.loads(pr.stdout or "{}").get("programs", [])
                mine = next((p for p in progs
                             if p.get("program_id") == prog), None)
                if mine:
                    for s in mine.get("streams", []):
                        idx, ct = s.get("index"), s.get("codec_type")
                        lang = (s.get("tags") or {}).get("language", "")
                        if ct == "video" and want_v is None:
                            want_v = idx
                        elif ct == "audio":
                            want_a.append((idx, lang))
                        elif ct == "subtitle":
                            want_s.append(idx)
                    # english-first audio preference
                    want_a.sort(key=lambda x: 0 if x[1].startswith("en") else 1)
                    break
                log(f"PMT probe {probe_try+1}/3: no program {prog} yet — "
                    f"waiting for the stream to prove it")
            except Exception as e:
                log(f"PMT probe failed: {e}")
            if probe_try < 2:
                ipc(["show-text",
                     f"verifying program {prog}… (attempt {probe_try+2}/3)",
                     15000])
                time.sleep(20)
        if want_v is None and not want_a:
            log(f"PMT never proved program {prog} — refusing to guess "
                f"another tenant's tracks")
        deadline = time.time() + 30
        prev = -1
        while time.time() < deadline:
            tracks = ipc(["get_property", "track-list"], req=5)
            n = len(tracks) if isinstance(tracks, list) else 0
            if n and n == prev: break
            prev = n; time.sleep(2)
        tracks = ipc(["get_property", "track-list"], req=5) or []
        vid = aud = sub = None
        a_want = [i for i, _ in want_a]
        aud_pri = len(a_want)
        for t in tracks:
            ffi = t.get("ff-index")
            if t.get("type") == "video" and ffi == want_v:
                vid = t["id"]
            if t.get("type") == "audio" and ffi in a_want:
                if a_want.index(ffi) < aud_pri:
                    aud = t["id"]
                    aud_pri = a_want.index(ffi)
            if t.get("type") == "sub" and ffi in want_s and sub is None:
                sub = t["id"]
        if vid is None and want_v is None:
            # PMT couldn't vouch for the program: show nothing wrong,
            # say so on screen instead of impersonating another channel
            ipc(["show-text",
                 f"program {prog} not decodable yet — waiting for "
                 f"signal (wrong-channel guessing disabled)", 8000])
            ipc(["set_property", "vid", "no"])
            ipc(["set_property", "aid", "no"])
        else:
            if vid: ipc(["set_property", "vid", vid])
            if aud: ipc(["set_property", "aid", aud])
            if sub: ipc(["set_property", "sid", sub])
            ipc(["show-text", f"program {prog} verified via PMT", 4000])
        log(f"mux tracks by PMT: vid={vid} aid={aud} sid={sub} "
            f"(wanted v={want_v} a={a_want} s={want_s})")
    else:
        # CC: loaded but HIDDEN — off by default, and one click of the OSC
        # sub button (or 'v') shows it instantly. Force-selecting it
        # visible at startup made the toggle need a full off/on cycle.
        ipc(["set_property", "sid", 1])
        ipc(["set_property", "sub-visibility", "no"])
        # AUDIO RULE (2026-07-05): English is the default; other languages
        # are optional translations behind the # key. Trust language TAGS
        # first (mpv --alang already does when they exist); only when the
        # stream is untagged fall back to track 1 — which the extractor's
        # English-first mapping made English whenever the PMT was readable.
        # Never blind-force aid over a tagged English track (that's how a
        # Spanish default snuck onto NBC).
        tracks = ipc(["get_property", "track-list"], req=5) or []
        auds = [t for t in tracks if t.get("type") == "audio"]
        eng = next((t["id"] for t in auds
                    if str(t.get("lang", "")).lower().startswith("en")), None)
        tagged = any(t.get("lang") for t in auds)
        if eng is not None:
            ipc(["set_property", "aid", eng])
        elif not tagged and auds:
            ipc(["set_property", "aid", auds[0]["id"]])
        # else: tags exist but none English — genuinely foreign-language
        # channel, leave the broadcaster's default alone
        if video_only:
            ipc(["show-text",
                 "VIDEO-ONLY: this signal is too marginal for audio", 8000])
    seek_live_solo()

    last_pos, stall = None, 0
    # first CC cycle ~20 s in (flushes any startup caption garbage that
    # slipped through), then every 8 min as before
    last_cc = time.time() - 460
    while mpv.poll() is None:
        time.sleep(5)
        if ex is not None and not ex.alive():
            log(f"extractor died — restarting it (mode={ex.mode})")
            mode = ex.mode
            ex.kill()
            ex = Extractor(prog, mode=mode); ex.start()
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
            vis = ipc(["get_property", "sub-visibility"], req=5)
            if cur and vis:      # only cycle when captions are showing
                ipc(["set_property", "sid", "no"]); time.sleep(0.4)
                ipc(["set_property", "sid", cur])
            last_cc = time.time()
    if ex: ex.kill()
    log("mpv exited")
    # PLAYER WATCHDOG (2026-07-07 night): mpv dying while the stream is
    # still growing left "watching X" in the panel with NO window (hit
    # twice live). Relaunch with a retry cap + success condition — the
    # unbounded-respawn disease killed a GPU once; never again.
    tries = int(os.environ.get("STVT_WATCH_RETRY", "0"))
    try:
        _sz = target.stat().st_size
        time.sleep(3)
        growing = target.stat().st_size > _sz
    except OSError:
        growing = False
    if growing and tries < 2:
        os.environ["STVT_WATCH_RETRY"] = str(tries + 1)
        log(f"watchdog: stream still growing, player gone — relaunching "
            f"(retry {tries + 1}/2)")
        os.execv(sys.executable, [sys.executable, __file__] + sys.argv[1:])

if __name__ == "__main__":
    main()
