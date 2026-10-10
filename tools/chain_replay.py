#!/usr/bin/env python3
"""Run tv_live.py's REAL chain from an IQ capture instead of the SDR.

`LiveTVTopBlock` builds its own `soapy.source`; this module hands it a file-backed stand-in
with the same handful of methods (set_sample_rate, set_gain, write_setting, ...) as no-ops,
so the exact block chain the user runs on air - every STVT_* lever included - decodes a
capture deterministically. Two tools sit on it: `profile_chain.py` (where the CPU goes) and
`gate_ts.py` (the decoded TS bytes against an oracle).

Captures: .cs16 (interleaved int16 I/Q, the format tv_tuner's IQ ring and the scanner
write) or .cf32 (interleaved float32), at the chain's native rate (8 MS/s by default).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from gnuradio import gr, blocks  # noqa: E402

import tv_live  # noqa: E402  (the chain under test; its main() only runs as __main__)


class _Range:
    def __init__(self, lo=0.0, hi=0.0):
        self._lo, self._hi = lo, hi

    def minimum(self):
        return self._lo

    def maximum(self):
        return self._hi


class FileSource(gr.hier_block2):
    """A capture pretending to be gr-soapy's source: fc32 at +-1.0, settings ignored."""

    def __init__(self, path: Path, repeat: bool = False):
        gr.hier_block2.__init__(self, "capture_source",
                                gr.io_signature(0, 0, 0),
                                gr.io_signature(1, 1, gr.sizeof_gr_complex))
        self.path = Path(path)
        if self.path.suffix.lower() == ".cf32":
            src = blocks.file_source(gr.sizeof_gr_complex, str(self.path), repeat)
            self.connect(src, self)
            self.samples = self.path.stat().st_size // 8
        else:
            src = blocks.file_source(gr.sizeof_short, str(self.path), repeat)
            s2c = blocks.interleaved_short_to_complex(False, False, 32768.0)
            self.connect(src, s2c, self)
            self.samples = self.path.stat().st_size // 4
        self.settings = {}

    # the soapy.source surface tv_live touches; all recorded, none acted on
    def set_sample_rate(self, chan, rate):
        self.settings["sample_rate"] = rate

    def set_frequency(self, chan, freq):
        self.settings["frequency"] = freq

    def set_antenna(self, chan, name):
        self.settings["antenna"] = name

    def set_gain_mode(self, chan, agc):
        self.settings["agc"] = agc

    def set_gain(self, chan, *args):
        self.settings["gain" if len(args) == 1 else args[0]] = args[-1]

    def set_bandwidth(self, chan, bw):
        self.settings["bandwidth"] = bw

    def write_setting(self, key, value):
        self.settings[key] = value

    def get_gain_range(self, chan, name=None):
        return _Range(0.0, 59.0)

    def set_min_output_buffer(self, n):
        pass


def build(iq_path, ts_path, rf: int = 36, native_rate: float | None = None):
    """-> (top block, [(name, block), ...] in connection order). The chain is tv_live's own."""
    iq_path = Path(iq_path)
    src = FileSource(iq_path)
    seen, order = {}, []

    def remember(obj):
        blk = obj[0] if isinstance(obj, tuple) else obj
        key = id(blk)
        if key not in seen and blk is not None and not isinstance(blk, gr.top_block):
            seen[key] = True
            order.append(blk)

    real_connect = tv_live.LiveTVTopBlock.connect
    real_source = tv_live.soapy.source
    # the matched filter hides inside gr-dtv's atsc_rx_filter hier block, where connect()
    # never sees it; catch the resampler at its factory so it gets a row of its own
    import gnuradio.filter as gfilt
    inner = []
    real_factories = {n: getattr(gfilt, n) for n in ("pfb_arb_resampler_ccf", "rational_resampler_ccc")}

    def recording(name):
        def make(*a, **k):
            blk = real_factories[name](*a, **k)
            inner.append((name, blk))
            return blk
        return make

    def connect(self, *args):
        for a in args:
            remember(a)
        return real_connect(self, *args)

    tv_live.soapy.source = lambda *a, **k: src
    tv_live.LiveTVTopBlock.connect = connect
    for n in real_factories:
        setattr(gfilt, n, recording(n))
    try:
        tb = tv_live.LiveTVTopBlock(rf, Path(ts_path))
    finally:
        tv_live.soapy.source = real_source
        tv_live.LiveTVTopBlock.connect = real_connect
        for n, f in real_factories.items():
            setattr(gfilt, n, f)
    for name, blk in inner:
        if id(blk) not in seen:                               # made inside a hier block
            seen[id(blk)] = True
            order.append(blk)
    rate = native_rate or src.settings.get("sample_rate") or tv_live.ATSC_NATIVE_SAMPLE_RATE
    tb.capture = src
    tb.air_seconds = src.samples / float(rate)
    named = []
    for blk in order:
        if blk is src:
            name = "capture source"
        else:
            try:
                name = blk.name()
            except Exception:                               # a hier block or a python block
                name = type(blk).__name__
        named.append((name, blk))
    tb.named_blocks = named
    return tb


def run(tb):
    """Run to end of capture; -> (wall seconds, process CPU seconds)."""
    c0 = os.times()
    t0 = time.perf_counter()
    tb.run()
    wall = time.perf_counter() - t0
    c1 = os.times()
    cpu = (c1.user - c0.user) + (c1.system - c0.system)
    return wall, cpu


def ts_packets(path):
    """The TS as a list of 188-byte packets (a tail shorter than one packet is dropped)."""
    data = Path(path).read_bytes()
    return [data[i:i + 188] for i in range(0, len(data) - 187, 188)]
