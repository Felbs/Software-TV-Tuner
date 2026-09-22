#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Video Pane (Qt): the television picture, inside the flowgraph's own window.

The flowgraph sends its transport stream to a local UDP port (stock UDP Sink, 1316-byte
payloads = 7 TS packets); this pane hosts a player (mpv) that reads that port and draws
into the pane (`--wid`). Picture, sound and captions are the player's business - an ATSC 1.0
transport stream is already a finished multiplex (MPEG-2/H.264 video, AC-3 audio).

A multi-programme stream: choose with `vid` / `aid` (mpv track numbers; 0 = the player's
choice), or press `_` / `#` in the pane to cycle video / audio tracks.

The player is respawned if it exits, at most `max_restarts` times - a player that cannot
start must not become a process storm."""
import shutil
import subprocess
import time

from gnuradio import gr
from PyQt5 import QtCore, QtWidgets


class video_pane(gr.basic_block, QtWidgets.QWidget):
    def __init__(self, url="udp://127.0.0.1:5004?fifo_size=1000000&overrun_nonfatal=1",
                 player="mpv", vid=0, aid=0, player_args="", start_delay_s=3.0, max_restarts=5,
                 parent=None):
        gr.basic_block.__init__(self, name="atscplus_video_pane", in_sig=None, out_sig=None)
        QtWidgets.QWidget.__init__(self, parent)
        self.url, self.player = url, player
        self.vid, self.aid = int(vid), int(aid)
        self.player_args = player_args
        self.max_restarts = int(max_restarts)
        self.proc = None
        self.n_starts = 0
        self._stopping = False
        self._t_start = 0.0
        self.setMinimumSize(640, 360)
        self.setAttribute(QtCore.Qt.WA_NativeWindow, True)       # the player needs a real window handle
        self.setStyleSheet("background: black;")
        lay = QtWidgets.QVBoxLayout(self)
        self.note = QtWidgets.QLabel("waiting for the transport stream ...")
        self.note.setStyleSheet("color: #9aa; background: transparent;")
        self.note.setAlignment(QtCore.Qt.AlignCenter)
        lay.addWidget(self.note)
        QtCore.QTimer.singleShot(int(float(start_delay_s) * 1000), self._spawn)
        self.watch = QtCore.QTimer(self)
        self.watch.timeout.connect(self._check)
        self.watch.start(2000)

    def _command(self):
        exe = shutil.which(self.player) or self.player
        cmd = [exe, f"--wid={int(self.winId())}", "--no-border", "--no-osc", "--keepaspect=yes",
               "--force-window=yes", "--profile=low-latency", "--cache=yes", "--demuxer-max-bytes=64MiB",
               "--demuxer-lavf-analyzeduration=3", "--idle=yes", "--loop-playlist=inf", "--really-quiet"]
        # --idle / --loop-playlist: a broken stream (a tuning loop's deaf cells) ends the "file";
        # instead of exiting, the player reopens the URL and waits for packets
        if self.vid:
            cmd.append(f"--vid={self.vid}")
        if self.aid:
            cmd.append(f"--aid={self.aid}")
        return cmd + self.player_args.split() + [self.url]

    def _spawn(self):
        if self._stopping or self.proc is not None:
            return
        if self.n_starts > self.max_restarts:
            self.note.setText(f"the player exited {self.n_starts} times - giving up")
            return
        try:
            self.proc = subprocess.Popen(self._command(), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            self.note.setText(f"cannot start '{self.player}': {e}\n(the stream is still on {self.url})")
            self.n_starts = self.max_restarts + 1
            return
        self.n_starts += 1
        self._t_start = time.time()
        self.note.setText("")

    def _check(self):
        if self.proc is not None and self.proc.poll() is not None and not self._stopping:
            self.proc = None
            if time.time() - self._t_start > 20:       # it ran for a while: that was not a start-up failure
                self.n_starts = 0
            self.note.setText("player exited - restarting")
            QtCore.QTimer.singleShot(2000, self._spawn)
        elif self.proc is None and not self._stopping and self.n_starts > self.max_restarts:
            # A tuning loop shatters the stream on purpose (deaf cells); the player gives up, and
            # the budget must come back once the stream is healthy again - so retry, slowly.
            if time.time() - self._t_start > 30:
                self.n_starts = self.max_restarts
                self._t_start = time.time()
                self._spawn()

    def _kill(self):
        self._stopping = True
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()

    def stop(self):
        self._kill()
        return True

    def closeEvent(self, ev):
        self._kill()
        super().closeEvent(ev)
