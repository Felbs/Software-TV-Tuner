"""Magic TV — single-file CLI for tuning, playing, recording, and streaming
ATSC 8-VSB TV via the project's SDRplay RSPdx + gr-atscplus pipeline.

Pipeline:

    [SDRplay RSPdx]
        v
    tv_live_rf34.py --rf <RF>     (subprocess; writes data/tv_live/live.ts)
        v
    Python tail thread            (188-byte aligned, 0x1FFF NULL heartbeat)
        v
    ffmpeg (libx264+aac, tee)
        |---> ffplay  (local play, optional)
        |---> mp4     (record, optional)
        |---> rtmp    (stream, optional)

Everything is stdlib + ffmpeg/ffplay subprocesses. No new pip installs.
This file is read-only with respect to the rest of the project — it
spawns tv_live_rf34.py and does not import or modify any decoder code.

See magic_tv_README.md for usage.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Make our colocated helper modules (atsc_psip.py, fcc_dc_stations.py)
# importable regardless of cwd or how this script was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Local stdlib-only PSIP parser for VCT + EIT (ATSC channel + show data)
try:
    from atsc_psip import extract_psip, find_current_event
except ImportError:
    extract_psip = None  # type: ignore
    find_current_event = None  # type: ignore

# ── Windows console UTF-8 (best-effort) ─────────────────────────
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── Paths / constants ────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
TV_LIVE_PY = HERE / "tv_live_rf34.py"
LIVE_TS = HERE / "data" / "tv_live" / "live.ts"
CONFIG_PATH = HERE / "magic_tv_config.json"
RECORD_DIR = HERE / "recordings"
SCAN_PATH = Path(os.path.expanduser("~")) / ".magic_tv" / "scan.json"

# tv_live needs the radioconda Python (for gr-atscplus + soapy). Override
# with $RADIOCONDA_PY if your install lives somewhere other than the
# default radioconda location under the user profile.
PYTHON_EXE = os.environ.get("RADIOCONDA_PY") or str(
    Path(os.path.expanduser("~")) / "radioconda" / "python.exe")
FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
FFPLAY = r"C:\ffmpeg\bin\ffplay.exe"
MAGIC_PLAYER = HERE / "magic_player.py"   # bundled with the repo
SDRPLAY_API_DIR = r"C:\Program Files\SDRplay\API\x64"

# A NULL transport-stream packet (PID 0x1FFF). VLC/ffmpeg ignore these
# but they keep the bytestream moving when live.ts has no fresh data,
# preventing the downstream demuxer from stalling. Pattern lifted from
# web/dashboard.py's tail_to_player code.
NULL_PKT = bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xFF" * 184
NULL_BURST = NULL_PKT * 32   # ~6 KB heartbeat

# Defaults / placeholders for first-run config
DEFAULT_CONFIG = {
    "destinations": {
        "twitch": "rtmp://live.twitch.tv/app/YOUR_STREAM_KEY",
        "youtube": "rtmp://a.rtmp.youtube.com/live2/YOUR_STREAM_KEY",
        "local_rtmp": "rtmp://localhost:1935/live/test",
    }
}

# Subprocess creation flags (Windows). On non-Windows we just use 0.
NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


# ── Channel table (read-only import) ─────────────────────────────
def _load_stations():
    """Import fcc_dc_stations dynamically so this script can still run
    `--help` / `--list` even if that module has a syntax problem."""
    sys.path.insert(0, str(HERE))
    try:
        from fcc_dc_stations import DC_DMA_STATIONS, lookup  # type: ignore
    except Exception as e:
        print(f"[magic_tv] WARNING: failed to import fcc_dc_stations: {e}",
              file=sys.stderr)
        return [], (lambda rf: None)
    return DC_DMA_STATIONS, lookup


# ── Config helpers ───────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # Ensure structure
        if "destinations" not in cfg or not isinstance(cfg["destinations"], dict):
            cfg["destinations"] = dict(DEFAULT_CONFIG["destinations"])
        return cfg
    except Exception as e:
        print(f"[magic_tv] config parse failed ({e}); using defaults",
              file=sys.stderr)
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ── Channel listing ──────────────────────────────────────────────
BANNER = r"""
              .  *   *   .   *      .   *   .
                  *  .   . *  .  *      *
            *   .─────────────────────.   .  *
              .|   ✦   M A G I C   ✦  |
            *  |  ┌───────────────┐   |  *  .
              .|  │░░░░░░░░░░░░░░░│   |
            .  |  │   T  V  ◉ ◉  │   | .  ✦
            *  |  │░░░░░░░░░░░░░░░│   |
              .|  └─┬───────────┬─┘   |   *
            .  |    │           │     | .
              .|═════════════════════════| ✦  *
                  .  ╲ * .   . *  ╱  .
                       ╲  *  .  ╱  .
                        ╲ ✦  ╱   *   .
                       ──┴───┴──    .  *
"""


def expand_channels() -> list[dict]:
    """Return a flat list of (rf, virtual, callsign, label) entries —
    one row per virtual channel including sub-channels — sorted by RF."""
    stations, _ = _load_stations()
    rows: list[dict] = []
    for row in stations:
        rf, virt, call, net, city, subs = row
        # Main channel (program 1)
        rows.append({
            "rf": rf, "virtual": virt, "callsign": call,
            "network": net, "city": city, "program": 1,
            "is_sub": False,
            "label": f"{call} {net}",
        })
        # Sub-channels (program 2, 3, ... in order found in PMT)
        for prog_idx, (sub_virt, sub_name) in enumerate(subs, start=2):
            rows.append({
                "rf": rf, "virtual": sub_virt, "callsign": call,
                "network": sub_name, "city": city, "program": prog_idx,
                "is_sub": True,
                "label": f"{call} {sub_name}",
            })
    return sorted(rows, key=lambda r: (r["rf"], r["virtual"]))


def print_channel_list() -> list[dict]:
    rows = expand_channels()
    if not rows:
        print("(no stations available — fcc_dc_stations.py not importable)")
        return []
    print()
    print(f"{'#':>3}  {'RF':>3}  {'Virtual':<7}  {'Callsign':<8}  "
          f"{'Sub/Network':<14}  City")
    print("-" * 72)
    last_rf = None
    for i, r in enumerate(rows, start=1):
        if last_rf is not None and r["rf"] != last_rf:
            # Faint separator between RF groups so the multiplex grouping
            # (one frequency, multiple sub-channels) is visible.
            print(f"     {'─' * 65}")
        # Indent sub-channels with arrow; mains are left-aligned and bold-marker.
        marker = "  └▸" if r["is_sub"] else " ★  "
        print(f"{i:>3} {marker} {r['rf']:>3}  {r['virtual']:<7}  "
              f"{r['callsign']:<8}  {r['network']:<14}  {r['city']}")
        last_rf = r["rf"]
    print()
    print("  ★  = main channel (no sub-channels above it on same RF)")
    print("  └▸ = sub-channel (shares same RF/multiplex as ★ above)")
    print()
    return rows


# ── Pipeline plumbing ────────────────────────────────────────────
def env_with_sdrplay() -> dict:
    """Return a copy of os.environ with the SDRplay API DLL dir on PATH."""
    env = os.environ.copy()
    path = env.get("PATH", "")
    if SDRPLAY_API_DIR.lower() not in path.lower():
        env["PATH"] = SDRPLAY_API_DIR + os.pathsep + path
    return env


def spawn_tv_live(rf: int, log_fh, viterbi: str = "stock") -> subprocess.Popen:
    """Start the SDR pipeline subprocess. Returns Popen handle.

    `viterbi='soft'` switches to tv_live_rf34_softvit.py which uses the
    fork's atsc_viterbi_soft block (Tier 9 fix). Default 'stock' is the
    workhorse hard-decision Viterbi everything has been tested on.
    """
    script = (HERE / "tv_live_rf34_softvit.py") if viterbi == "soft" else TV_LIVE_PY
    if not script.exists():
        raise FileNotFoundError(f"tv_live script not found at {script}")
    if not Path(PYTHON_EXE).exists():
        raise FileNotFoundError(
            f"radioconda python not found at {PYTHON_EXE}. "
            "Install radioconda or update PYTHON_EXE in magic_tv.py.")

    return subprocess.Popen(
        [PYTHON_EXE, "-u", str(script), "--rf", str(rf)],
        cwd=str(HERE),
        env=env_with_sdrplay(),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        creationflags=NEW_PROCESS_GROUP,
    )


def wait_for_live_ts(timeout_sec: float = 30.0) -> bool:
    """Poll until live.ts exists and is growing. Returns True iff growing.

    Logic: every 0.5s, sample size; success when we observe two distinct
    sizes >= 1 MB.
    """
    deadline = time.time() + timeout_sec
    last_size = -1
    saw_two = False
    while time.time() < deadline:
        try:
            sz = LIVE_TS.stat().st_size
        except FileNotFoundError:
            sz = 0
        if sz > 1_000_000 and sz != last_size and last_size >= 0:
            saw_two = True
            return True
        last_size = sz
        time.sleep(0.5)
    return saw_two


def probe_program_id(program_index: int = 1, timeout_sec: float = 8.0) -> int | None:
    """ffprobe live.ts to discover ATSC program IDs in the multiplex,
    return the program ID at the given 1-based index (1=main, 2=first
    sub, etc). Returns None if probe fails — caller falls back to no
    -map filter. ATSC program IDs are often the virtual major channel
    number (e.g. 5 for Fox 5), not 1/2/3, so we can't guess."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            sz = LIVE_TS.stat().st_size
        except OSError:
            sz = 0
        if sz >= 5_000_000:
            break
        time.sleep(0.5)
    try:
        result = subprocess.run(
            [r"C:\ffmpeg\bin\ffprobe.exe",
             "-v", "error", "-of", "json", "-show_programs",
             "-analyzeduration", "5000000", "-probesize", "5000000",
             "-f", "mpegts", "-i", str(LIVE_TS)],
            capture_output=True, timeout=15, text=True,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        progs = data.get("programs", [])
        if not progs:
            return None
        idx = max(0, program_index - 1)
        if idx >= len(progs):
            idx = 0
        return progs[idx].get("program_id")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def measure_convergence(min_size: int = 5_000_000) -> int:
    """Read the most-recent ~5MB of live.ts and return PAT packet count.

    The atscplus long equalizer converges probabilistically (~1 in 3
    cold-starts succeed). On a converged stream PAT count in 5MB is
    typically >5. On a failed convergence it's 0–1. Used by the
    startup watchdog to decide whether to keep this tv_live or kill it.
    """
    try:
        size = LIVE_TS.stat().st_size
    except FileNotFoundError:
        return 0
    read = min(min_size, size)
    if read < 188 * 100:
        return 0
    with open(LIVE_TS, "rb") as f:
        f.seek(size - read)
        buf = f.read(read)
    # Find packet alignment
    start = 0
    for i in range(min(len(buf) - 188 - 1, 10000)):
        if buf[i] == 0x47 and buf[i + 188] == 0x47:
            start = i
            break
    buf = buf[start:]
    n = len(buf) // 188
    pat_count = 0
    for i in range(n):
        pkt = buf[i * 188 : (i + 1) * 188]
        if pkt[0] != 0x47:
            continue
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid == 0x0000:
            pat_count += 1
    return pat_count


# ── Channel scanner ──────────────────────────────────────────────
# RF channels worth probing in a typical North American market post-repack.
# VHF-Hi (7-13) and UHF (14-36). VHF-Lo (2-6) is mostly empty post-2009.
# Scanning all 36 takes ~6 minutes wall clock at 12s dwell.
SCAN_RF_RANGE = list(range(7, 14)) + list(range(14, 37))


def kill_proc(proc, label: str = "proc"):
    """Best-effort terminate a subprocess on Windows."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def ffprobe_programs(timeout_sec: float = 12.0) -> list[dict]:
    """ffprobe live.ts for all programs in the multiplex. Returns list of
    dicts with program_id, program_num, service_name, video_codec,
    video_height, audio_codec. Empty list if probe fails."""
    try:
        result = subprocess.run(
            [r"C:\ffmpeg\bin\ffprobe.exe",
             "-v", "error", "-of", "json",
             "-show_programs", "-show_streams",
             "-analyzeduration", "5000000", "-probesize", "5000000",
             "-f", "mpegts", "-i", str(LIVE_TS)],
            capture_output=True, timeout=timeout_sec, text=True,
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return []
    progs = []
    for p in data.get("programs", []):
        info = {
            "program_id": p.get("program_id"),
            "program_num": p.get("program_num"),
            "service_name": (p.get("tags") or {}).get("service_name"),
        }
        for s in p.get("streams", []) or []:
            ct = s.get("codec_type")
            if ct == "video":
                info["video_codec"] = s.get("codec_name")
                info["video_height"] = s.get("height")
            elif ct == "audio":
                info["audio_codec"] = s.get("codec_name")
        progs.append(info)
    return progs


def scan_one_rf(rf: int, dwell_sec: float = 12.0,
                 viterbi: str = "stock",
                 log_fh=None) -> dict:
    """Tune one RF, wait for convergence, probe programs. Returns a result
    dict. Always cleans up its tv_live process before returning."""
    # Clean live.ts so convergence/probe see only this RF's bytes.
    try:
        LIVE_TS.unlink()
    except (FileNotFoundError, PermissionError, OSError):
        pass

    fh = log_fh if log_fh is not None else subprocess.DEVNULL
    proc = None
    try:
        proc = spawn_tv_live(rf, fh, viterbi=viterbi)
    except Exception as e:
        return {"rf": rf, "lock": False, "reason": f"spawn failed: {e}"}

    try:
        if not wait_for_live_ts(timeout_sec=15.0):
            return {"rf": rf, "lock": False, "reason": "no live.ts growth"}
        # Wait for the equalizer to converge.
        deadline = time.time() + dwell_sec
        while time.time() < deadline:
            if proc.poll() is not None:
                return {"rf": rf, "lock": False, "reason": "tv_live died"}
            time.sleep(0.5)
        pat = measure_convergence()
        if pat < 5:
            return {"rf": rf, "lock": False, "pat_count": pat,
                    "reason": "weak signal / no lock"}
        progs = ffprobe_programs()
        result = {
            "rf": rf,
            "freq_mhz": rf_to_freq_mhz(rf),
            "lock": True,
            "pat_count": pat,
            "programs": progs,
        }
        # ATSC PSIP: virtual-channel labels + the next ~12 hours of EIT
        # show titles, decoded directly from the captured TS via our
        # stdlib-only parser. Events are stored with GPS start_time so
        # the picker can compute "what's on now" against the current
        # wall clock at display time.
        if extract_psip is not None:
            try:
                psip = extract_psip(LIVE_TS)
                if psip.get("channels") or psip.get("events"):
                    # JSON keys must be strings — convert source_id ints.
                    result["psip"] = {
                        "channels": psip["channels"],
                        "events": {str(k): v for k, v in
                                   psip["events"].items()},
                    }
            except Exception as e:
                print(f"[scan]   psip parse failed: {e}", file=sys.stderr)
        return result
    finally:
        kill_proc(proc, "scan_tv_live")
        # SDRplay driver needs ~3 s to fully release the device.
        time.sleep(3)


def rf_to_freq_mhz(rf: int) -> float:
    """North American ATSC RF→MHz table (post-2009 repack lower-edge)."""
    if 2 <= rf <= 4:
        return 57.0 + (rf - 2) * 6.0
    if 5 <= rf <= 6:
        return 79.0 + (rf - 5) * 6.0
    if 7 <= rf <= 13:
        return 177.0 + (rf - 7) * 6.0
    if 14 <= rf <= 36:
        return 473.0 + (rf - 14) * 6.0
    return 0.0


def lookup_callsign(rf: int) -> tuple[str, str] | None:
    """Best-effort callsign + network from the bundled DC stations table.
    Returns None if RF isn't in the table — scan still works, you just
    won't get a friendly label."""
    _stations, lookup = _load_stations()
    info = lookup(rf)
    if info is None:
        return None
    return info.get("callsign", ""), info.get("network", "")


def run_scan(rf_list: list[int] | None = None,
             dwell_sec: float = 12.0,
             save: bool = True) -> dict:
    """Sweep RF channels, return scan result + optionally persist to
    SCAN_PATH. Prints a one-line update per channel."""
    if rf_list is None:
        rf_list = SCAN_RF_RANGE
    results = []
    print(f"[scan] sweeping {len(rf_list)} RF channels "
          f"(~{int(len(rf_list) * (dwell_sec + 5))}s)...")
    log_dir = HERE / "data" / "tv_live"
    log_dir.mkdir(parents=True, exist_ok=True)
    scan_log = log_dir / "magic_tv.scan.log"
    with open(scan_log, "a", encoding="utf-8") as log_fh:
        for rf in rf_list:
            freq = rf_to_freq_mhz(rf)
            print(f"  RF {rf:>2} ({freq:5.1f} MHz) … ", end="", flush=True)
            res = scan_one_rf(rf, dwell_sec=dwell_sec, log_fh=log_fh)
            if res.get("lock"):
                cs = lookup_callsign(rf)
                if cs:
                    res["callsign"], res["network_hint"] = cs
                n_progs = len(res.get("programs") or [])
                pat = res.get("pat_count", 0)
                label = res.get("callsign") or "?"
                print(f"LOCKED  {label:<6} {n_progs} programs, "
                      f"PAT={pat}")
            else:
                why = res.get("reason", "no lock")
                print(f"{why}")
            results.append(res)

    scan = {
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "channels": results,
    }
    n_lock = sum(1 for r in results if r.get("lock"))
    print(f"[scan] done — {n_lock} of {len(results)} channels locked.")
    if save:
        SCAN_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCAN_PATH.write_text(json.dumps(scan, indent=2), encoding="utf-8")
        print(f"[scan] saved to {SCAN_PATH}")
    return scan


def load_scan() -> dict | None:
    if not SCAN_PATH.exists():
        return None
    try:
        return json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def build_ffmpeg_cmd(play: bool, record_path: Path | None,
                     stream_url: str | None,
                     program: int = 1) -> list[str]:
    """Build the central ffmpeg command line.

    Always re-encodes — passthrough exposes raw 1080i interlace combing
    and unconcealed mpeg2 macroblock breakage from TEI scrubs.
      * Local play: high-quality h264_nvenc (cq 19, preset p5) + yadif
        deinterlace + error concealment. Visually transparent re-encode.
      * Record / stream: same encoder settings, fanned through tee.
    """
    cmd = [
        FFMPEG,
        "-hide_banner", "-loglevel", "warning",
        # Input: maximally tolerant TS demux. discardcorrupt drops bad
        # packets, output_corrupt keeps the partially-decoded frames the
        # decoder DOES produce (rather than freezing waiting for clean
        # ones). genpts regenerates timing.
        "-fflags", "+genpts+igndts+discardcorrupt+nobuffer",
        "-err_detect", "ignore_err",
        "-flags", "+output_corrupt",
        # Decoder-side error concealment: when frames have missing
        # macroblocks, guess motion vectors from neighbors, deblock, and
        # favor inter-prediction. Smooths the visible breakage from TEI
        # scrubs that the FS-validator can't catch.
        "-ec", "favor_inter+deblock+guess_mvs",
        "-analyzeduration", "2000000",
        "-probesize", "3000000",
        "-thread_queue_size", "4096",
        "-f", "mpegts",
        "-i", "pipe:0",
        # Select the requested program (PMT). ATSC TS multiplexes subchannels
        # — RF34 carries 4.1/4.2/4.3/4.4. Default program=1 → main channel
        # (e.g. 4.1 NBC). `program` arg is the 1-based subchannel number.
        "-map", f"0:p:{program}:v", "-map", f"0:p:{program}:a?",
        # yadif mode=1 ("bob") emits one frame per input *field* — preserves
        # the full 60-field-per-second temporal smoothness of 1080i. mode=0
        # would halve temporal info to 30 fps. No-op on 720p60 progressive.
        "-vf", "yadif=mode=1:parity=auto:deint=interlaced",
        # NVENC: live-streaming tune (ll, not hq — hq wants two-pass which
        # breaks on a stdin pipe). p7 gives best quality at the cost of
        # GPU time; lookahead helps allocate bitrate in motion. No
        # maxrate cap so bursts during high-motion scenes get the headroom
        # they need.
        "-c:v", "h264_nvenc", "-preset", "p7", "-tune", "ll",
        "-rc", "vbr", "-cq", "19", "-b:v", "0",
        "-rc-lookahead", "32",
        "-bf", "3", "-spatial-aq", "1", "-temporal-aq", "1",
        "-pix_fmt", "yuv420p",
        # Shorter GOP so ffplay can resync after corruption within ~1s
        # instead of waiting up to 2s for the next keyframe.
        "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        # AC3 surround → AAC stereo (ffplay's default audio device is
        # almost always 2-channel; 5.1 AC3 passthrough usually plays as
        # silence on stereo outputs).
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
    ]

    sinks = []
    if record_path is not None:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        # tee's mp4 output needs a faststart-friendly muxer; we pass
        # movflags via the tee per-output options.
        sinks.append(("mp4", str(record_path),
                      "movflags=+faststart+frag_keyframe+empty_moov"))
    if stream_url:
        sinks.append(("flv", stream_url, ""))
    if play:
        sinks.append(("mpegts", "pipe:1", ""))

    if not sinks:
        # Nothing to do — caller should have caught this, but be safe.
        cmd += ["-f", "null", "-"]
        return cmd

    if len(sinks) == 1:
        fmt, target, opts = sinks[0]
        if opts:
            # For single-output we put movflags via -movflags etc.
            if fmt == "mp4":
                cmd += ["-movflags", "+faststart+frag_keyframe+empty_moov"]
        cmd += ["-f", fmt, target]
        return cmd

    # tee muxer: each branch gets [opt=val:opt=val:f=fmt]target
    parts = []
    for fmt, target, opts in sinks:
        head = []
        if opts:
            head.append(opts)
        head.append(f"f={fmt}")
        # tee uses '|' as separator and ':' inside [].  Escape any '|'
        # in the target by replacing with '\|' (rare in URLs/paths).
        safe_target = target.replace("|", "\\|")
        parts.append(f"[{':'.join(head)}]{safe_target}")
    tee_arg = "|".join(parts)
    cmd += ["-f", "tee", tee_arg]
    return cmd


def spawn_ffmpeg(cmd: list[str], log_fh, want_stdout_pipe: bool):
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=(subprocess.PIPE if want_stdout_pipe else log_fh),
        stderr=log_fh,
        creationflags=NEW_PROCESS_GROUP,
    )


def spawn_ffplay(window_title: str, log_fh):
    return subprocess.Popen(
        [FFPLAY,
         "-hide_banner", "-loglevel", "warning",
         "-window_title", window_title,
         "-fflags", "+genpts+igndts+discardcorrupt",
         "-err_detect", "ignore_err",
         "-analyzeduration", "3000000",
         "-probesize", "3000000",
         "-f", "mpegts",
         "-i", "pipe:0",
         "-sync", "audio",
         "-framedrop",
         "-infbuf"],
        stdin=subprocess.PIPE,
        stdout=log_fh,
        stderr=log_fh,
        creationflags=NEW_PROCESS_GROUP,
    )


# ── Tail thread: live.ts → ffmpeg stdin ──────────────────────────
class TailWorker:
    """Reads the growing live.ts file and writes to a target stdin.

    Behaviour mirrors web/dashboard.py:tail_to_player —
      * Seek 1 MB before EOF on open
      * Re-align to a 188-byte sync boundary (look for 0x47 ... 0x47+188)
      * Emit a NULL-packet heartbeat when no fresh bytes are available
      * Stop when stop_event is set or target stdin breaks

    The .target stdin can be hot-swapped via swap_target() when the
    downstream ffmpeg is recovered after a freeze. The tail thread
    will pick up the new target on the next chunk.
    """

    def __init__(self, target_stdin, stop_event: threading.Event):
        self.target = target_stdin
        self.shared_stop = stop_event   # global pipeline shutdown
        self.local_stop = threading.Event()  # per-instance stop
        self.bytes_forwarded = 0
        self._target_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)

    @property
    def stop(self):
        # Tail considers itself stopped when EITHER signal is set.
        class _OrEvent:
            def __init__(self, a, b):
                self._a, self._b = a, b
            def is_set(self):
                return self._a.is_set() or self._b.is_set()
        return _OrEvent(self.shared_stop, self.local_stop)

    def stop_local(self):
        """Stop just this tail (e.g. for decoder restart) without
        shutting down the rest of the pipeline."""
        self.local_stop.set()

    def swap_target(self, new_target_stdin) -> None:
        """Atomically replace the write target (called during ffmpeg recovery)."""
        with self._target_lock:
            old = self.target
            self.target = new_target_stdin
        # Best-effort close old; ignore errors
        try: old.close()
        except (OSError, ValueError, AttributeError): pass

    def _wait_for_swap(self, old_target, timeout_sec: float) -> bool:
        """Wait until self.target differs from old_target (a swap happened).
        Returns True if swap detected within timeout, False otherwise.
        """
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if self.stop.is_set():
                return False
            with self._target_lock:
                if self.target is not old_target:
                    return True
            time.sleep(0.1)
        return False

    def start(self):
        self.thread.start()

    def _run(self):
        # Wait up to 60s for live.ts to be substantial enough to start.
        deadline = time.time() + 60
        while time.time() < deadline and not self.stop.is_set():
            try:
                if LIVE_TS.exists() and LIVE_TS.stat().st_size > 500_000:
                    break
            except OSError:
                pass
            time.sleep(0.25)
        if self.stop.is_set():
            return

        try:
            f = open(LIVE_TS, "rb")
        except OSError as e:
            print(f"[magic_tv] cannot open live.ts: {e}", file=sys.stderr)
            return

        try:
            f.seek(0, 2)
            cur = f.tell()
            f.seek(max(0, cur - 1_000_000))   # ~0.5 sec of recent bytes
            head = f.read(2048)
            offset = 0
            for i in range(len(head) - 188 - 1):
                if head[i] == 0x47 and head[i + 188] == 0x47:
                    offset = i
                    break
            f.seek(f.tell() - len(head) + offset)
        except OSError:
            pass

        leftover = b""
        idle = 0
        try:
            while not self.stop.is_set():
                chunk = b""
                try:
                    chunk = f.read(64 * 1024)
                except OSError:
                    break
                # Snapshot current target under lock — supports hot-swap.
                with self._target_lock:
                    target = self.target
                if not chunk:
                    # Detect file truncation — tv_live restart reopens
                    # live.ts with mode='wb' which truncates. If file is
                    # smaller than our position, reopen and re-align so we
                    # follow the fresh stream without disturbing ffmpeg.
                    try:
                        cur_pos = f.tell()
                        cur_size = LIVE_TS.stat().st_size
                        if cur_size < cur_pos:
                            try: f.close()
                            except OSError: pass
                            time.sleep(0.5)  # wait for tv_live to write a bit
                            f = open(LIVE_TS, "rb")
                            f.seek(0, 2)
                            new_end = f.tell()
                            f.seek(max(0, new_end - 1_000_000))
                            head = f.read(2048)
                            for i in range(len(head) - 188 - 1):
                                if head[i] == 0x47 and head[i + 188] == 0x47:
                                    f.seek(f.tell() - len(head) + i)
                                    break
                            leftover = b""
                            idle = 0
                            print("[tail] live.ts truncated — reopened, "
                                  "ffmpeg keeps reading", flush=True)
                            continue
                    except OSError:
                        pass
                    idle += 1
                    try:
                        target.write(NULL_BURST)
                        target.flush()
                    except (OSError, BrokenPipeError, ValueError):
                        # Target broke. Wait briefly for a possible
                        # swap_target() from the recovery callback. If
                        # swap doesn't arrive within ~10s, bail.
                        if not self._wait_for_swap(target, 10.0):
                            break
                        time.sleep(0.05)
                        continue
                    if idle > 1200:   # ~60s of nothing → bail
                        break
                    time.sleep(0.05)
                    continue
                idle = 0
                buf = leftover + chunk
                aligned = (len(buf) // 188) * 188
                if aligned:
                    try:
                        target.write(buf[:aligned])
                        target.flush()
                    except (OSError, BrokenPipeError, ValueError):
                        if not self._wait_for_swap(target, 10.0):
                            break
                        # Else: swapped — retry the write into the new
                        # target on the next iteration. We discard the
                        # buffered chunk because the new ffmpeg started
                        # mid-stream and needs to re-align on TS sync
                        # bytes; partial in-flight bytes would just
                        # confuse its demuxer.
                        leftover = b""
                        continue
                    self.bytes_forwarded += aligned
                leftover = buf[aligned:]
        finally:
            try: f.close()
            except OSError: pass
            with self._target_lock:
                t = self.target
            try: t.close()
            except (OSError, ValueError, AttributeError): pass


# ── Relay: ffmpeg stdout → ffplay stdin ──────────────────────────
def spawn_relay(src, dst, stop_event: threading.Event,
                tag: str = "relay") -> threading.Thread:
    def run():
        try:
            while not stop_event.is_set():
                data = src.read(64 * 1024)
                if not data:
                    break
                try:
                    dst.write(data)
                    dst.flush()
                except (OSError, BrokenPipeError, ValueError):
                    break
        except OSError:
            pass
        finally:
            try: dst.close()
            except OSError: pass
    t = threading.Thread(target=run, daemon=True, name=tag)
    t.start()
    return t


# ── Cleanup helpers ──────────────────────────────────────────────
def kill_proc(p: subprocess.Popen | None, name: str = "") -> None:
    if p is None:
        return
    try:
        if p.poll() is None:
            try:
                if os.name == "nt":
                    p.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    p.terminate()
            except (OSError, ValueError):
                pass
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try: p.kill()
                except OSError: pass
                try: p.wait(timeout=2)
                except Exception: pass
    except Exception as e:
        print(f"[magic_tv] error killing {name}: {e}", file=sys.stderr)


# ── Interactive prompts ──────────────────────────────────────────
def prompt_yn(msg: str, default: bool = False) -> bool:
    suffix = " (Y/n): " if default else " (y/N): "
    while True:
        try:
            ans = input(msg + suffix).strip().lower()
        except EOFError:
            return default
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  please answer y or n")


def prompt_choice(msg: str, options: list[str], default: int = 1) -> int:
    while True:
        for i, o in enumerate(options, start=1):
            marker = "*" if i == default else " "
            print(f"  {marker} {i}) {o}")
        try:
            ans = input(f"{msg} [{default}]: ").strip()
        except EOFError:
            return default
        if not ans:
            return default
        try:
            n = int(ans)
            if 1 <= n <= len(options):
                return n
        except ValueError:
            pass
        print("  invalid choice")


def expand_channels_from_scan(scan: dict) -> list[dict]:
    """Build the picker rows from scan.json.

    Prefers ATSC PSIP data (broadcast-authoritative virtual channels +
    EIT show schedule) when available. Falls back to ffprobe program
    info + the static fcc_dc_stations table for unmapped channels.

    Each row is the picker entry for one (rf, program_number) tuple.
    """
    _stations, lookup = _load_stations()
    rows = []
    for ch in scan.get("channels", []):
        if not ch.get("lock"):
            continue
        rf = ch["rf"]
        info = lookup(rf)
        progs = ch.get("programs") or []
        psip = ch.get("psip") or {}
        psip_channels = psip.get("channels") or []
        psip_events = psip.get("events") or {}
        # Index PSIP virtual-channel records by program_number for
        # quick lookup against ffprobe's per-stream program_num.
        psip_by_prognum = {c.get("program_number"): c for c in psip_channels}

        for sub_idx, p in enumerate(progs, start=1):
            program_id = p.get("program_id")
            program_num = p.get("program_num")
            video_h = p.get("video_height")
            video_codec = p.get("video_codec")
            audio_codec = p.get("audio_codec")
            service_name = p.get("service_name") or ""

            virt = ""
            callsign = ""
            network = service_name
            now_title = ""
            now_remaining = 0

            psip_ch = psip_by_prognum.get(program_num)
            if psip_ch is not None:
                # Authoritative virtual channel from broadcaster's VCT.
                virt = f"{psip_ch['major']}.{psip_ch['minor']}"
                callsign = psip_ch.get("short_name", "") or ""
                evs = psip_events.get(str(psip_ch.get("source_id"))) or []
                if find_current_event is not None:
                    cur = find_current_event(evs)
                    if cur is not None:
                        now_title = cur.get("title") or ""
                        now_remaining = cur.get("remaining_sec") or 0

            if not virt and info is not None:
                # Fall back to static fcc_dc_stations table.
                callsign = callsign or info.get("callsign", "") or ""
                if sub_idx == 1:
                    virt = info.get("virtual", "") or ""
                    network = info.get("network", "") or network
                else:
                    subs = info.get("subs", []) or []
                    if sub_idx - 2 < len(subs):
                        virt, network = subs[sub_idx - 2]
            if not virt:
                virt = f"{rf}.{program_num or sub_idx}"
            if not callsign:
                callsign = f"RF{rf}"

            quality_bits = []
            if video_h:
                quality_bits.append(f"{video_h}p" if video_h <= 720 else
                                     f"{video_h}i")
            if video_codec:
                quality_bits.append(video_codec)
            if audio_codec:
                quality_bits.append(audio_codec)
            quality = " ".join(quality_bits)

            rows.append({
                "rf": rf,
                "virtual": virt,
                "callsign": callsign,
                "network": network or "",
                "city": "",
                "program": program_id or sub_idx,
                "is_sub": (sub_idx > 1),
                "label": f"{callsign} {network}".strip(),
                "quality": quality,
                "service_name": service_name,
                "now_title": now_title,
                "now_remaining_sec": now_remaining,
            })
    # Sort by virtual major.minor when available so 4.1, 4.2, 4.3, 4.4
    # group naturally above 5.1, 7.1, etc.
    def sort_key(r):
        try:
            major, minor = r["virtual"].split(".")
            return (int(major), int(minor))
        except (ValueError, AttributeError):
            return (r["rf"], r["program"])
    return sorted(rows, key=sort_key)


def print_scan_table(scan: dict) -> list[dict]:
    """Pretty-print the scanned channel table grouped by RF major channel,
    return picker rows so caller can prompt by index. When the scan
    captured ATSC EIT data, the currently-airing show appears next to
    each entry (computed against current wall clock)."""
    rows = expand_channels_from_scan(scan)
    if not rows:
        print("(scan.json contains no locked channels — try --scan again)")
        return []
    has_now = any(r.get("now_title") for r in rows)
    print()
    if has_now:
        print(f"  {'#':>3}  {'Ch':<5}  {'Callsign':<8}  "
              f"{'Quality':<14}  Now playing")
    else:
        print(f"  {'#':>3}  {'Ch':<5}  {'Callsign':<8}  "
              f"{'Network':<14}  Quality")
    print("  " + "-" * 70)
    last_major = None
    for i, r in enumerate(rows, start=1):
        major = r["virtual"].split(".")[0] if "." in r["virtual"] else None
        if last_major is not None and major != last_major:
            print(f"       {'─' * 65}")
        marker = "└▸" if r["is_sub"] else "★ "
        q = r.get("quality", "") or ""
        if has_now:
            now = r.get("now_title") or ""
            rem = r.get("now_remaining_sec") or 0
            now_str = (f"{now[:38]}{'…' if len(now) > 38 else ''}"
                       f" ({rem // 60}m)") if now else ""
            print(f"  {i:>3}  {marker} {r['virtual']:<3}  "
                  f"{r['callsign']:<8}  {q:<14}  {now_str}")
        else:
            print(f"  {i:>3}  {marker} {r['virtual']:<3}  "
                  f"{r['callsign']:<8}  {r['network']:<14}  {q}")
        last_major = major
    print()
    return rows


def maybe_first_run_scan(cfg: dict) -> dict | None:
    """If no scan.json exists, offer to run a scan now. Returns the
    scan dict if one is available (either pre-existing or freshly run),
    None if the user declines and there's nothing on disk."""
    scan = load_scan()
    if scan is not None:
        return scan
    print()
    print("  No channel scan found at", SCAN_PATH)
    print("  A scan tunes each RF channel for ~12 seconds and saves which")
    print("  ones receive at your antenna. It takes about 6 minutes total.")
    print()
    if not prompt_yn("Run a scan now?", default=True):
        return None
    return run_scan()


def interactive_pick(cfg: dict):
    """Returns dict {rf, program, callsign, play, stream_url, record_path}."""
    print(BANNER)
    scan = maybe_first_run_scan(cfg)
    if scan is not None:
        rows = print_scan_table(scan)
        # Offer re-scan at the picker too.
        print("  r) Re-scan       q) Quit")
    else:
        rows = print_channel_list()
    if not rows:
        # Fall back to the static table if a fresh scan produced nothing.
        rows = print_channel_list()
    if not rows:
        raise SystemExit("no channels available")

    while True:
        try:
            ans = input(f"Pick channel # [1-{len(rows)}], r=re-scan, q=quit: ").strip().lower()
        except EOFError:
            raise SystemExit("no input")
        if ans in ("q", "quit", "exit"):
            raise SystemExit(0)
        if ans in ("r", "rescan", "re-scan"):
            new_scan = run_scan()
            rows = print_scan_table(new_scan) or rows
            print("  r) Re-scan       q) Quit")
            continue
        try:
            n = int(ans)
            if 1 <= n <= len(rows):
                break
        except ValueError:
            pass
        print("  invalid")

    r = rows[n - 1]
    rf, virt, call, net, city, prog = (
        r["rf"], r["virtual"], r["callsign"],
        r["network"], r["city"], r["program"])
    print(f"Selected: RF{rf} {virt} {call} ({net}) program {prog} — {city}")

    play = prompt_yn("Play locally?", default=True)

    stream_url = None
    if prompt_yn("Stream to a destination?", default=False):
        dests = cfg.get("destinations", {})
        if not dests:
            print("  no destinations configured (use --config-set NAME URL)")
        else:
            names = list(dests.keys()) + ["off"]
            idx = prompt_choice("Destination", names, default=len(names))
            chosen = names[idx - 1]
            if chosen != "off":
                stream_url = dests[chosen]
                print(f"  stream → {chosen} ({stream_url})")

    record_path = None
    if prompt_yn("Record to file?", default=False):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"recordings/{call}_{ts}.mp4"
        try:
            ans = input(f"  filename [{default_name}]: ").strip()
        except EOFError:
            ans = ""
        record_path = Path(ans or default_name)
        if not record_path.is_absolute():
            record_path = HERE / record_path

    print()
    print("── Confirm ──────────────────────────────────────────")
    print(f"  RF{rf} {virt} {call} ({net})")
    print(f"  play   : {'yes' if play else 'no'}")
    print(f"  stream : {stream_url or 'no'}")
    print(f"  record : {record_path or 'no'}")
    if not prompt_yn("Proceed?", default=True):
        raise SystemExit("aborted")

    return {
        "rf": rf, "program": prog, "callsign": call,
        "play": play, "stream_url": stream_url,
        "record_path": record_path,
    }


# ── Status loop ──────────────────────────────────────────────────
def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class PipelineState:
    """Mutable handle for pipeline subprocess refs and counters.

    status_loop reads/writes this in-place; recovery callbacks update
    the ffmpeg_proc / ffplay_proc after respawn.
    """
    def __init__(self):
        self.tv_proc: subprocess.Popen | None = None
        self.ffmpeg_proc: subprocess.Popen | None = None
        self.ffplay_proc: subprocess.Popen | None = None
        self.tail: TailWorker | None = None
        self.relay_thread: threading.Thread | None = None
        self.recovery_events = 0
        self.last_recovery_t = 0.0


def status_loop(state: PipelineState, stop_event: threading.Event,
                record_path: Path | None, stream_url: str | None,
                recover_ffmpeg=None, recover_ffplay=None,
                recover_decoder=None) -> None:
    start = time.time()
    last_size = 0
    last_t = start
    last_fwd = 0
    last_fwd_t = start
    last_rec_size = 0
    last_rec_t = start
    # Per-stream "frozen since" timestamps. When 0 we haven't decided
    # the stream is suspect yet.
    fwd_frozen_since = 0.0
    rec_frozen_since = 0.0
    # Cooldown after recovery — don't immediately re-trigger.
    RECOVERY_COOLDOWN_SEC = 15.0
    # Startup grace period — ffmpeg's mp4 muxer buffers the first
    # ~5-10s before flushing, and TS demux takes a few seconds to
    # lock to streams. Skip freeze detection until we have evidence
    # the pipeline reached steady state.
    STARTUP_GRACE_SEC = 6.0
    # If forwarded bytes haven't grown for this long while live.ts is
    # growing, ffmpeg's stdin is blocked. fwd is responsive to write
    # success; stall here means ffmpeg.exe is wedged.
    FREEZE_THRESHOLD_FWD_SEC = 8.0
    # Recording stalls take longer to be diagnostic because mp4 muxer
    # batches frame writes (every keyframe interval = 2s minimum).
    FREEZE_THRESHOLD_REC_SEC = 15.0
    # Periodic refresh: even when no explicit freeze is detected, the
    # decoder produces increasingly-corrupt H.264 over time as the
    # equalizer drifts. ffplay's window freezes despite ffmpeg's stdin
    # staying open (fwd keeps growing). Force-respawn ffmpeg+ffplay
    # every REFRESH_INTERVAL_SEC to give the user a fresh playback
    # window. Set to 0 to disable. Recordings disable this — kill+
    # respawn truncates the mp4.
    REFRESH_INTERVAL_SEC = 0.0   # disabled — cycling looked like channel changes
    last_refresh_t = start
    # Decoder-quality watchdog: every DECODER_CHECK_INTERVAL_SEC after
    # startup grace, sample PAT count from live.ts; if it's below
    # DECODER_BAD_PAT for DECODER_BAD_GRACE consecutive checks, restart
    # tv_live to grab a fresh equalizer convergence. This is what makes
    # playback usable indefinitely instead of permafreezing after drift.
    DECODER_CHECK_INTERVAL_SEC = 5.0
    DECODER_BAD_PAT = 3        # PAT in 5MB; below this = lost lock
    DECODER_BAD_GRACE = 1      # single bad check = restart (drift is fast)
    last_decoder_check_t = start
    decoder_bad_count = 0

    while not stop_event.is_set():
        time.sleep(2)
        if stop_event.is_set():
            break
        elapsed = time.time() - start
        try:
            ts_size = LIVE_TS.stat().st_size if LIVE_TS.exists() else 0
        except OSError:
            ts_size = 0
        now = time.time()
        rate = (ts_size - last_size) / max(0.001, now - last_t)
        last_size, last_t = ts_size, now
        locked = rate > 500_000   # ~0.5 MB/s ≈ ATSC lock
        rec_bytes = 0
        if record_path and record_path.exists():
            try: rec_bytes = record_path.stat().st_size
            except OSError: pass
        tv_alive = (state.tv_proc is not None
                    and state.tv_proc.poll() is None)
        ff_alive = (state.ffmpeg_proc is not None
                    and state.ffmpeg_proc.poll() is None)
        play_alive = (state.ffplay_proc is not None
                      and state.ffplay_proc.poll() is None)
        fwd = state.tail.bytes_forwarded if state.tail else 0

        past_grace = elapsed > STARTUP_GRACE_SEC
        # Freeze detection: ffmpeg stdin blocked.
        # Conditions for "stdin blocked":
        #   - past startup grace period
        #   - tv_live is alive AND live.ts is growing (>200KB/s) AND
        #   - tail.bytes_forwarded hasn't increased for threshold
        ffmpeg_freeze = False
        if past_grace and tv_alive and ff_alive and rate > 200_000:
            if fwd != last_fwd:
                last_fwd, last_fwd_t = fwd, now
                fwd_frozen_since = 0.0
            else:
                if fwd_frozen_since == 0.0:
                    fwd_frozen_since = last_fwd_t
                if now - fwd_frozen_since > FREEZE_THRESHOLD_FWD_SEC:
                    ffmpeg_freeze = True
        else:
            last_fwd, last_fwd_t = fwd, now
            fwd_frozen_since = 0.0

        # Note: we do NOT use record-file growth as a freeze indicator.
        # Empirically, when ffmpeg gets corrupted TS input, the mp4
        # muxer silently swallows frames (rec_bytes stops growing) but
        # ffmpeg itself is healthy and keeps consuming stdin. Killing
        # and respawning ffmpeg in that case TRUNCATES the partial
        # recording (mp4 needs a single open from start to write the
        # moov atom). The fwd-stalled detection above is the correct
        # signal — it fires only when ffmpeg's stdin is truly blocked.
        ffmpeg_rec_freeze = False
        last_rec_size, last_rec_t = rec_bytes, now
        rec_frozen_since = 0.0

        line = (f"[{int(elapsed):>5}s] "
                f"tv={'OK' if tv_alive else 'DEAD'} "
                f"ff={'OK' if ff_alive else 'DEAD'} "
                f"{('play=OK ' if play_alive else '') if state.ffplay_proc else ''}"
                f"lock={'YES' if locked else 'no '} "
                f"ts={fmt_size(ts_size)} "
                f"rate={rate/1e6:.2f}MB/s "
                f"fwd={fmt_size(fwd)}")
        if record_path:
            line += f" rec={fmt_size(rec_bytes)}"
        if stream_url:
            line += f" stream={'OK' if ff_alive else 'DOWN'}"
        if state.recovery_events:
            line += f" recoveries={state.recovery_events}"
        print(line, flush=True)

        if not tv_alive:
            print("[magic_tv] tv_live exited — shutting down")
            stop_event.set()
            break

        # Trigger ffmpeg recovery if frozen and a callback was provided.
        in_cooldown = (now - state.last_recovery_t) < RECOVERY_COOLDOWN_SEC
        if (ffmpeg_freeze or ffmpeg_rec_freeze) and recover_ffmpeg \
                and not in_cooldown:
            reason = "stdin blocked" if ffmpeg_freeze else "rec stalled"
            print(f"[magic_tv] ffmpeg appears frozen ({reason}, "
                  f"ts growing at {rate/1e6:.2f}MB/s but "
                  f"fwd/rec stalled) — recovering...")
            try:
                if recover_ffmpeg():
                    state.recovery_events += 1
                    state.last_recovery_t = now
                    last_fwd = state.tail.bytes_forwarded if state.tail else 0
                    last_fwd_t = now
                    fwd_frozen_since = 0.0
                    rec_frozen_since = 0.0
                    last_rec_t = now
                    print(f"[magic_tv] ffmpeg recovered "
                          f"(event #{state.recovery_events})")
                else:
                    print("[magic_tv] ffmpeg recovery failed — shutting down")
                    stop_event.set()
                    break
            except Exception as e:
                print(f"[magic_tv] recovery error: {e} — shutting down")
                stop_event.set()
                break
            continue

        # Decoder quality watchdog: sample PAT count periodically.
        # When it's persistently low past startup grace, the tv_live
        # equalizer has drifted. Restart it for a fresh lock.
        if (recover_decoder is not None
                and past_grace and tv_alive
                and not in_cooldown
                and (now - last_decoder_check_t) >= DECODER_CHECK_INTERVAL_SEC):
            last_decoder_check_t = now
            try:
                pat = measure_convergence()
            except Exception:
                pat = -1
            if pat >= 0 and pat < DECODER_BAD_PAT:
                decoder_bad_count += 1
                print(f"[magic_tv] decoder quality low: PAT={pat} "
                      f"(strike {decoder_bad_count}/{DECODER_BAD_GRACE})")
                if decoder_bad_count >= DECODER_BAD_GRACE:
                    print("[magic_tv] decoder drifted — restarting tv_live "
                          "for fresh lock...")
                    try:
                        if recover_decoder():
                            state.recovery_events += 1
                            state.last_recovery_t = time.time()
                            decoder_bad_count = 0
                            last_fwd = state.tail.bytes_forwarded if state.tail else 0
                            last_fwd_t = time.time()
                            fwd_frozen_since = 0.0
                            # Reset size tracking — file_sink truncated live.ts
                            last_size = LIVE_TS.stat().st_size if LIVE_TS.exists() else 0
                            last_t = time.time()
                            last_decoder_check_t = time.time()
                            print(f"[magic_tv] decoder restarted "
                                  f"(event #{state.recovery_events})")
                        else:
                            print("[magic_tv] decoder restart failed "
                                  "— continuing with degraded stream")
                            decoder_bad_count = 0
                    except Exception as e:
                        print(f"[magic_tv] decoder restart error: {e}")
                        decoder_bad_count = 0
                    continue
            else:
                # Good check resets the strike counter.
                decoder_bad_count = 0

        # Periodic refresh: silently respawn ffmpeg+ffplay every
        # REFRESH_INTERVAL_SEC to drop accumulated decoder-corruption from
        # the playback chain. Skip when recording (mp4 truncates) or when
        # we just recovered. Disabled by setting REFRESH_INTERVAL_SEC=0.
        if (REFRESH_INTERVAL_SEC > 0
                and record_path is None
                and recover_ffmpeg is not None
                and past_grace
                and not in_cooldown
                and ff_alive
                and (now - last_refresh_t) >= REFRESH_INTERVAL_SEC):
            print(f"[magic_tv] periodic refresh ({REFRESH_INTERVAL_SEC:.0f}s) "
                  "— respawning ffmpeg+ffplay for fresh playback window")
            try:
                if recover_ffmpeg():
                    state.recovery_events += 1
                    state.last_recovery_t = now
                    last_refresh_t = now
                    last_fwd = state.tail.bytes_forwarded if state.tail else 0
                    last_fwd_t = now
                    fwd_frozen_since = 0.0
            except Exception as e:
                print(f"[magic_tv] refresh error: {e}")
            continue

        # Only treat ffmpeg death as fatal if we actually spawned an
        # ffmpeg process. The magic_player playback path skips ffmpeg
        # entirely (player decodes live.ts directly), so state.ffmpeg_proc
        # will be None and ff_alive False legitimately.
        if state.ffmpeg_proc is not None and not ff_alive:
            # ffmpeg died but tv_live alive: try recovery.
            if recover_ffmpeg and not in_cooldown:
                print("[magic_tv] ffmpeg exited unexpectedly — recovering...")
                try:
                    if recover_ffmpeg():
                        state.recovery_events += 1
                        state.last_recovery_t = now
                        last_fwd = state.tail.bytes_forwarded if state.tail else 0
                        last_fwd_t = now
                        print(f"[magic_tv] ffmpeg recovered "
                              f"(event #{state.recovery_events})")
                        continue
                except Exception as e:
                    print(f"[magic_tv] recovery error: {e}")
            print("[magic_tv] ffmpeg exited — shutting down")
            stop_event.set()
            break

        # ffplay freeze detection: process alive but if it has stdin
        # we can't easily tell if it's blocked. We rely on its own
        # responsiveness; the relay thread will exit if its writes
        # break. For now, just respawn ffplay if it died unexpectedly.
        if state.ffplay_proc is not None and not play_alive \
                and recover_ffplay and not in_cooldown:
            print("[magic_tv] ffplay exited unexpectedly — recovering...")
            try:
                if recover_ffplay():
                    state.recovery_events += 1
                    state.last_recovery_t = now
                    print(f"[magic_tv] ffplay recovered "
                          f"(event #{state.recovery_events})")
            except Exception as e:
                print(f"[magic_tv] ffplay recovery error: {e}")


# ── Pipeline orchestration ───────────────────────────────────────
def run_pipeline(rf: int, callsign: str, play: bool,
                 stream_url: str | None, record_path: Path | None,
                 dry_run: bool = False, program: int = 1,
                 player: str = "ffplay", viterbi: str = "stock") -> int:
    # The 'magic' player reads live.ts directly and decodes with decoupled
    # audio/video clocks, so audio keeps playing while video holds on a drift
    # event — what produced our best real-RF result. Recording / RTMP need
    # the ffmpeg+ffplay pipeline, so those flags force player='ffplay'.
    if record_path is not None or stream_url is not None:
        if player == "magic":
            print("[magic_tv] --record/--stream require ffplay player path; "
                  "switching player from 'magic' to 'ffplay'.")
        player = "ffplay"
    if not (play or stream_url or record_path):
        # No outputs requested → still allow it if user is just locking
        # the tuner; emit warning.
        print("[magic_tv] no outputs selected (no --play, --stream, --record)."
              " Pipeline will run with a /dev/null sink.")

    log_dir = HERE / "data" / "tv_live"
    log_dir.mkdir(parents=True, exist_ok=True)
    tv_log = log_dir / "magic_tv.tv_live.log"
    ff_log = log_dir / "magic_tv.ffmpeg.log"
    play_log = log_dir / "magic_tv.ffplay.log"

    cmd = build_ffmpeg_cmd(play=play, record_path=record_path,
                           stream_url=stream_url, program=program)

    if dry_run:
        print("── DRY RUN ──")
        print("tv_live cmd: ", " ".join(
            [PYTHON_EXE, "-u", str(TV_LIVE_PY), "--rf", str(rf)]))
        print("ffmpeg cmd : ", " ".join(cmd))
        if play:
            print("ffplay cmd : ", " ".join(
                [FFPLAY, "-f", "mpegts", "-i", "pipe:0", "..."]))
        return 0

    if not Path(FFMPEG).exists():
        raise SystemExit(f"ffmpeg not found at {FFMPEG}")
    if play and not Path(FFPLAY).exists():
        raise SystemExit(f"ffplay not found at {FFPLAY}")

    stop_event = threading.Event()
    state = PipelineState()

    tv_log_fh = open(tv_log, "ab")
    ff_log_fh = open(ff_log, "ab")
    play_log_fh = open(play_log, "ab") if play else None

    # Original SIGINT handler so we can restore it
    orig_sigint = signal.getsignal(signal.SIGINT)

    def shutdown(*_a):
        if stop_event.is_set():
            return
        print("\n[magic_tv] shutting down...")
        stop_event.set()
        # Order: tv_live first (drops SDR), then ffmpeg, then ffplay
        kill_proc(state.tv_proc, "tv_live")
        kill_proc(state.ffmpeg_proc, "ffmpeg")
        kill_proc(state.ffplay_proc, "ffplay")

    signal.signal(signal.SIGINT, shutdown)

    # ── Recovery callbacks ─────────────────────────────────────
    def recover_ffmpeg() -> bool:
        """Kill+respawn ffmpeg, hot-swap tail target, restart relay.
        Returns True on success, False if respawn fails.
        """
        if stop_event.is_set():
            return False
        # Kill old ffmpeg (and ffplay if it depends on its stdout).
        kill_proc(state.ffmpeg_proc, "ffmpeg")
        old_play = state.ffplay_proc
        state.ffmpeg_proc = None

        # If we were playing, the relay thread feeding ffplay from
        # ffmpeg's stdout has already exited (its src closed). Kill
        # ffplay too — we'll respawn it and rebuild the relay so the
        # new ffmpeg's output flows there.
        if old_play is not None:
            kill_proc(old_play, "ffplay")
            state.ffplay_proc = None

        # Respawn ffmpeg.
        try:
            new_ff = spawn_ffmpeg(cmd, ff_log_fh,
                                  want_stdout_pipe=play)
        except Exception as e:
            print(f"[magic_tv] failed to respawn ffmpeg: {e}",
                  file=sys.stderr)
            return False
        state.ffmpeg_proc = new_ff

        # Hot-swap the tail's output target. The tail worker keeps
        # running; old stdin is closed inside swap_target.
        if state.tail is not None:
            state.tail.swap_target(new_ff.stdin)

        # Rebuild ffplay + relay if user wanted local play.
        if play:
            try:
                new_play = spawn_ffplay(
                    f"Magic TV — {callsign} (RF{rf})", play_log_fh)
                state.ffplay_proc = new_play
                state.relay_thread = spawn_relay(
                    new_ff.stdout, new_play.stdin,
                    stop_event, tag="ffmpeg→ffplay")
            except Exception as e:
                print(f"[magic_tv] ffplay respawn failed: {e}",
                      file=sys.stderr)
                # Continue without local playback.
        return True

    def recover_ffplay() -> bool:
        """Respawn ffplay (and its relay) without touching ffmpeg."""
        if stop_event.is_set() or not play:
            return False
        ff = state.ffmpeg_proc
        if ff is None or ff.poll() is not None:
            return False
        kill_proc(state.ffplay_proc, "ffplay")
        state.ffplay_proc = None
        try:
            new_play = spawn_ffplay(
                f"Magic TV — {callsign} (RF{rf})", play_log_fh)
            state.ffplay_proc = new_play
            state.relay_thread = spawn_relay(
                ff.stdout, new_play.stdin,
                stop_event, tag="ffmpeg→ffplay")
        except Exception as e:
            print(f"[magic_tv] ffplay respawn failed: {e}",
                  file=sys.stderr)
            return False
        return True

    def acquire_lock(max_retries: int, window_sec: float,
                     min_pat: int) -> subprocess.Popen | None:
        """Spawn tv_live, run the convergence-retry loop, return the live
        Popen on success or None if all retries exhaust."""
        for attempt in range(1, max_retries + 1):
            tv = spawn_tv_live(rf, tv_log_fh, viterbi=viterbi)
            print(f"[magic_tv] tv_live PID={tv.pid} (attempt {attempt}); "
                  f"waiting {window_sec:.0f}s for convergence...")
            if not wait_for_live_ts(timeout_sec=15.0):
                kill_proc(tv, "tv_live")
                if attempt >= max_retries:
                    return None
                time.sleep(2)
                continue
            t0 = time.time()
            while time.time() - t0 < window_sec:
                if tv.poll() is not None:
                    break
                time.sleep(0.5)
            pat = measure_convergence()
            print(f"[magic_tv] convergence check: PAT={pat} in 5MB "
                  f"(need ≥{min_pat})")
            if pat >= min_pat:
                print(f"[magic_tv] LOCK acquired on attempt {attempt}.")
                return tv
            print(f"[magic_tv] bad convergence — killing and retrying...")
            kill_proc(tv, "tv_live")
            time.sleep(2)
        return None

    def recover_decoder() -> bool:
        """Restart tv_live AND the player chain for a clean fresh state.
        We tried keeping ffmpeg/ffplay alive across truncation (commit
        261629a), but ffmpeg's audio/video decoder state went bad after
        the live.ts reset, producing no-sound + glitchy output. Full
        restart gives a brief player window flicker but clean A/V on
        recovery — the better tradeoff in practice."""
        if stop_event.is_set():
            return False
        kill_proc(state.tv_proc, "tv_live")
        state.tv_proc = None
        if state.ffmpeg_proc is not None:
            kill_proc(state.ffmpeg_proc, "ffmpeg")
            state.ffmpeg_proc = None
        if state.ffplay_proc is not None:
            kill_proc(state.ffplay_proc, "ffplay")
            state.ffplay_proc = None
        if state.tail is not None:
            state.tail.stop_local()
            state.tail = None
        time.sleep(2)

        new_tv = acquire_lock(max_retries=4, window_sec=12.0, min_pat=5)
        if new_tv is None:
            print("[magic_tv] decoder restart could not re-acquire lock")
            return False
        state.tv_proc = new_tv

        # Respawn ffmpeg + tail + ffplay with clean state.
        try:
            new_ff = spawn_ffmpeg(cmd, ff_log_fh, want_stdout_pipe=play)
        except Exception as e:
            print(f"[magic_tv] ffmpeg respawn failed: {e}", file=sys.stderr)
            return False
        state.ffmpeg_proc = new_ff
        state.tail = TailWorker(new_ff.stdin, stop_event)
        state.tail.start()
        if play:
            try:
                new_play = spawn_ffplay(
                    f"Magic TV — {callsign} (RF{rf})", play_log_fh)
                state.ffplay_proc = new_play
                state.relay_thread = spawn_relay(
                    new_ff.stdout, new_play.stdin,
                    stop_event, tag="ffmpeg→ffplay")
            except Exception as e:
                print(f"[magic_tv] ffplay respawn failed: {e}",
                      file=sys.stderr)
        return True

    try:
        print(f"[magic_tv] starting tv_live for RF{rf} ({callsign})")
        print(f"[magic_tv]   logs: {tv_log}, {ff_log}"
              f"{', '+str(play_log) if play else ''}")

        # Convergence watchdog: the atscplus long equalizer locks
        # probabilistically. Spawn tv_live, wait, sample PAT count;
        # if convergence is bad, kill and retry. Up to MAX_RETRIES tries.
        MAX_RETRIES = 6
        CONVERGENCE_WINDOW_SEC = 12.0
        MIN_GOOD_PAT = 5
        for attempt in range(1, MAX_RETRIES + 1):
            state.tv_proc = spawn_tv_live(rf, tv_log_fh, viterbi=viterbi)
            print(f"[magic_tv] tv_live PID={state.tv_proc.pid} "
                  f"(attempt {attempt}); waiting "
                  f"{CONVERGENCE_WINDOW_SEC:.0f}s for convergence...")
            if not wait_for_live_ts(timeout_sec=15.0):
                print("[magic_tv] live.ts didn't start growing.")
                kill_proc(state.tv_proc, "tv_live")
                state.tv_proc = None
                if attempt < MAX_RETRIES:
                    time.sleep(2)
                    continue
                print("[magic_tv] giving up — check tv_live log:", tv_log)
                shutdown()
                return 2
            t0 = time.time()
            while time.time() - t0 < CONVERGENCE_WINDOW_SEC:
                if state.tv_proc.poll() is not None:
                    break
                time.sleep(0.5)
            pat_count = measure_convergence()
            print(f"[magic_tv] convergence check: PAT={pat_count} in 5MB "
                  f"(need ≥{MIN_GOOD_PAT})")
            if pat_count >= MIN_GOOD_PAT:
                print(f"[magic_tv] LOCK acquired on attempt {attempt}.")
                break
            print(f"[magic_tv] bad convergence — killing and retrying...")
            kill_proc(state.tv_proc, "tv_live")
            state.tv_proc = None
            time.sleep(2)
        else:
            print(f"[magic_tv] failed to acquire good lock after "
                  f"{MAX_RETRIES} attempts.")
            shutdown()
            return 3

        # Two playback paths.
        # (a) magic_player: reads live.ts directly with decoupled audio/video
        #     clocks. Skips the ffmpeg+ffplay middleman entirely. This is the
        #     path that produced our best real-RF result — audio kept playing
        #     through SDR drift while video held the last good frame.
        # (b) ffplay: legacy path. ffmpeg re-encodes, tee fans out to ffplay
        #     and any record/stream sinks. Required when --record or --stream
        #     is set (run_pipeline forces player='ffplay' in that case).
        if play and player == "magic":
            if not MAGIC_PLAYER.exists():
                print(f"[magic_tv] magic_player.py not found at {MAGIC_PLAYER} "
                      "— falling back to ffplay path.", file=sys.stderr)
                player = "ffplay"

        if play and player == "magic":
            # cv2.imshow() doesn't reliably attach a GUI window when
            # magic_player is spawned as a child subprocess on Windows.
            # The configuration that does work is launching magic_player
            # interactively from its own PowerShell window. Print clear
            # instructions and don't spawn it ourselves.
            print()
            print("=" * 70)
            print(" Open a SECOND PowerShell window and paste this:")
            print()
            print(f'   & "{PYTHON_EXE}" "{MAGIC_PLAYER}" "{LIVE_TS}"')
            print()
            print(" The OpenCV video window will appear once it locks.")
            print(" Status overlay shows decoder + buffer health in real time.")
            print(" When done, Ctrl+C in that window to close the player.")
            print()
            print(" NOTE: Use the radioconda python path above — system")
            print(" python doesn't have cv2 / sounddevice installed.")
            print("=" * 70)
            print()
            # We still keep magic_tv running so the decoder watchdog,
            # convergence retries, and tv_live process are managed.
            # state.ffplay_proc stays None — status_loop tolerates that.
            # No ffmpeg, no tail thread — magic_player runs separately
            # in the user's second PowerShell window.
            status_loop(state, stop_event, record_path, stream_url,
                        recover_ffmpeg=None,
                        recover_ffplay=None,
                        recover_decoder=recover_decoder)
        else:
            # Legacy ffplay path: ffmpeg re-encode → optional tee fan-out.
            # Now that live.ts is converged, translate the user's 1-based
            # subchannel index to the actual ATSC program_id by probing
            # the PMT — broadcasters number programs arbitrarily (RF34's
            # 4.1 NBC isn't program_id=1).
            actual_pid = probe_program_id(program)
            if actual_pid is not None and actual_pid != program:
                print(f"[magic_tv] subchannel {program} → program_id {actual_pid}")
                cmd = build_ffmpeg_cmd(play=play, record_path=record_path,
                                       stream_url=stream_url, program=actual_pid)
            elif actual_pid is None:
                print(f"[magic_tv] WARN: could not probe program list; "
                      f"using --program {program} as program_id directly.")
            state.ffmpeg_proc = spawn_ffmpeg(cmd, ff_log_fh,
                                             want_stdout_pipe=play)
            print(f"[magic_tv] ffmpeg PID={state.ffmpeg_proc.pid}")

            if play:
                state.ffplay_proc = spawn_ffplay(
                    f"Magic TV — {callsign} (RF{rf})", play_log_fh)
                print(f"[magic_tv] ffplay PID={state.ffplay_proc.pid}")
                state.relay_thread = spawn_relay(
                    state.ffmpeg_proc.stdout, state.ffplay_proc.stdin,
                    stop_event, tag="ffmpeg→ffplay")

            state.tail = TailWorker(state.ffmpeg_proc.stdin, stop_event)
            state.tail.start()
            status_loop(state, stop_event, record_path, stream_url,
                        recover_ffmpeg=recover_ffmpeg,
                        recover_ffplay=recover_ffplay,
                        recover_decoder=recover_decoder)

    except Exception as e:
        print(f"[magic_tv] error: {e}", file=sys.stderr)
        shutdown()
        return 1
    finally:
        shutdown()
        signal.signal(signal.SIGINT, orig_sigint)
        for fh in (tv_log_fh, ff_log_fh, play_log_fh):
            if fh:
                try: fh.close()
                except OSError: pass

    print("[magic_tv] clean exit.")
    return 0


# ── CLI ──────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="magic_tv",
        description="Magic TV — tune, play, record, and stream ATSC channels "
                    "via the SDRplay RSPdx + gr-atscplus pipeline.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--rf", type=int, default=None,
                   help="RF channel to tune (skips interactive picker)")
    p.add_argument("--program", type=int, default=1,
                   help="ATSC program/sub-channel number to play (1 = main, "
                        "2 = .2 sub, 3 = .3 sub, ...). Used with --rf.")
    p.add_argument("--player", choices=["magic", "ffplay"], default="ffplay",
                   help="Playback engine. 'ffplay' (default) uses ffmpeg "
                        "re-encode + ffplay — single window, simpler UX. "
                        "'magic' uses our resilient magic_player.py with "
                        "decoupled audio/video clocks and a diagnostic "
                        "status overlay (held last frame on drift). "
                        "Recording / RTMP streaming require 'ffplay'.")
    p.add_argument("--viterbi", choices=["stock", "soft"], default="stock",
                   help="Viterbi decoder variant. 'stock' (default) uses the "
                        "hard-decision gr-dtv Viterbi. 'soft' uses the fork's "
                        "atsc_viterbi_soft (Tier 9 fix landed) which can in "
                        "theory recover marginal-SNR symbols but didn't show "
                        "measurable real-RF gain on the test channel. Opt-in.")
    p.add_argument("--play", dest="play", action="store_true",
                   default=None,
                   help="Force local playback via ffplay")
    p.add_argument("--no-play", dest="play", action="store_false",
                   help="Disable local playback (recording/streaming only)")
    p.add_argument("--stream", default=None, metavar="NAME",
                   help="Name of a destination from magic_tv_config.json "
                        "(or a literal rtmp:// URL)")
    p.add_argument("--record", default=None, metavar="FILE",
                   help="Record to this MP4 file (relative paths go under "
                        "Z:\\SDR_Agent_v2)")
    p.add_argument("--list", action="store_true",
                   help="Print the channel table and exit")
    p.add_argument("--scan", action="store_true",
                   help="Sweep the SDR across all RF channels (~6 min), "
                        "save the result to ~/.magic_tv/scan.json, then "
                        "exit. Run this once after install.")
    p.add_argument("--scan-dwell", type=float, default=12.0,
                   help="Seconds to wait per channel during --scan "
                        "(longer = more reliable lock detection)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the planned subprocess commands and exit "
                        "(does not start tv_live, ffmpeg, or ffplay)")
    p.add_argument("--config-set", nargs=2, metavar=("NAME", "URL"),
                   help="Save NAME→URL to the destinations config and exit")
    p.add_argument("--config-show", action="store_true",
                   help="Print the current config and exit")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Always make sure config exists / is loaded
    cfg = load_config()

    if args.config_set:
        name, url = args.config_set
        cfg.setdefault("destinations", {})[name] = url
        save_config(cfg)
        print(f"[magic_tv] saved destination: {name} → {url}")
        return 0

    if args.config_show:
        print(json.dumps(cfg, indent=2))
        return 0

    if args.list:
        print_channel_list()
        return 0

    if args.scan:
        run_scan(dwell_sec=args.scan_dwell)
        return 0

    # Resolve stream URL: NAME from config, else literal URL, else None
    def resolve_stream(s: str | None) -> str | None:
        if not s:
            return None
        dests = cfg.get("destinations", {})
        if s in dests:
            return dests[s]
        if "://" in s:
            return s
        print(f"[magic_tv] unknown destination '{s}'. "
              f"Known: {', '.join(dests.keys()) or '(none)'}")
        sys.exit(2)

    # Resolve record path
    def resolve_record(s: str | None) -> Path | None:
        if not s:
            return None
        p = Path(s)
        return p if p.is_absolute() else (HERE / p)

    # Decide between scriptable mode and interactive mode.
    has_flags = (args.rf is not None or args.stream is not None
                 or args.record is not None or args.play is not None
                 or args.dry_run)

    if has_flags:
        if args.rf is None:
            print("[magic_tv] --rf is required in scriptable mode.",
                  file=sys.stderr)
            return 2
        _, lookup = _load_stations()
        info = lookup(args.rf)
        callsign = info["callsign"] if info else f"RF{args.rf}"
        play = True if args.play is None else bool(args.play)
        stream_url = resolve_stream(args.stream)
        record_path = resolve_record(args.record)
        return run_pipeline(rf=args.rf, callsign=callsign,
                            play=play, stream_url=stream_url,
                            record_path=record_path,
                            dry_run=args.dry_run,
                            program=args.program,
                            player=args.player,
                            viterbi=args.viterbi)
    else:
        # Pure interactive mode
        choice = interactive_pick(cfg)
        return run_pipeline(rf=choice["rf"], callsign=choice["callsign"],
                            play=choice["play"],
                            stream_url=choice["stream_url"],
                            record_path=choice["record_path"],
                            dry_run=False,
                            program=choice["program"],
                            player=args.player,
                            viterbi=args.viterbi)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[magic_tv] interrupted")
        sys.exit(130)
