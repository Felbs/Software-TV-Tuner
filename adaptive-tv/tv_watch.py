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
import json, os, shutil, subprocess, sys, threading, time
from pathlib import Path

IS_WIN = sys.platform == "win32"
HERE = Path(__file__).resolve().parent
if IS_WIN:
    MPV = r"C:\Program Files\MPV Player\mpv.exe"
    FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
    _LIVE_DIR = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live")
    IPC = r"\\.\pipe\mpv-tvtuna-super"
else:   # platform layer: PATH binaries, in-repo live dir, unix IPC socket
    MPV = shutil.which("mpv") or "mpv"
    FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
    _LIVE_DIR = HERE.parent / "tools" / "data" / "tv_live"
    IPC = "/tmp/mpv-tvtuna-super"
LIVE = _LIVE_DIR / "live.ts"
SOLO = _LIVE_DIR / "live_solo.ts"
MUXBPS = 19_392_658 / 8

# MEMORY-TUNE (2026-07-10): PID/program-layout cache. Discovery (the 20 MB
# ffprobe below) costs ~8-11 s per tune; the layout per (rf, prog) changes
# rarely (broadcaster remaps). Cache it, start the extractor immediately on
# a hit with small probe windows, and SELF-HEAL: if cached PIDs produce
# nothing in ~8 s, invalidate the entry and redo full discovery.
RF = os.environ.get("STVT_RF", "?")     # panel sets this; "?" disables cache
PID_CACHE = Path(__file__).resolve().parent / "lab" / "pid_cache.json"

def load_pid_cache():
    try:
        return json.loads(PID_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

def save_pid_cache(cache):
    try:
        PID_CACHE.parent.mkdir(parents=True, exist_ok=True)
        PID_CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    except OSError:
        pass

def pmt_pids(prog, timeout=5):
    """Cheap PSI-only read: which PIDs does live.ts's PMT list for prog
    right now? PSI tables repeat every ~100 ms, so a 3 MB probe is plenty
    for the table itself (codec detail may be incomplete — not needed).
    None = couldn't tell (young/thin file) — caller stays optimistic."""
    try:
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-probesize", "3000000", "-analyzeduration", "1000000",
             "-show_programs", str(LIVE)],
            capture_output=True, text=True, timeout=timeout)
        progs = json.loads(pr.stdout or "{}").get("programs", [])
        mine = next((p for p in progs if p.get("program_id") == prog), None)
        if not mine:
            return None
        pids = set()
        for s in mine.get("streams", []):
            pid_s = s.get("id")
            if not pid_s:
                continue
            pids.add(int(str(pid_s), 16) if str(pid_s).startswith("0x")
                     else int(pid_s))
        return pids or None
    except Exception:
        return None

def _ipc_scan(buf, req):
    for line in buf.split(b"\n"):
        if not line.strip(): continue
        try: r = json.loads(line)
        except ValueError: continue
        if r.get("request_id") == req:
            return r.get("data")
    return None

def ipc(command, req=None):
    msg = {"command": command}
    if req is not None: msg["request_id"] = req
    data = json.dumps(msg).encode() + b"\n"
    try:
        if IS_WIN:   # mpv IPC = named pipe on Windows (plain file open works)
            with open(IPC, "r+b", buffering=0) as p:
                p.write(data)
                if req is None: return True
                t0 = time.time(); buf = b""
                while time.time() - t0 < 3:
                    buf += p.read(4096)
                    got = _ipc_scan(buf, req)
                    if got is not None: return got
            return None
        # Linux/Mac: mpv IPC = unix domain socket
        import socket
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect(IPC)
            s.sendall(data)
            if req is None: return True
            t0 = time.time(); buf = b""
            while time.time() - t0 < 3:
                try: chunk = s.recv(4096)
                except OSError: break
                if not chunk: break
                buf += chunk
                got = _ipc_scan(buf, req)
                if got is not None: return got
    except OSError:
        return None
    return None

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def pid_packet_counts(pids, max_bytes=6_000_000):
    """How many TS packets does live.ts actually CARRY per pid (tail
    sample)? A PMT can declare streams the broadcaster never transmits
    (2026-07-11: WDVM-SD lists a Spanish SAP with zero packets ever) —
    mapping such a ghost makes copy-mode ffmpeg die at header time
    ('sample rate not set') and the whole extraction fails. One chunked
    read at extractor start (not a poll loop)."""
    counts = {p: 0 for p in pids}
    try:
        size = LIVE.stat().st_size
        with open(LIVE, "rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read(max_bytes)
        off = next((o for o in range(188)
                    if all(data[o + i * 188] == 0x47 for i in range(5))), None)
        if off is None:
            return counts
        for i in range(off, len(data) - 188, 188):
            if data[i] != 0x47:
                continue
            pid = ((data[i + 1] & 0x1F) << 8) | data[i + 2]
            if pid in counts:
                counts[pid] += 1
    except OSError:
        pass
    return counts


def drop_ghost_streams(vids, others, label):
    """Keep video unconditionally; drop declared-but-silent extras."""
    counts = pid_packet_counts(list(vids) + list(others))
    kept = [p for p in others if counts.get(p, 0) >= 5]
    ghosts = [p for p in others if counts.get(p, 0) < 5]
    if ghosts:
        log(f"{label}: dropping ghost stream PIDs {ghosts} "
            "(declared in PMT, zero packets on air)")
    return kept


class Extractor:
    """tail(live.ts) -> ffmpeg -map 0:p:PROG -c copy -> live_solo.ts

    Cliff-edge fallback (2026-07-05, learned on RF7): when a stream rides
    the cliff, damaged AC-3 headers make copy-mode ffmpeg refuse to write
    ANYTHING ('sample rate not set') — audio wreckage blanks the video
    too. Mode ladder: full program -> video-only (silent TV beats a
    black screen). tv_watch's monitor loop restarts a dead extractor,
    so each restart tries the ladder afresh as the stream matures."""
    def __init__(self, prog, mode="full", pids=None):
        self.prog = prog
        self.mode = mode          # "cached", "full" or "video"
        self.pids = pids          # ordered PID list (cached mode / discovered)
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
            extras = drop_ghost_streams(vids[:1],
                                        [a[1] for a in auds] + subs, "full")
            if not extras:
                return None           # every audio is a ghost: video mode
            self.pids = vids[:1] + extras
            self.n_subs = len(subs)
            maps = []
            for pid in self.pids:
                maps += ["-map", f"0:i:{pid}"]
            return maps
        except Exception:
            return None

    def start(self):
        if SOLO.exists():
            try: SOLO.unlink()
            except OSError: pass
        fast = False
        if self.mode == "cached" and self.pids:
            # layout known from a previous tune: no discovery probe, and
            # the demuxer needs only a small window (stream shape known).
            # Ghost-proof: never map a declared-but-silent PID (it kills
            # copy-mode ffmpeg at header time and fails the whole tune)
            vid, extras = self.pids[:1], self.pids[1:]
            live_extras = drop_ghost_streams(vid, extras, "cached")
            self.pids = vid + live_extras
            maps = []
            for pid in self.pids:
                maps += ["-map", f"0:i:{pid}"]
            fast = True
        elif self.mode == "full":
            maps = self._english_first_maps() or ["-map", f"0:p:{self.prog}"]
        else:
            maps = ["-map", f"0:p:{self.prog}:v:0", "-an"]
        self.ff = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "error",
             # CC-salad forensics 2026-07-11: garbled captions
             # ("PORTIA4093H@" letter-salad) were proven ON-AIR — ffmpeg
             # decoded the same salad from the RAW mux (program 3, 19:00
             # show), while the 18:50 show's captions decoded clean
             # through this exact pipeline. These flags are innocent.
             # discardcorrupt removed 7/10: the I-frame killer (anti-mosh)
             "-fflags", "+genpts+igndts+nobuffer",
             "-err_detect", "ignore_err",
             "-analyzeduration", "2000000" if fast else "10000000",
             "-probesize", "3000000" if fast else "20000000",
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

    # MEMORY-TUNE: on a PID-cache hit the discovery probe is skipped, so a
    # smaller prebuffer suffices; cache-MISS keeps the 20 MB law (multi-
    # program ffprobe needs a thick file to see every program).
    cache = load_pid_cache()
    key = f"{RF}:{prog}"
    hit = cache.get(key) if RF != "?" else None
    cached_pids = None
    if hit:
        cached_pids = ([hit["vpid"]] + list(hit.get("apids", []))
                       + list(hit.get("spids", []))) if "vpid" in hit else None
    min_live = 6_000_000 if cached_pids else 25_000_000
    while not LIVE.exists() or LIVE.stat().st_size < min_live:
        time.sleep(2)

    if cached_pids:
        # VERIFY WHILE TUNING: a remapped-but-still-existing PID would
        # extract the WRONG program and flow happily — the one stale case
        # the no-output fallback can't catch. A PSI-only probe (~1 s)
        # checks the cached PIDs are still this program's PIDs. Probe
        # inconclusive (young file) -> stay optimistic; the 8 s output
        # gate still guards vanished PIDs.
        live_pids = pmt_pids(prog)
        if live_pids is not None and not set(cached_pids) <= live_pids:
            log(f"PID cache for {key} is STALE (PMT remapped: cached "
                f"{cached_pids} vs live {sorted(live_pids)}) — "
                "invalidating, full discovery")
            cache.pop(key, None)
            save_pid_cache(cache)
            cached_pids = None

    ex = None
    video_only = False
    if not muxmode:
        # extraction ladder: cached (if hit) -> full -> video -> mux fallback
        # cached gets an ~8 s proof window and a lower launch threshold;
        # a stale entry self-heals (invalidate + full discovery).
        ladder = ([("cached", 8, 1_200_000)] if cached_pids else []) \
            + [("full", 45, 3_000_000), ("video", 45, 3_000_000)]
        for mode, patience, need in ladder:
            if mode != "cached":
                # discovery law: the 20 MB prebuffer for N-program ffprobe
                while not LIVE.exists() or LIVE.stat().st_size < 25_000_000:
                    time.sleep(2)
            ex = Extractor(prog, mode=mode,
                           pids=cached_pids if mode == "cached" else None)
            ex.start()
            t0 = time.time()
            ok = False
            while time.time() - t0 < patience:
                time.sleep(0.5)
                if SOLO.exists() and SOLO.stat().st_size > need:
                    ok = True
                    break
                if not ex.alive() and time.time() - t0 > 2:
                    break        # extractor died young — fail fast down the ladder
            if ok:
                video_only = (mode == "video")
                if mode == "cached":
                    log(f"PID cache HIT for {key} — extractor started "
                        "with known layout (discovery skipped)")
                if video_only:
                    log("audio too damaged for extraction — VIDEO-ONLY mode "
                        "(silent TV beats a black screen)")
                if mode == "full" and ex.pids and RF != "?":
                    # proven discovery -> remember the layout for next time
                    ns = getattr(ex, "n_subs", 0)
                    cut = len(ex.pids) - ns
                    cache[key] = {"vpid": ex.pids[0],
                                  "apids": ex.pids[1:cut],
                                  "spids": ex.pids[cut:],
                                  "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
                    save_pid_cache(cache)
                break
            log(f"extractor mode '{mode}' produced nothing in {patience}s")
            if mode == "cached":
                log(f"stale PID cache for {key} — invalidating, "
                    "falling back to full discovery")
                cache.pop(key, None)
                save_pid_cache(cache)
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
           # ANTI-MOSH (2026-07-10, agent-measured on the bottled Fox
           # impulse storm): the 7/04 +discardcorrupt captions fix was
           # the datamosh AMPLIFIER — it drops whole pictures on one bad
           # packet, killing I-frames first (11/100 survived), leaving
           # P/B frames to smear against stale refs for up to 6.4 s.
           # New set: keep 99%-intact pictures; +explode drops the truly
           # unparseable ones individually (cost: ~7 frames/storm);
           # favor_inter+deblock patch damaged blocks as static copies
           # (freeze beats melt). Measured: mosh frames 67.5% -> 0.7%,
           # frames shown +89%. All flags inert on clean streams.
           "--demuxer-lavf-o=err_detect=ignore_err",
           # damaged bursts corrupt size/aspect metadata and mpv
           # resizes the window to match ("screen randomly gets wider
           # then narrower", 2026-07-11) — keep the window put
           "--auto-window-resize=no",
           "--vd-lavc-o=err_detect=+crccheck+bitstream+buffer+explode,"
           "error_concealment=deblock+favor_inter",
           "--sub-create-cc-track=yes",
           # captions OFF by default (user pref 2026-07-11) — the CC track
           # still exists, press j in the player to toggle it on/off.
           "--sid=no",
           f"--title=TV Tuna — program {prog}" + (" (solo)" if not muxmode else ""),
           ]
    if not IS_WIN:
        # WSLg/Wayland: the gpu VO black-screens under WSLg — wlshm is the
        # June-proven reliable path (override with STVT_MPV_VO).
        cmd += [f"--vo={os.environ.get('STVT_MPV_VO', 'wlshm')}"]
    if marginal:
        # show-all removed 7/10: it forces pre-keyframe garbage frames
        # onto the screen — the opposite of anti-mosh
        cmd += ["--vd-lavc-skipframe=none", "--framedrop=no"]
    mpv = subprocess.Popen(cmd)

    if not IS_WIN:
        # FORMAT-AWARE DEINTERLACE (2026-07-11): 1080-line ATSC is always
        # interlaced and NEEDS yadif (woven-field combing without it, the
        # RF34 WRC lesson) — but a blanket --deinterlace=yes was a trap:
        # FOX flags its 720p60 frames interlaced, yadif field-doubled
        # them to 119.88 fps and the software wlshm VO choked (sound
        # played over a stuck picture). ATSC has no 720i, so the honest
        # rule is BY HEIGHT: >=1080 -> deinterlace on, else off. Height
        # is read from mpv itself once the first frames decode.
        def _auto_deint():
            for _ in range(40):                     # up to ~20 s
                time.sleep(0.5)
                h = ipc(["get_property", "video-params/h"], req=411)
                if h:
                    if int(h) >= 1080:
                        ipc(["set_property", "deinterlace", True])
                        log("auto-deinterlace ON (%s-line interlaced)" % h)
                    else:
                        log("auto-deinterlace off (%sp progressive)" % h)
                    return
        threading.Thread(target=_auto_deint, daemon=True).start()
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
            mode, pids = ex.mode, ex.pids
            ex.kill()
            ex = Extractor(prog, mode=mode, pids=pids); ex.start()
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
