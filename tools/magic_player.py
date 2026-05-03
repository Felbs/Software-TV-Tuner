#!/usr/bin/env python3
"""
magic_player.py - Resilient video player for unreliable / partial elementary streams

Designed for the Magic TV Tuner project where SDR ATSC reception produces
intermittent corrupt H.264 / MPEG-2.  Where ffplay/VLC freeze, this player
holds the last good frame, optionally interpolates, advances audio in
real-time, and overlays a status line.

Architecture
------------

    +------------+      +-----------------+      +-----------------+
    |  Source    |      |  ffmpeg sub     |      |     Display     |
    |  file/-/   |----->|  proc           |----->|  cv2.imshow     |
    |  http/...  |      |  -> rawvideo    |      |  +overlay       |
    +------------+      |  -> s16le       |      +-----------------+
                        |                 |
                        +--Probe (PyAV)---+      +-----------------+
                        |                 |----->|  Audio out      |
                        +-----------------+      |  sounddevice    |
                                                 +-----------------+

The decoder is run in a separate process (ffmpeg).  Python only sees
already-decoded raw video + audio.  This means:
  - bad bytes never propagate into the Python interpreter
  - libav's own +discardcorrupt / -err_detect ignore_err do their job
    *and* the ffmpeg process is isolated from the player
  - if ffmpeg dies, we respawn it without taking down the player

Decoupling:
  * Reader thread pulls rawvideo bytes -> video ring buffer
  * Reader thread pulls s16le audio    -> audio ring buffer
  * Display thread renders at fixed fps from the latest frame
  * Audio callback thread plays at real-time from the audio buffer
  * Watchdog thread detects ffmpeg stalls and respawns it

Dependencies
------------
  pip install av numpy opencv-python sounddevice
  (PyAV is only used for probing; rawvideo/s16le decode is handled by
  ffmpeg subprocess.  If PyAV probe hangs, we fall back to ffprobe CLI
  or to user-supplied --video-size / --audio-rate / --audio-channels.)

Run
---
    python magic_player.py <source>
        <source> = path to .ts/.mp4/.mkv, or - for stdin, or http(s)://...

    Examples:
      python magic_player.py Z:/SDR_Agent_v2/soak_test.mp4
      python magic_player.py Z:/SDR_Agent_v2/data/tv_live/live.ts
      ffmpeg ... | python magic_player.py -

Keys
----
    q / ESC : quit
    s       : toggle stats overlay
    i       : toggle frame interpolation
    space   : (placeholder; audio always plays)
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np


# ----------------------------------------------------------------------------
# Optional imports
# ----------------------------------------------------------------------------
def _imp(name):
    try:
        return __import__(name)
    except Exception as e:
        print(f"[magic_player] WARN cannot import {name}: {e}", file=sys.stderr)
        return None


cv2 = _imp("cv2")
sd = _imp("sounddevice")


# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
FFMPEG = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
FFPROBE = shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"


# ----------------------------------------------------------------------------
# Data types
# ----------------------------------------------------------------------------
@dataclass
class VideoFrame:
    img: np.ndarray  # BGR uint8 HxWx3
    received_at: float = field(default_factory=time.monotonic)


@dataclass
class AudioFrame:
    samples: np.ndarray  # float32 (n,2)
    received_at: float = field(default_factory=time.monotonic)


@dataclass
class PlayerState:
    running: bool = True
    show_stats: bool = True
    interp_enabled: bool = False

    bytes_in_video: int = 0
    bytes_in_audio: int = 0
    video_frames_decoded: int = 0
    audio_chunks_decoded: int = 0
    video_frames_displayed: int = 0
    last_frame_age_ms: float = 0.0
    waiting: bool = False
    decoder_status: str = "INIT"
    ffmpeg_respawns: int = 0
    audio_underruns: int = 0
    last_video_byte_at: float = field(default_factory=time.monotonic)
    last_audio_byte_at: float = field(default_factory=time.monotonic)


# ----------------------------------------------------------------------------
# Ring buffer
# ----------------------------------------------------------------------------
class RingBuffer:
    def __init__(self, maxlen: int):
        self._dq = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def put(self, item):
        with self._lock:
            self._dq.append(item)

    def pop_oldest(self):
        with self._lock:
            return self._dq.popleft() if self._dq else None

    def get_latest(self):
        with self._lock:
            return self._dq[-1] if self._dq else None

    def __len__(self):
        with self._lock:
            return len(self._dq)


# ----------------------------------------------------------------------------
# Probe
# ----------------------------------------------------------------------------
def probe_streams(source: str, timeout: float = 12.0
                  ) -> Tuple[Optional[Tuple[int, int, float]],
                             Optional[Tuple[int, int]]]:
    """Probe video (w,h,fps) and audio (sr,channels) using ffprobe.
    Returns (video_info or None, audio_info or None).  Tolerates corrupt
    inputs.  For stdin or pipe we cannot probe; caller must supply hints."""
    if source == "-" or source == "stdin":
        return None, None

    try:
        # Video
        cmd_v = [FFPROBE, "-v", "error", "-fflags", "+discardcorrupt",
                 "-err_detect", "ignore_err", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,r_frame_rate,codec_name",
                 "-of", "json", source]
        cmd_a = [FFPROBE, "-v", "error", "-fflags", "+discardcorrupt",
                 "-err_detect", "ignore_err", "-select_streams", "a:0",
                 "-show_entries", "stream=sample_rate,channels,codec_name",
                 "-of", "json", source]

        v = subprocess.run(cmd_v, capture_output=True, timeout=timeout)
        a = subprocess.run(cmd_a, capture_output=True, timeout=timeout)

        v_info = None
        a_info = None
        if v.returncode == 0:
            try:
                d = json.loads(v.stdout.decode("utf-8", "replace"))
                if d.get("streams"):
                    s = d["streams"][0]
                    w = int(s.get("width", 0))
                    h = int(s.get("height", 0))
                    rfr = s.get("r_frame_rate", "30/1")
                    num, den = rfr.split("/")
                    fps = float(num)/float(den) if float(den) else 30.0
                    if w > 0 and h > 0:
                        v_info = (w, h, fps)
            except Exception:
                pass
        if a.returncode == 0:
            try:
                d = json.loads(a.stdout.decode("utf-8", "replace"))
                if d.get("streams"):
                    s = d["streams"][0]
                    sr = int(s.get("sample_rate", 0))
                    ch = int(s.get("channels", 0))
                    if sr > 0 and ch > 0:
                        a_info = (sr, ch)
            except Exception:
                pass
        return v_info, a_info
    except subprocess.TimeoutExpired:
        return None, None
    except Exception as e:
        print(f"[probe] error: {e}", file=sys.stderr)
        return None, None


# ----------------------------------------------------------------------------
# ffmpeg subprocess decoder
# ----------------------------------------------------------------------------
class FFDecoder(threading.Thread):
    """Spawn ffmpeg, read rawvideo (bgr24) and s16le audio from two pipes.
    Respawn on death.  Never raise out of run()."""

    def __init__(self,
                 source: str,
                 width: int,
                 height: int,
                 fps: float,
                 audio_sr: int,
                 audio_channels: int,
                 video_buf: RingBuffer,
                 audio_buf: RingBuffer,
                 state: PlayerState,
                 disable_audio: bool = False,
                 vf: Optional[str] = None):
        super().__init__(daemon=True, name="FFDecoder")
        self.source = source
        self.width = width
        self.height = height
        self.fps = fps
        self.audio_sr = audio_sr
        self.audio_channels = audio_channels
        self.video_buf = video_buf
        self.audio_buf = audio_buf
        self.state = state
        self.disable_audio = disable_audio
        self.vf = vf  # additional video filter, e.g. scale
        self._proc: Optional[subprocess.Popen] = None
        self._stop_evt = threading.Event()
        self._readers: list[threading.Thread] = []

    # ------------------------------------------------------------------
    def stop(self):
        self._stop_evt.set()
        self._kill()

    def _kill(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _build_cmd(self) -> list:
        # Two outputs on a single ffmpeg: video to fd 3, audio to fd 4
        # Windows: cannot use named fds easily; use named pipes only via
        # \\.\pipe.  Simpler: invoke two ffmpeg processes? No - we want a
        # single decode pass.  We'll use stdout for video and a TCP-like
        # split: muxed in a single nut/matroska on stdout and re-demuxed.
        # That's complex.  Instead we'll run ONE ffmpeg per output stream;
        # cost is ~2x demux but avoids platform issues.
        # NOTE: Each FFDecoder instance handles ONE stream type.
        raise NotImplementedError

    def run(self):
        # We launch two ffmpeg subprocesses: one for video, one for audio.
        # On Windows this is the simplest robust approach.
        while not self._stop_evt.is_set() and self.state.running:
            self.state.decoder_status = "SPAWN"
            try:
                self._spawn_pair()
                self.state.decoder_status = "RUNNING"
                # Wait for both readers to finish
                for t in self._readers:
                    t.join()
                self.state.decoder_status = "EOF"
            except Exception as e:
                self.state.decoder_status = f"SPAWN_FAIL: {type(e).__name__}"
                print(f"[ffdecoder] spawn error: {e}", file=sys.stderr)
                time.sleep(1.0)
                continue

            if self._stop_evt.is_set() or not self.state.running:
                return

            # If source is a finite file, exit.
            if not self._is_live_source():
                return
            # If source is stdin, the input has been consumed; can't replay.
            if self.source in ("-", "stdin"):
                return
            self.state.ffmpeg_respawns += 1
            print(f"[ffdecoder] respawning ffmpeg (#{self.state.ffmpeg_respawns})",
                  file=sys.stderr)
            time.sleep(0.3)

    # ------------------------------------------------------------------
    def _is_live_source(self) -> bool:
        if self.source in ("-", "stdin"):
            return True
        if self.source.startswith(("http://", "https://", "udp://", "rtp://",
                                   "tcp://", "rtsp://")):
            return True
        return False

    # ------------------------------------------------------------------
    def _ffmpeg_input_args(self) -> list:
        args = [
            FFMPEG, "-hide_banner", "-loglevel", "warning",
            "-fflags", "+discardcorrupt+nobuffer+igndts",
            "-err_detect", "ignore_err",
            "-thread_queue_size", "1024",
        ]
        # If reading from stdin, tell ffmpeg
        if self.source in ("-", "stdin"):
            args += ["-f", "mpegts", "-i", "pipe:0"]
        else:
            # For local files, pace at real-time so audio buffer doesn't
            # explode and video buffer stays sized.  Live URLs already
            # come at real-time so we don't add -re for those.
            if not self.source.startswith(("http://", "https://", "udp://",
                                            "rtp://", "tcp://", "rtsp://")):
                args += ["-re"]
            args += ["-i", self.source]
        return args

    # ------------------------------------------------------------------
    def _spawn_pair(self):
        # Video subprocess: pipes BGR24 rawvideo on stdout
        vf_chain = f"scale={self.width}:{self.height}"
        if self.vf:
            vf_chain = f"{vf_chain},{self.vf}"
        v_cmd = self._ffmpeg_input_args() + [
            "-an", "-sn", "-dn",
            "-map", "0:v:0?",
            "-vf", vf_chain,
            "-pix_fmt", "bgr24",
            "-f", "rawvideo",
            "pipe:1",
        ]
        # Audio subprocess: pipes s16le @ audio_sr/channels on stdout
        a_cmd = self._ffmpeg_input_args() + [
            "-vn", "-sn", "-dn",
            "-map", "0:a:0?",
            "-ar", str(self.audio_sr),
            "-ac", str(self.audio_channels),
            "-f", "s16le",
            "pipe:1",
        ]

        # If source is stdin, we can't run two processes both reading from
        # stdin.  In that case, run one ffmpeg that pipes both via mkv on
        # stdout and a Python demuxer.  Simpler: tee the stdin to two pipes
        # via a forwarder thread.
        if self.source in ("-", "stdin"):
            # Spawn one ffmpeg that maps video AND audio into a single
            # mpeg-ts on stdout, then parse with a second ffmpeg... too
            # complex.  Use a different layout: ONE ffmpeg, reading stdin,
            # outputs raw video on fd 1 and audio in a sidecar via the
            # `nut` container on stdout.  Then we need a parser.
            # Simpler:  duplicate stdin in Python.
            # But we already are *piping into* this process; we can fan out.
            return self._spawn_pair_stdin()

        # Two normal processes
        self._proc_v = subprocess.Popen(
            v_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=0,
        )

        if not self.disable_audio:
            self._proc_a = subprocess.Popen(
                a_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=0,
            )
        else:
            self._proc_a = None

        # Track for kill
        self._proc = self._proc_v
        self._readers = []
        # Video reader
        t_v = threading.Thread(target=self._read_video,
                               args=(self._proc_v,), daemon=True,
                               name="VRead")
        t_v.start()
        self._readers.append(t_v)
        # Stderr drain (video)
        t_ve = threading.Thread(target=self._drain_stderr,
                                args=(self._proc_v, "ffmpeg-v"), daemon=True)
        t_ve.start()
        self._readers.append(t_ve)
        if self._proc_a is not None:
            t_a = threading.Thread(target=self._read_audio,
                                   args=(self._proc_a,), daemon=True,
                                   name="ARead")
            t_a.start()
            self._readers.append(t_a)
            t_ae = threading.Thread(target=self._drain_stderr,
                                    args=(self._proc_a, "ffmpeg-a"),
                                    daemon=True)
            t_ae.start()
            self._readers.append(t_ae)

    # ------------------------------------------------------------------
    def _spawn_pair_stdin(self):
        # Use a single ffmpeg on stdin that writes a `nut` container with
        # both streams.  Then parse the nut on our side via a SECOND ffmpeg
        # (process pair).  Cleaner: we fan-out stdin in Python using a
        # named pipe... too platform-specific.
        #
        # Simplest robust approach: use a single ffmpeg, write to MKV on
        # stdout, and have a second ffmpeg read it from a Python pipe.
        # But that needs a real OS pipe.  In Python, subprocess.PIPE is OK
        # for parent <-> one child.
        #
        # Even simpler: since stdin from magic_tv.py is the *re-encoded*
        # stream from a known mux, demand the user pre-mux to a single
        # output.  For clean stdin we can:
        #   1) read from stdin in Python
        #   2) write into a named pipe / temp file
        #   3) two ffmpegs read from the pipe/file
        # Or:
        #   - launch one ffmpeg with `-map 0:v -map 0:a` and use two
        #     output URLs `pipe:1` and `pipe:3`.  Python sets up fd 3 via
        #     subprocess.Popen pass_fds on Unix; on Windows this is not
        #     reliable.
        #
        # For now we punt:  stdin mode supports VIDEO ONLY.
        v_cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "warning",
            "-fflags", "+discardcorrupt+nobuffer+igndts",
            "-err_detect", "ignore_err",
            "-f", "mpegts",
            "-i", "pipe:0",
            "-an", "-sn", "-dn",
            "-map", "0:v:0?",
            "-vf", f"scale={self.width}:{self.height}",
            "-pix_fmt", "bgr24",
            "-f", "rawvideo",
            "pipe:1",
        ]
        self._proc_v = subprocess.Popen(
            v_cmd,
            stdin=sys.stdin.buffer.fileno(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._proc_a = None
        self._proc = self._proc_v

        self._readers = []
        t_v = threading.Thread(target=self._read_video,
                               args=(self._proc_v,), daemon=True,
                               name="VRead")
        t_v.start()
        self._readers.append(t_v)
        t_ve = threading.Thread(target=self._drain_stderr,
                                args=(self._proc_v, "ffmpeg-v"),
                                daemon=True)
        t_ve.start()
        self._readers.append(t_ve)
        print("[ffdecoder] stdin mode: VIDEO ONLY (no audio).",
              file=sys.stderr)

    # ------------------------------------------------------------------
    def _drain_stderr(self, proc, tag):
        try:
            for line in iter(proc.stderr.readline, b""):
                if not line:
                    break
                # Throttle: only print first 5 occurrences of each unique line
                # to avoid log spam from corrupt streams
                msg = line.decode("utf-8", "replace").rstrip()
                # We just drop most stderr; uncomment for debug:
                # print(f"[{tag}] {msg}", file=sys.stderr)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _read_video(self, proc):
        frame_bytes = self.width * self.height * 3
        buf = bytearray()
        try:
            while not self._stop_evt.is_set() and self.state.running:
                # Read in chunks
                chunk = proc.stdout.read(frame_bytes - len(buf))
                if not chunk:
                    break
                self.state.bytes_in_video += len(chunk)
                self.state.last_video_byte_at = time.monotonic()
                buf.extend(chunk)
                while len(buf) >= frame_bytes:
                    arr = np.frombuffer(bytes(buf[:frame_bytes]), dtype=np.uint8)
                    img = arr.reshape(self.height, self.width, 3)
                    self.video_buf.put(VideoFrame(img=img))
                    self.state.video_frames_decoded += 1
                    del buf[:frame_bytes]
        except Exception as e:
            print(f"[vread] {e}", file=sys.stderr)

    # ------------------------------------------------------------------
    def _read_audio(self, proc):
        # s16le, audio_channels, audio_sr
        chunk_samples = self.audio_sr // 50  # 20 ms
        bytes_per_sample = 2 * self.audio_channels
        chunk_bytes = chunk_samples * bytes_per_sample
        try:
            while not self._stop_evt.is_set() and self.state.running:
                data = proc.stdout.read(chunk_bytes)
                if not data:
                    break
                self.state.bytes_in_audio += len(data)
                self.state.last_audio_byte_at = time.monotonic()
                if len(data) % bytes_per_sample != 0:
                    # truncate to whole samples
                    data = data[:-(len(data) % bytes_per_sample)]
                if not data:
                    continue
                arr = np.frombuffer(data, dtype=np.int16)
                # convert to float32 [-1, 1]
                f = arr.astype(np.float32) / 32768.0
                f = f.reshape(-1, self.audio_channels)
                self.audio_buf.put(AudioFrame(samples=f))
                self.state.audio_chunks_decoded += 1
        except Exception as e:
            print(f"[aread] {e}", file=sys.stderr)


# ----------------------------------------------------------------------------
# Audio output
# ----------------------------------------------------------------------------
class AudioPlayer:
    def __init__(self, audio_buf: RingBuffer, state: PlayerState,
                 sr: int = 48000, channels: int = 2):
        self.audio_buf = audio_buf
        self.state = state
        self.sr = sr
        self.channels = channels
        self._tail = np.zeros((0, channels), dtype=np.float32)
        self._stream = None

    def start(self):
        if sd is None:
            print("[audio] sounddevice unavailable; muted", file=sys.stderr)
            return
        try:
            self._stream = sd.OutputStream(
                samplerate=self.sr,
                channels=self.channels,
                dtype="float32",
                latency="low",
                callback=self._cb,
                blocksize=1024,
            )
            self._stream.start()
        except Exception as e:
            print(f"[audio] open failed: {e}", file=sys.stderr)
            self._stream = None

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass

    def _cb(self, outdata, frames, time_info, status):
        out = np.zeros((frames, self.channels), dtype=np.float32)
        i = 0
        if len(self._tail) > 0:
            n = min(frames, len(self._tail))
            out[i:i+n] = self._tail[:n]
            self._tail = self._tail[n:]
            i += n
        while i < frames:
            af = self.audio_buf.pop_oldest()
            if af is None:
                self.state.audio_underruns += 1
                break  # silence pad rest
            need = frames - i
            avail = len(af.samples)
            n = min(need, avail)
            out[i:i+n] = af.samples[:n]
            i += n
            if avail > n:
                self._tail = af.samples[n:]
        outdata[:] = out


# ----------------------------------------------------------------------------
# Display
# ----------------------------------------------------------------------------
class Display:
    def __init__(self, video_buf: RingBuffer, audio_buf: RingBuffer,
                 state: PlayerState,
                 width: int, height: int,
                 target_fps: float = 30.0,
                 stale_ms: float = 100.0,
                 dry_ms: float = 1500.0,
                 use_interp: bool = False,
                 window: str = "Magic Player"):
        self.video_buf = video_buf
        self.audio_buf = audio_buf
        self.state = state
        self.width = width
        self.height = height
        self.target_fps = target_fps
        self.stale_ms = stale_ms
        self.dry_ms = dry_ms
        self.use_interp = use_interp
        self.window = window
        self._last: Optional[VideoFrame] = None
        self._prev: Optional[VideoFrame] = None
        self._fps_window = collections.deque(maxlen=60)
        self.snapshot_every = 0.0
        self.max_seconds = 0.0
        self._t0 = time.monotonic()
        self._next_snapshot = 0.0
        self._snapshot_idx = 0

    def run(self):
        period = 1.0 / self.target_fps
        next_t = time.monotonic()
        while self.state.running:
            now = time.monotonic()
            if now < next_t:
                time.sleep(min(0.005, next_t - now))
                continue
            next_t += period
            if next_t < now - period * 5:
                next_t = now + period

            # Pop oldest frame so we play every frame in order, not just
            # the latest.  If the buffer is dangerously deep (latency
            # creeping up), drop frames to catch up.
            new = None
            buf_n = len(self.video_buf)
            if buf_n > 60:
                # >2 seconds of backlog -> drop until <=30 (1 sec)
                while len(self.video_buf) > 30:
                    self.video_buf.pop_oldest()
                new = self.video_buf.pop_oldest()
            elif buf_n > 0:
                new = self.video_buf.pop_oldest()
            if new is not None:
                self._prev = self._last
                self._last = new
                self.state.video_frames_displayed += 1

            img = self._compose(now)
            self._fps_window.append(now)

            # Snapshot for testing
            if self.snapshot_every > 0 and \
                    (now - self._t0) >= self._next_snapshot and \
                    cv2 is not None:
                fname = f"magic_player_snap_{self._snapshot_idx:03d}.png"
                cv2.imwrite(fname, img)
                self._snapshot_idx += 1
                self._next_snapshot += self.snapshot_every

            # Auto-quit
            if self.max_seconds > 0 and (now - self._t0) >= self.max_seconds:
                self.state.running = False

            if cv2 is not None:
                cv2.imshow(self.window, img)
                k = cv2.waitKey(1) & 0xFF
                self._key(k)

    def _compose(self, now: float) -> np.ndarray:
        idle_v_ms = (now - self.state.last_video_byte_at) * 1000.0
        if self._last is None:
            self.state.waiting = True
            return self._stats(self._blank("WAITING FOR STREAM..."), now)

        age_ms = (now - self._last.received_at) * 1000.0
        self.state.last_frame_age_ms = age_ms
        stale = age_ms > self.stale_ms
        if stale and self.use_interp and self._prev is not None and \
                self._prev.img.shape == self._last.img.shape:
            img = cv2.addWeighted(self._prev.img, 0.5, self._last.img, 0.5, 0)
        else:
            img = self._last.img.copy()

        if idle_v_ms > self.dry_ms:
            self.state.waiting = True
            img = (img * 0.45).astype(np.uint8)
            img = self._banner(img, "WAITING FOR STREAM",
                               color=(0, 165, 255))
        elif stale:
            self.state.waiting = False
            img = self._banner(img, f"RECOVERING (held {age_ms:.0f} ms)",
                               color=(0, 200, 255))
        else:
            self.state.waiting = False

        return self._stats(img, now)

    def _blank(self, msg):
        img = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        if cv2 is not None:
            (tw, th), _ = cv2.getTextSize(msg, cv2.FONT_HERSHEY_SIMPLEX,
                                          1.0, 2)
            cv2.putText(img, msg,
                        ((self.width - tw)//2, (self.height + th)//2),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 165, 255), 2,
                        cv2.LINE_AA)
        return img

    def _banner(self, img, text, color=(0, 200, 255)):
        if cv2 is None:
            return img
        h, w = img.shape[:2]
        strip = img.copy()
        cv2.rectangle(strip, (0, 0), (w, 36), (0, 0, 0), -1)
        img = cv2.addWeighted(strip, 0.55, img, 0.45, 0)
        cv2.putText(img, text, (10, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2, cv2.LINE_AA)
        return img

    def _stats(self, img, now):
        if not self.state.show_stats or cv2 is None:
            return img
        if len(self._fps_window) >= 2:
            span = self._fps_window[-1] - self._fps_window[0]
            fps = (len(self._fps_window) - 1) / span if span > 0 else 0.0
        else:
            fps = 0.0
        st = self.state
        idle_v = (now - st.last_video_byte_at) * 1000.0
        idle_a = (now - st.last_audio_byte_at) * 1000.0
        lines = [
            f"render fps      : {fps:5.1f}",
            f"decoder         : {st.decoder_status}",
            f"vid dec / disp  : {st.video_frames_decoded:6d}/{st.video_frames_displayed:6d}",
            f"aud chunks dec  : {st.audio_chunks_decoded:6d}",
            f"v-buf / a-buf   : {len(self.video_buf):3d}/{len(self.audio_buf):3d}",
            f"frame age (ms)  : {st.last_frame_age_ms:5.0f}",
            f"v idle (ms)     : {idle_v:5.0f}",
            f"a idle (ms)     : {idle_a:5.0f}",
            f"v bytes in      : {st.bytes_in_video:,}",
            f"a bytes in      : {st.bytes_in_audio:,}",
            f"audio underrun  : {st.audio_underruns}",
            f"ffmpeg respawns : {st.ffmpeg_respawns}",
            f"interp          : {'on' if st.interp_enabled else 'off'}",
        ]
        h, w = img.shape[:2]
        pw = 290
        ph = 21 * len(lines) + 10
        x0 = w - pw - 10
        y0 = 10
        sub = img[y0:y0+ph, x0:x0+pw].copy()
        cv2.rectangle(sub, (0, 0), (pw, ph), (0, 0, 0), -1)
        img[y0:y0+ph, x0:x0+pw] = cv2.addWeighted(
            sub, 0.6, img[y0:y0+ph, x0:x0+pw], 0.4, 0)
        for i, ln in enumerate(lines):
            cv2.putText(img, ln, (x0+8, y0+18+i*21),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (220, 220, 220), 1,
                        cv2.LINE_AA)
        return img

    def _key(self, k):
        if k == 0xFF:
            return
        if k in (ord('q'), 27):
            self.state.running = False
        elif k == ord('s'):
            self.state.show_stats = not self.state.show_stats
        elif k == ord('i'):
            self.use_interp = not self.use_interp
            self.state.interp_enabled = self.use_interp


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description="Magic resilient video player")
    parser.add_argument("source", help="path/URL/- (stdin)")
    parser.add_argument("--width", type=int, default=0,
                        help="Force scaled output width (0=auto)")
    parser.add_argument("--height", type=int, default=0,
                        help="Force scaled output height (0=auto)")
    parser.add_argument("--fps", type=float, default=30.0,
                        help="Display fps")
    parser.add_argument("--audio-sr", type=int, default=48000,
                        help="Audio output sample rate")
    parser.add_argument("--audio-ch", type=int, default=2,
                        help="Audio output channels (1 or 2)")
    parser.add_argument("--no-audio", action="store_true",
                        help="Disable audio")
    parser.add_argument("--interp", action="store_true",
                        help="Enable simple linear interpolation on stale frames")
    parser.add_argument("--stale-ms", type=float, default=100.0)
    parser.add_argument("--dry-ms", type=float, default=1500.0)
    parser.add_argument("--video-buf", type=int, default=240)
    parser.add_argument("--audio-buf", type=int, default=200)
    parser.add_argument("--headless", action="store_true",
                        help="No window; print stats")
    parser.add_argument("--probe-timeout", type=float, default=12.0)
    parser.add_argument("--snapshot-every", type=float, default=0.0,
                        help="Save the rendered frame every N seconds to "
                             "magic_player_snap_*.png (for testing)")
    parser.add_argument("--max-seconds", type=float, default=0.0,
                        help="Auto-quit after N wall-clock seconds (0 = forever)")
    args = parser.parse_args(argv)

    if not os.path.exists(FFMPEG):
        print(f"FATAL: ffmpeg not found at {FFMPEG}", file=sys.stderr)
        return 2

    state = PlayerState()
    state.interp_enabled = args.interp

    # Probe (skip for stdin)
    v_info = None
    a_info = None
    if args.source not in ("-", "stdin"):
        v_info, a_info = probe_streams(args.source, timeout=args.probe_timeout)
        if v_info is None:
            print("[main] probe could not determine video size; "
                  "defaulting to 854x480 30fps", file=sys.stderr)
        if a_info is None and not args.no_audio:
            print("[main] probe could not determine audio params; "
                  "defaulting 48000/2", file=sys.stderr)

    # Resolve dimensions
    if args.width and args.height:
        out_w, out_h = args.width, args.height
    elif v_info is not None:
        out_w, out_h = v_info[0], v_info[1]
    else:
        out_w, out_h = 854, 480

    fps = v_info[2] if v_info is not None else args.fps
    sr = a_info[0] if a_info is not None else args.audio_sr
    ch = a_info[1] if a_info is not None else args.audio_ch
    if ch > 2:
        ch = 2  # downmix to stereo

    print(f"[main] source={args.source}", file=sys.stderr)
    print(f"[main] video {out_w}x{out_h} @ {fps:.2f} fps, audio {sr}/{ch}",
          file=sys.stderr)

    video_buf = RingBuffer(args.video_buf)
    audio_buf = RingBuffer(args.audio_buf)

    decoder = FFDecoder(
        source=args.source,
        width=out_w, height=out_h, fps=fps,
        audio_sr=sr, audio_channels=ch,
        video_buf=video_buf, audio_buf=audio_buf,
        state=state,
        disable_audio=args.no_audio,
    )
    decoder.start()

    audio = None
    if not args.no_audio:
        audio = AudioPlayer(audio_buf, state, sr=sr, channels=ch)
        audio.start()

    def _stop(*_):
        state.running = False
    try:
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
    except Exception:
        pass

    display = Display(
        video_buf, audio_buf, state,
        width=out_w, height=out_h,
        target_fps=args.fps,
        stale_ms=args.stale_ms,
        dry_ms=args.dry_ms,
        use_interp=args.interp,
    )
    display.snapshot_every = args.snapshot_every
    display.max_seconds = args.max_seconds

    if args.headless or cv2 is None:
        try:
            t0 = time.monotonic()
            eof_seen_at = None
            while state.running:
                time.sleep(1.0)
                age = state.last_frame_age_ms
                idle = (time.monotonic() - state.last_video_byte_at) * 1000
                print(f"[t={time.monotonic()-t0:6.1f}s] "
                      f"st={state.decoder_status:9s} "
                      f"v={state.video_frames_decoded:5d} "
                      f"a={state.audio_chunks_decoded:5d} "
                      f"vbuf={len(video_buf):3d} abuf={len(audio_buf):3d} "
                      f"age={age:5.0f}ms idle={idle:5.0f}ms "
                      f"resp={state.ffmpeg_respawns} "
                      f"under={state.audio_underruns} "
                      f"vbytes={state.bytes_in_video:,}")
                if state.decoder_status == "EOF":
                    if eof_seen_at is None:
                        eof_seen_at = time.monotonic()
                    elif time.monotonic() - eof_seen_at > 3.0:
                        break
        except KeyboardInterrupt:
            pass
    else:
        try:
            display.run()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

    state.running = False
    decoder.stop()
    if audio is not None:
        audio.stop()
    decoder.join(timeout=2.0)
    # If decoder is still alive (ffmpeg pipes blocked), force exit:
    if decoder.is_alive():
        os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
