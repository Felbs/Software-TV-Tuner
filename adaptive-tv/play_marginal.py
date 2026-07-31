"""Marginal-signal-friendly TV player for Windows.

Solves the "chain locks cleanly but VLC starves/crashes on glitchy MPEG-2"
problem documented in [[marginal-antenna-branch-2026-06-20]] / Ubuntu work.
Port of the winning recipe (scored 100/100 on quality_judge.sh):

  STVT_MPV_EC=1         error_concealment=3 (interpolate lost macroblocks)
  STVT_MPV_SMOOTH=1     +genpts +igndts video-sync=desync (no rebuffer-pause)
  STVT_CACHE_SECS=8     tight cache so it never rebuffer-freezes
  STVT_AUDIO_LIMIT=1    ffmpeg +discardcorrupt + mpv alimiter (audio pop guard)

Pipeline:
    tail live.ts → ffmpeg (-c copy, +discardcorrupt, program select) → mpv (stdin)

Why this pipeline (not VLC direct):
  - VLC's MPEG-2 decoder bails on persistent macroblock errors → crashes
  - mpv's error_concealment=3 interpolates lost blocks like a TV does
  - ffmpeg `-c copy` passes through without re-encoding (CPU-cheap) but
    DOES de-mux a single program out of the multi-program TS, dodging
    VLC's --programs reliability issue ([[vlc-program-filter-unreliable]])
  - +discardcorrupt at ffmpeg drops audio packets that would have caused
    pops/static

Usage:
    python play_marginal.py 1                 # play program 1
    python play_marginal.py 3 --ts custom.ts  # custom TS file
    python play_marginal.py 1 --no-ec         # disable error concealment
    python play_marginal.py 1 --no-hwdec      # force SW decode (concealment works best)

By default reads the live.ts from the running tv_live chain at
  Z:\\src\\magic-tv-decoder\\tools\\data\\tv_live\\live.ts
and tails the last N MB so it skips the equalizer-convergence burst at start.
"""
import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_TS = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
MPV_EXE    = Path(r"C:\Program Files\MPV Player\mpv.exe")
# ffmpeg from winget install or PATH
FFMPEG_EXE = "ffmpeg"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("program", type=int, nargs="?", default=1,
                    help="ATSC program number (default 1)")
    ap.add_argument("--ts", default=str(DEFAULT_TS),
                    help="path to TS file (default: running chain's live.ts)")
    ap.add_argument("--tail-mb", type=int, default=2,
                    help="read last N MB of TS as runway, then tail. default 2 MB (~0.8s of stream).")
    ap.add_argument("--no-ec", action="store_true", help="disable error concealment")
    ap.add_argument("--no-smooth", action="store_true", help="disable smooth playback profile")
    ap.add_argument("--strong", action="store_true",
                    help="STRONG-signal mode: clean decode (wait for keyframes, drop "
                         "corrupt packets). Use when SNR is high — the anti-freeze "
                         "show-all hacks cause 'frozen green' on a clean signal.")
    ap.add_argument("--no-hwdec", action="store_true",
                    help="force SW decode (default; HW decode disables concealment)")
    ap.add_argument("--hwdec", action="store_true",
                    help="enable HW decode (faster but no error concealment)")
    ap.add_argument("--cache-secs", type=float, default=8.0,
                    help="player cache depth in seconds (default 8)")
    ap.add_argument("--alang", default="eng", help="preferred audio language (default eng)")
    ap.add_argument("--cc", action="store_true",
                    help="show closed captions (CEA-608 ride inside the video "
                         "stream and survive the -c copy remux; mpv just never "
                         "creates/selects the track by default)")
    args = ap.parse_args()

    ts_path = Path(args.ts)
    if not ts_path.exists():
        print(f"[play] no TS at {ts_path}; is tv_live running?", file=sys.stderr)
        sys.exit(1)
    if not MPV_EXE.exists():
        print(f"[play] mpv missing at {MPV_EXE}", file=sys.stderr)
        sys.exit(2)

    size_mb = ts_path.stat().st_size // (1024 * 1024)
    print(f"[play] TS: {ts_path}  ({size_mb} MB)  program={args.program}")

    # Direct mpv on the file — bypass the ffmpeg-pipe whose stdin-streaming
    # disrupted MPEG-TS sync. mpv's internal libavformat demuxer handles a
    # growing file fine when given the right flags.
    ff_cmd = None   # not used in direct-mpv path

    # LIVE 3-STAGE PIPE: tail(live.ts) -> ffmpeg(isolate 1 program) -> mpv.
    #   - tail thread follows the growing file (never EOFs, 188-byte aligned)
    #   - ffmpeg -map 0:p:N reliably ISOLATES one program (mpv's program_number
    #     option is ignored through a stdin pipe — ffmpeg demux is the only
    #     dependable way to pick the SD subchannel over the fragile HD one)
    #   - mpv decodes the clean single-program stream with error concealment
    use_hwdec = args.hwdec and not args.no_hwdec
    # NEVER discard packets — even on a clean TS, +discardcorrupt drops the
    # sequence-header packets the probe needs and you get "Invalid frame
    # dimensions 0x0" + mpv exit. The strong-vs-marginal difference is ONLY the
    # mpv show-all anti-freeze hacks (which cause "frozen green" on a clean
    # signal), not packet dropping.
    # --cc: drop corrupt/pre-keyframe frames in the remux so the CEA-608
    # decoder never ingests mid-sequence caption bytes (the "stuck gibberish
    # caption on startup" bug — 608 doubles control bytes; joining mid-pair
    # scrambles alignment and the garbage sticks until the next erase).
    discard = "+discardcorrupt" if args.cc else ""
    ff_cmd = [FFMPEG_EXE, "-hide_banner", "-loglevel", "error",
              "-fflags", f"{discard}+genpts+igndts+nobuffer",
              "-err_detect", "ignore_err",
              "-analyzeduration", "3000000", "-probesize", "5000000",
              "-f", "mpegts", "-i", "-",
              "-map", f"0:p:{args.program}",
              "-c", "copy", "-f", "mpegts", "-"]
    mpv_cmd = [str(MPV_EXE), "fd://0",
               "--demuxer-lavf-format=mpegts",
               "--demuxer-lavf-o=err_detect=ignore_err",
               f"--demuxer-lavf-o=fflags={discard}+genpts+igndts+nobuffer",
               "--demuxer-lavf-analyzeduration=2",
               "--demuxer-lavf-probesize=3000000",
               f"--alang={args.alang}",
               "--keep-open=no", "--osc=yes",
               f"--cache-secs={args.cache_secs}",
               "--cache-pause=no",
               "--demuxer-readahead-secs=10",
               "--audio-stream-silence=yes",
               ]
    if not args.strong:
        # ANTI-FREEZE (marginal only): show every decoded frame even without a
        # clean keyframe reference, never skip. Trades freeze->datamosh. On a
        # STRONG signal these cause a stuck green pre-keyframe frame, so skip them.
        mpv_cmd.extend(["--vd-lavc-show-all=yes",
                        "--vd-lavc-skipframe=none",
                        "--vd-lavc-skipidct=none",
                        "--framedrop=no"])
    mpv_cmd.append("--hwdec=auto" if use_hwdec else "--hwdec=no")
    if not args.no_ec:
        mpv_cmd.extend(["--vd-lavc-o=error_concealment=3",
                        "--vd-lavc-o=err_detect=ignore_err"])
    if not args.no_smooth:
        mpv_cmd.append("--video-sync=desync")
    # IPC pipe: lets a supervisor verify real playback (time-pos advancing)
    # and lets the CC cleaner reset the caption renderer after startup.
    # STVT_PLAY_IPC overrides the name so external tools can find it.
    cc_pipe = os.environ.get("STVT_PLAY_IPC") or \
        (rf"\\.\pipe\mpv-tvtuna-{os.getpid()}" if args.cc else None)
    if cc_pipe:
        mpv_cmd.append(f"--input-ipc-server={cc_pipe}")
    if args.cc:
        # single-program remux -> the auto-created CC track is sub track 1.
        # Joining a live 608 stream mid-caption paints partial garbage that
        # can stick on unused rows, so an IPC helper auto-cycles the track
        # once after startup (same fix as manually toggling CC off/on).
        mpv_cmd.extend(["--sub-create-cc-track=yes", "--sid=1"])

    print(f"[play] LIVE 3-stage pipe: tail -> ffmpeg(-map 0:p:{args.program}) -> mpv")
    print(f"[play] hwdec={'on' if use_hwdec else 'off'}  ec={'off' if args.no_ec else 'on'}  "
          f"program={args.program}  cache={args.cache_secs}s  alang={args.alang}")
    print()

    ff  = subprocess.Popen(ff_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    mpv = subprocess.Popen(mpv_cmd, stdin=ff.stdout)
    ff.stdout.close()   # mpv owns the read end

    if cc_pipe and args.cc:
        def cc_cleaner():
            # let the caption decoder align on real data, then reset the
            # renderer (drops any startup garbage; harmless if none)
            time.sleep(14)
            for cmd_line in (b'{"command":["set_property","sid","no"]}\n',
                             b'{"command":["set_property","sid",1]}\n'):
                try:
                    with open(cc_pipe, "wb") as pipe:
                        pipe.write(cmd_line)
                    time.sleep(0.5)
                except OSError:
                    return   # mpv gone or pipe unavailable; captions still work
        threading.Thread(target=cc_cleaner, daemon=True).start()

    stop_flag = threading.Event()
    def tail_worker():
        with open(ts_path, "rb") as f:
            cur = ts_path.stat().st_size
            start = max(0, cur - args.tail_mb * 1024 * 1024)
            start = (start // 188) * 188
            f.seek(start)
            print(f"[tail] streaming from {start//(1024*1024)} MB (runway {(cur-start)//1024} KB), following growth")
            idle = 0
            while not stop_flag.is_set():
                chunk = f.read(188 * 1024)
                if not chunk:
                    idle += 1
                    if idle > 300:
                        print("[tail] no data for 30s — chain stopped")
                        break
                    time.sleep(0.1)
                    continue
                idle = 0
                try:
                    ff.stdin.write(chunk)
                    ff.stdin.flush()
                except (BrokenPipeError, ValueError, OSError):
                    break
        try: ff.stdin.close()
        except Exception: pass

    t = threading.Thread(target=tail_worker, daemon=True)
    t.start()
    try:
        mpv.wait()
    except KeyboardInterrupt:
        print("\n[play] stopping...")
    finally:
        stop_flag.set()
        for p in (mpv, ff):
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=3)
                except Exception: p.kill()


if __name__ == "__main__":
    main()
