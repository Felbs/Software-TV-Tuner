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
    MPV = (os.environ.get("MPV_EXE") or shutil.which("mpv")
           or r"C:\Program Files\MPV Player\mpv.exe")
    FFMPEG = (os.environ.get("FFMPEG_EXE") or shutil.which("ffmpeg")
              or r"C:\ffmpeg\bin\ffmpeg.exe")
    IPC = r"\\.\pipe\mpv-tvtuna-super"
else:   # platform layer: PATH binaries, unix IPC socket
    MPV = shutil.which("mpv") or "mpv"
    FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
    IPC = "/tmp/mpv-tvtuna-super"
# live dir: the repo's own tools/data/tv_live (chain and watcher in the
# same tree). Split deployments (chain in one tree, watcher in another)
# point STVT_LIVE_DIR at the chain's output dir.
_LIVE_DIR = Path(os.environ.get("STVT_LIVE_DIR")
                 or (HERE.parent / "tools" / "data" / "tv_live"))
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


def first_av_program(exclude=None):
    """Lowest program_id in live.ts carrying BOTH video and audio — the
    graceful single-program target when the REQUESTED program is absent
    or data-only. ATSC program numbers are rarely 1..N (a blind or
    guessed number lands on no PMT), and dumping the whole multi-program
    mux at the player gives every program's audio + one CC track each:
    the 'wall of audio tracks / scrambled captions' glitch. Extracting
    one real program instead keeps the player clean."""
    try:
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-probesize", "20000000", "-analyzeduration", "10000000",
             "-show_programs", str(LIVE)],
            capture_output=True, text=True, timeout=30)
        cands = []
        for p in json.loads(pr.stdout or "{}").get("programs", []):
            pidn = p.get("program_id")
            if pidn is None or pidn == exclude:
                continue
            types = {s.get("codec_type") for s in p.get("streams", [])}
            if "video" in types and "audio" in types:
                cands.append(pidn)
        return min(cands) if cands else None
    except Exception:
        return None


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
    # Seek to the LIVE EDGE with a small cushion, SIZE-INDEPENDENTLY.
    # 2026-07-13: the old "98%" seek jumped back MINUTES once live_solo grew
    # to hours of uptime (98% of a 2.5 h file = ~3 min behind) — that was
    # the resync LOOP the user saw. Seek to (duration - cushion) instead:
    # always ~a few seconds from the edge no matter how long the file is.
    # mpv DOES track a live duration for the growing TS (verified: dur/pos
    # agree to ~1 s); fall back to 98% only if it can't report one yet.
    cushion = float(os.environ.get("STVT_LIVE_CUSHION", "4"))
    dur = ipc(["get_property", "duration"], req=71)
    if isinstance(dur, (int, float)) and dur > cushion + 2:
        ipc(["seek", str(round(dur - cushion, 1)), "absolute+keyframes"])
    else:
        ipc(["seek", "98", "absolute-percent+keyframes"])

def main():
    args = sys.argv[1:]
    # Headless / tuner-only mode: the Pi has no HW MPEG-2 decode, so when it is
    # acting purely as an HDHomeRun tuner (streaming to Jellyfin/Plex/etc.) it
    # should not launch a local player at all — 100% of its CPU goes to the
    # tuner chain. The panel still launches this script per tune; it just no-ops.
    if os.environ.get("STVT_HEADLESS", "0") not in ("0", "", "false", "no"):
        log("STVT_HEADLESS=1 — tuner-only mode; no local player "
            "(the HDHomeRun server / media client handles playback)")
        return
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
            # Proof must measure GROWTH, not absolute size. Found in real use
            # 2026-07-30: a 59.6 MB live_solo.ts left over from an earlier
            # session passed `size > need` instantly, so a cached-PID
            # extractor that wrote NOTHING was declared proven, mpv was pointed
            # at a file whose tail never advanced, and the watch loop resynced
            # every 10 s forever — a frozen picture with glitchy captions.
            # Baseline AFTER start(): start() unlinks/recreates SOLO, so a
            # pre-start baseline still counts the stale file and dooms the
            # rung (needs base+need from a file that restarts at 0 bytes).
            ex.start()
            base = SOLO.stat().st_size if SOLO.exists() else 0
            t0 = time.time()
            ok = False
            while time.time() - t0 < patience:
                time.sleep(0.5)
                if SOLO.exists() and SOLO.stat().st_size > base + need:
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
            # The requested program yielded nothing — most often it does
            # not exist in this mux (a blind/guessed number lands on no
            # PMT). Before dumping the ENTIRE multi-program mux at the
            # player (every program's audio + a CC track each = the "9
            # audios / scrambled captions" glitch), extract the first
            # REAL video+audio program instead.
            alt = first_av_program(exclude=prog)
            if alt is not None:
                log(f"program {prog} has no extractable A/V — extracting "
                    f"the first real program {alt} instead of the whole mux")
                for mode in ("full", "video"):
                    cand = Extractor(alt, mode=mode)
                    cand.start()
                    t0 = time.time()
                    while time.time() - t0 < 45:
                        time.sleep(0.5)
                        if SOLO.exists() and SOLO.stat().st_size > 3_000_000:
                            ex, prog, video_only = cand, alt, (mode == "video")
                            break
                        if not cand.alive() and time.time() - t0 > 2:
                            break
                    if ex is not None:
                        break
                    cand.kill()
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
           f"--title=TV Tuna — program {prog}" + (" (solo)" if not muxmode else ""),
           ]
    # captions OFF by default: showing the CC track at startup renders it
    # glitched until toggled (mpv's cc_dec needs the stream established first).
    # The track is still created + selectable — press 'v' (or set STVT_CC=on) to
    # show it; it renders clean once the stream is up.
    _cc_on = os.environ.get("STVT_CC", "off").lower() not in ("off", "0", "no", "false")
    cmd.append("--sid=1" if _cc_on else "--sid=no")
    # STVT_MPV_EXTRA: space-separated extra mpv flags — the experiment
    # hook (A/B player knobs without code edits). Empty by default.
    cmd += [f for f in os.environ.get("STVT_MPV_EXTRA", "").split() if f]
    if not IS_WIN:
        # WSLg/Wayland: the gpu VO black-screens under WSLg — wlshm is the
        # June-proven reliable path (override with STVT_MPV_VO).
        cmd += [f"--vo={os.environ.get('STVT_MPV_VO', 'wlshm')}"]
    # STVT_DEINT (2026-07-12, ported from the June Pi lineage):
    #   auto     (default) full-res yadif toggled at runtime for 1080-line
    #            content — right for machines with CPU headroom
    #   lowdeint June Pi recipe: half-res decode (lowres=1) + bwdif
    #            send_frame — full-res software deinterlace does NOT fit
    #            small boards (measured: NBC 1080i re-overloaded the Pi 5
    #            chain, 4.3 oso/min + 2.2% cc-err through the full stack)
    #   off      never deinterlace
    LOWDEINT_FLAGS = ["--vd-lavc-o=lowres=1",
                      "--vf=lavfi=[bwdif=mode=send_frame:deint=interlaced]"]
    deint_mode = os.environ.get("STVT_DEINT", "auto")
    lowdeint_applied = False
    if (not IS_WIN and deint_mode == "lowdeint"
            and (hit or {}).get("vheight", 0) >= 1080):
        # decoder-open options — must ride the launch; height cached by a
        # previous visit (see _auto_deint below)
        cmd += LOWDEINT_FLAGS
        lowdeint_applied = True
        log("lowdeint at launch (cached %s-line)" % hit["vheight"])
    if marginal:
        # show-all removed 7/10: it forces pre-keyframe garbage frames
        # onto the screen — the opposite of anti-mosh
        cmd += ["--vd-lavc-skipframe=none", "--framedrop=no"]
    mpv_h = {"p": subprocess.Popen(cmd)}

    if not IS_WIN:
        # FORMAT-AWARE DEINTERLACE (2026-07-11): 1080-line ATSC is always
        # interlaced and NEEDS a deinterlacer (woven-field combing, the
        # RF34 WRC lesson) — but FOX flags its 720p60 frames interlaced
        # and a blanket yadif field-doubled them to 119.88 fps (choked
        # the software VO). ATSC has no 720i, so the rule is BY HEIGHT.
        # Height is read from mpv once frames decode, then CACHED so
        # lowdeint launches can apply decoder-open flags immediately.
        def _auto_deint():
            for _ in range(40):                     # up to ~20 s
                time.sleep(0.5)
                h = ipc(["get_property", "video-params/h"], req=411)
                if not h:
                    continue
                h = int(h)
                try:                                # remember for next launch
                    if RF != "?":
                        c = load_pid_cache()
                        e = c.get(key) or {}
                        if e.get("vheight") != h:
                            e["vheight"] = h
                            c[key] = e
                            save_pid_cache(c)
                except Exception:
                    pass
                if h >= 1080:
                    if deint_mode == "lowdeint" and not lowdeint_applied:
                        # first visit to a 1080i channel in lowdeint mode:
                        # decoder-open flags need one relaunch (cached after)
                        log("lowdeint: relaunching player for %s-line" % h)
                        try:
                            mpv_h["p"].kill()
                        except Exception:
                            pass
                        mpv_h["p"] = subprocess.Popen(cmd + LOWDEINT_FLAGS)
                    elif deint_mode == "auto":
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
        # CC track SELECTED (sid=1) so one 'v' press shows it instantly, but
        # HIDDEN by default (STVT_CC=off) — showing it at startup renders
        # glitched until toggled. Set STVT_CC=on to show it immediately.
        ipc(["set_property", "sid", 1])
        _cc_vis = os.environ.get("STVT_CC", "off").lower() not in (
            "off", "0", "no", "false")
        ipc(["set_property", "sub-visibility", "yes" if _cc_vis else "no"])
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
    ex_deaths, resyncs = 0, 0

    def rebuild_extractor(reason):
        """Tear the extractor down and re-run FULL discovery, then reload mpv.

        The in-loop respawn used to restart the extractor in the SAME mode with
        the SAME pids, forever. With a stale PID cache that is an unbounded
        respawn loop with no success condition (the exact shape the mpv-loop
        law forbids): cached-mode ffmpeg extracts PIDs the mux no longer
        carries, writes nothing, dies, respawns identically. Seen live
        2026-07-30 on RF36 — 'extractor died (mode=cached)' every ~63 s while
        the picture sat frozen. De-escalate instead: invalidate the cached
        layout, pay the full-discovery cost once, and reload the player the
        same way the hourly solo-rotation does.
        """
        nonlocal ex, last_pos, ex_deaths, resyncs, last_cc
        log(f"REBUILD extractor via full discovery ({reason})")
        try:
            cache.pop(key, None)
            save_pid_cache(cache)
        except Exception:
            pass
        if ex is not None:
            ex.kill()
        ex = Extractor(prog, mode="full", pids=None)
        ex.start()
        base = SOLO.stat().st_size if SOLO.exists() else 0
        grew = False
        for _ in range(90):                  # up to 45 s for real discovery
            time.sleep(0.5)
            if SOLO.exists() and SOLO.stat().st_size > base + 3_000_000:
                grew = True
                break
        if grew and ex.pids and RF != "?":
            # proven re-discovery -> remember the fresh layout, same as the
            # ladder's full-mode success (otherwise the next tune goes cold)
            ns = getattr(ex, "n_subs", 0)
            cut = len(ex.pids) - ns
            cache[key] = {"vpid": ex.pids[0],
                          "apids": ex.pids[1:cut],
                          "spids": ex.pids[cut:],
                          "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
            save_pid_cache(cache)
        ipc(["loadfile", str(SOLO), "replace"])
        time.sleep(2)
        seek_live_solo()
        last_pos = None
        # EIA-608 captions carry no keyframe state: any seek/reload can garble
        # the caption decoder, and the periodic flush is 8 MINUTES away. Seen
        # live 2026-07-30 ("CC was fine, then glitched after flipping around").
        # Pull the next flush to ~6 s from now instead of waiting out the timer.
        last_cc = time.time() - 474
        ex_deaths = resyncs = 0
    # CAPTION KEEPER (2026-07-21): captions ON by default now; keep the
    # eia_608 CC track selected + visible once the stream settles. The old
    # one-shot sid=1 broke whenever the CC track appeared late, sat at an id
    # other than 1, or got deselected on a channel change — so re-assert BY
    # CODEC every pass (idempotent). STVT_CC=off opts out; STVT_CC_DELAY sets
    # the settle wait that avoids mpv's brief startup-caption glitch.
    _cc_want = os.environ.get("STVT_CC", "on").lower() not in (
        "off", "0", "no", "false")
    _cc_delay = float(os.environ.get("STVT_CC_DELAY", "8"))
    t_start = time.time()
    # first CC cycle ~20 s in (flushes any startup caption garbage that
    # slipped through), then every 8 min as before
    last_cc = time.time() - 460
    while mpv_h["p"].poll() is None:
        time.sleep(5)
        if ex is not None and not ex.alive():
            ex_deaths += 1
            if ex_deaths >= 2:
                # twice in a row = this mode/layout is wrong, stop repeating it
                rebuild_extractor(f"{ex_deaths} consecutive deaths in "
                                  f"mode={ex.mode}")
            else:
                log(f"extractor died — restarting it (mode={ex.mode})")
                mode, pids = ex.mode, ex.pids
                ex.kill()
                ex = Extractor(prog, mode=mode, pids=pids); ex.start()
                time.sleep(3)
        # SOLO ROTATION (2026-07-13): live_solo.ts grows ~0.6 MB/s and never
        # truncated — 3.5 GB after 6 h uptime, which would eventually fill
        # the disk. Recycle it past a cap. The seek-live-edge fix means
        # playback rides the tail regardless of size, so the cap is LARGE
        # and this fires rarely (~hourly) — a ~2 s reload blip, not the
        # every-few-minutes churn a small cap would need.
        try:
            cap = int(os.environ.get("STVT_SOLO_CAP_MB", "1536")) * 1024 * 1024
            if ex is not None and SOLO.exists() and SOLO.stat().st_size > cap:
                log(f"live_solo rotate ({SOLO.stat().st_size//1048576} MB > cap)")
                mode, pids = ex.mode, ex.pids
                ex.kill()
                ex = Extractor(prog, mode=mode, pids=pids); ex.start()
                for _ in range(24):          # wait for the fresh file to fill
                    time.sleep(0.5)
                    if SOLO.exists() and SOLO.stat().st_size > 3_000_000:
                        break
                ipc(["loadfile", str(SOLO), "replace"])
                time.sleep(2)
                seek_live_solo()
                last_pos = None
                last_cc = time.time() - 474   # reload garbles 608 — flush soon
                continue
        except OSError:
            pass
        pos = ipc(["get_property", "time-pos"], req=5)
        eof = ipc(["get_property", "eof-reached"], req=5)
        paused = ipc(["get_property", "pause"], req=5)
        if not isinstance(pos, (int, float)):
            continue
        stalled = last_pos is not None and pos <= last_pos + 0.5
        last_pos = pos
        stall = stall + 1 if (eof or paused or stalled) else 0
        if stall == 0:
            ex_deaths = resyncs = 0          # playback advancing = healthy
        if stall >= 2:
            resyncs += 1
            # A resync is a nudge for a hiccup. Four in a row is not a hiccup:
            # the feed itself is dead (2026-07-30: an extractor writing nothing
            # kept mpv pinned at the same position through 17 straight resyncs
            # while the log said everything was being handled). Escalate — but
            # ONLY when an extractor exists. In mux fallback ex is None and mpv
            # is playing live.ts directly; firing the breaker there (seen live
            # 19:16:42 same night) spawns an extractor nobody asked for and
            # loadfile-yanks mpv off the mux onto a possibly-empty SOLO. A
            # dead mux feed has no player-side rebuild — keep nudging.
            if resyncs >= 4 and ex is not None:
                rebuild_extractor(f"{resyncs} consecutive resyncs — feed dead")
            else:
                log(f"RESYNC (stall={stall} eof={eof})")
                seek_live_solo()
                stall = 0
                last_cc = time.time() - 474   # seek garbles 608 — flush soon
        if _cc_want and time.time() - t_start > _cc_delay:
            tl = ipc(["get_property", "track-list"], req=6) or []
            cc = next((t for t in tl if t.get("type") == "sub"
                       and str(t.get("codec", "")).startswith("eia_608")),
                      None)
            if cc is not None:                    # broadcaster IS captioning
                if not cc.get("selected"):
                    ipc(["set_property", "sid", cc["id"]])
                if ipc(["get_property", "sub-visibility"],
                       req=6) not in (True, "yes"):
                    ipc(["set_property", "sub-visibility", "yes"])
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
