"""ambush3.py — the dawn hunt that brings home breakfast television.

v3 = v2 (guard-armed dwells, DFE, warm taps, sheriff) + THE TS ARCHIVER:
every dwell that crosses the watchability bar gets its transport stream
archived; at the end the best archive is remuxed video-only (the
marginal-viewing law: record-then-play, silent TV beats no TV), frame-
counted with ffprobe, and — if it holds real video — crowned
rf9_morning_show.ts with a voice announcement. Playback needs no
atmosphere: the 15.8+ peak is captured at 5 AM, watched at 7:30.

Lessons folded in: rabbit-only (discone blanked its dawn lottery),
announce throttling (43 voice events in one night = a cruel alarm
clock), guard default-on (no env), archives capped at best 2.

    python ambush3.py                     # production (until 06:45/07:10)
    python ambush3.py --test              # one 60 s dwell on RF34, full
                                          # archive->remux->verdict path
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import overnight_cube as oc

HERE = Path(__file__).parent
PY = sys.executable
FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
FFPROBE = r"C:\ffmpeg\bin\ffprobe.exe"
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
ARCHIVE = HERE / "lab" / "dawn_archive"
ARCHIVE.mkdir(parents=True, exist_ok=True)
CACHE = HERE / "tap_cache"
CACHE.mkdir(exist_ok=True)

os.environ.update({
    "STVT_EQ_TAP_CACHE": str(CACHE),
    "STVT_EQ_DFE": "1",
    "STVT_EQ_DFE_ANCHOR": "1",   # 7/07: halves bad pkts on canyon replay
    "STVT_EQ_DD_MU": "1e-2",     # 7/10: -31% bad on RF9 breather replay
    "STVT_EQ_RESEED": "1",
    "STVT_EQ_QUALITY_BAD_RMS": "8",
    "STVT_EQ_CMD_FILE": str(HERE / "eq_cmd.txt"),
    # SOVA (2026-07-07): trellis-doubt erasures — 174 rescues/75s where
    # the histogram managed 1-in-586k lifetime; cliff gauntlet win.
    "STVT_SOVA": "1",
    "STVT_RS_ERASURES": "14",
})

ARCHIVE_MER = 15.5        # archive dwells at/above this
ARCHIVE_HDR = 400
KEEP_BEST = 2


def log_event(obj):
    obj["t"] = datetime.now().strftime("%H:%M:%S")
    with open(HERE / "cube_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")
    print(obj, flush=True)


def now_hm():
    return datetime.now().strftime("%H:%M")


def archive_dwell(stats):
    """Copy the completed dwell's TS if it clears the bar; keep best 2."""
    mer = stats.get("mer_med") or 0
    hdr = stats.get("hdr") or 0
    if mer < ARCHIVE_MER or hdr < ARCHIVE_HDR or not LIVE.exists():
        return None
    stamp = datetime.now().strftime("%H%M%S")
    dst = ARCHIVE / f"rf9_{stamp}_mer{mer:.2f}_hdr{hdr}.ts"
    shutil.copyfile(LIVE, dst)
    log_event({"event": "dawn-archive", "file": dst.name,
               "mb": round(dst.stat().st_size / 1e6)})
    # retention: best KEEP_BEST by (mer, hdr) parsed from names
    def score(p):
        try:
            parts = p.stem.split("_")
            return (float(parts[2][3:]), int(parts[3][3:]))
        except (IndexError, ValueError):
            return (0.0, 0)
    allts = sorted(ARCHIVE.glob("rf9_*.ts"), key=score, reverse=True)
    for p in allts[KEEP_BEST:]:
        p.unlink()
    return dst


def produce_show():
    """Best archive -> video-only remux -> frame count -> verdict."""
    def score(p):
        try:
            parts = p.stem.split("_")
            return (float(parts[2][3:]), int(parts[3][3:]))
        except (IndexError, ValueError):
            return (0.0, 0)
    allts = sorted(ARCHIVE.glob("rf9_*.ts"), key=score, reverse=True)
    if not allts:
        log_event({"event": "morning-show", "verdict": "no archives"})
        return
    best = allts[0]
    show = ARCHIVE / "rf9_morning_show.ts"
    # remux main-program video; fall back to any video stream (test
    # muxes differ from WUSA's)
    for vmap in ("0:p:1:v:0", "0:v:0"):
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-fflags", "+genpts+discardcorrupt",
             "-err_detect", "ignore_err",
             "-analyzeduration", "20M", "-probesize", "20M",
             "-i", str(best), "-map", vmap, "-an", "-c", "copy",
             str(show)],
            capture_output=True, text=True)
        if show.exists() and show.stat().st_size > 1_000_000:
            break
    # honest gate: NULL-SINK DECODE frame count (the May law — ffprobe's
    # -count_frames hallucinates on ATSC muxes; only rendered frames count)
    frames = 0
    if show.exists() and show.stat().st_size > 1_000_000:
        pr = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-stats",
             "-fflags", "+genpts+discardcorrupt",
             "-err_detect", "ignore_err",
             "-i", str(show), "-f", "null", "-"],
            capture_output=True, text=True, timeout=600)
        import re as _re
        m = _re.findall(r"frame=\s*(\d+)", pr.stderr or "")
        frames = int(m[-1]) if m else 0
    watchable = frames >= 500
    log_event({"event": "morning-show", "source": best.name,
               "frames": frames,
               "verdict": "READY" if watchable else "below watchable"})
    if watchable:
        oc.announce(f"Channel 9 morning show is ready: "
                    f"{frames} frames recorded")
    return frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true",
                    help="one 60s dwell on RF34, full pipeline")
    args = ap.parse_args()

    if args.test:
        log_event({"event": "ambush3-test-start"})
        s = oc.sample(34, "Antenna B", 2, 32, secs=60)
        s["event"] = "rf9-ambush"          # reuse the machinery
        log_event(s)
        # force the bar for the test: RF34 healthy always clears MER,
        # hdr in 60s ~ 250-500; relax hdr bar via direct call
        global ARCHIVE_HDR
        ARCHIVE_HDR = 100
        dst = archive_dwell(s)
        print("archived:", dst)
        frames = produce_show()
        print("TEST", "PASSED — full pipeline works"
              if (frames or 0) >= 500 else
              f"pipeline ran; frames={frames} (RF34 should be >500 — review)")
        return

    sheriff = subprocess.Popen(
        [PY, "-u", str(HERE / "fec_sheriff.py"),
         "--log", str(HERE / "cube_chain.log"),
         "--cmd", str(HERE / "eq_cmd.txt"),
         "--mer", "14.0", "--badfrac", "0.6", "--cooldown", "20"])
    log_event({"event": "ambush3-start"})
    best = 0
    dwell_n = 0
    recent_zero = 0
    last_announce = 0.0
    AMBUSH_END, HARD_STOP = "06:45", "07:10"
    while True:
        if now_hm() >= HARD_STOP:
            break
        if now_hm() >= AMBUSH_END and recent_zero >= 2:
            break
        dwell_n += 1
        s = oc.sample(9, "Antenna B", 5, 32, secs=300)
        s["event"] = "rf9-ambush"
        s["ant"] = "philips"   # port B = Philips since 7/07
        log_event(s)
        hdr = s.get("hdr") or 0
        recent_zero = recent_zero + 1 if hdr == 0 else 0
        archive_dwell(s)
        # throttled announcements: first catch, new records, else hourly
        if hdr > 0 and (best == 0 or hdr > best
                        or time.time() - last_announce > 3600):
            oc.announce(f"Channel 9: {hdr} video headers")
            last_announce = time.time()
        if hdr > best:
            best = hdr
            if hdr >= 20:
                trig = Path(r"Z:\src\magic-tv-decoder\tools\data"
                            r"\specimens\TRIGGER")
                try:
                    trig.write_text(f"RF9-GOLDEN {hdr} headers")
                except OSError:
                    pass
    log_event({"event": "ambush-done", "best_hdr": best, "dwells": dwell_n})
    produce_show()
    try:
        sheriff.terminate()
    except Exception:
        pass
    env = os.environ.copy()
    env["PATH"] = r"C:\Program Files\SDRplay\API\x64" + os.pathsep + env["PATH"]
    subprocess.Popen([PY, "-u", str(HERE / "tv_tuna_panel.py")], env=env,
                     cwd=str(HERE))
    print("ambush3 complete — panel relaunched", flush=True)


if __name__ == "__main__":
    main()
