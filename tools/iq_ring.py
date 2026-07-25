"""iq_ring.py — the Glitch Specimen Recorder's memory.

A GNU Radio sink that keeps the last N seconds of IQ in a RAM ring
(stored cs16 — half the bytes of cf32) while the chain runs. When a
trigger file appears (dropped by specimen_watch.py, or by hand), it
snapshots the ring and writes a specimen: the raw IQ of the seconds
surrounding a glitch, plus a meta json. Transient failures become
reproducible lab specimens.

Zero cost when unused: tv_live.py only instantiates this when
STVT_IQ_RING=<seconds> is set. Trigger: <STVT_IQ_RING_DIR>/TRIGGER
(file contents = reason text, recorded in the meta).
"""
import json
import os
import threading
import time
from pathlib import Path

import numpy as np
from gnuradio import gr

COOLDOWN_S = 30            # min gap between specimens (disk guard)
CHECK_EVERY_ITEMS = 2**18  # trigger-file poll cadence (~0.1 s of samples)


class iq_ring_sink(gr.sync_block):
    def __init__(self, sample_rate, secs, out_dir, scale=1.0, meta=None):
        gr.sync_block.__init__(self, name="iq_ring_sink",
                               in_sig=[np.complex64], out_sig=None)
        self.rate = float(sample_rate)
        self.n = int(sample_rate * secs)
        # raw complex64 (2x the cs16 RAM) so work() is a pure memcpy —
        # the int16 conversion happens on the dump writer thread only
        self.ring = np.zeros(self.n, dtype=np.complex64)
        self.widx = 0                                     # write index (samples)
        self.filled = 0
        self.scale = float(scale)   # multiply floats -> int16 range
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.trigger = self.dir / "TRIGGER"
        self.meta = dict(meta or {})
        self.meta.update({"sample_rate": self.rate, "ring_secs": secs,
                          "format": "cs16 interleaved"})
        self._since_check = 0
        self._last_dump = 0.0
        self._t0 = time.time()

    def work(self, input_items, output_items):
        # HOT PATH = PURE MEMCPY (2026-07-07 regression fix): the old
        # per-chunk float->int16 conversion ran inside the GR scheduler
        # and stalled the chain in transients — live ABBA A/B measured
        # bad packets 4.3% (ring on) vs 2.5% (off) on a HEALTHY channel.
        # Store raw complex64 (2x RAM), convert only at snapshot time on
        # the writer thread.
        x = input_items[0]
        m = len(x)
        w = self.widx % self.n
        first = min(m, self.n - w)
        self.ring[w:w + first] = x[:first]
        rest = m - first
        if rest > 0:                       # wrap (rest < n by construction
            rest = rest % self.n           # for absurdly large work calls)
            self.ring[:rest] = x[m - rest:]
        self.widx = (self.widx + m) % self.n
        self.filled = min(self.filled + m, self.n)

        self._since_check += m
        if self._since_check >= CHECK_EVERY_ITEMS:
            self._since_check = 0
            if self.trigger.exists() and \
                    time.time() - self._last_dump > COOLDOWN_S:
                self._dump()
        return m

    def _dump(self):
        self._last_dump = time.time()
        # stale trigger = left over from a previous (dead) chain, aimed
        # at a different channel/moment — discard, don't dump garbage
        try:
            if time.time() - self.trigger.stat().st_mtime > 20:
                self.trigger.unlink()
                print("[iq_ring] stale trigger discarded", flush=True)
                return
        except OSError:
            return
        try:
            reason = self.trigger.read_text(errors="ignore").strip()
        except OSError:
            reason = "unknown"
        # snapshot in chronological order, then write on a side thread so
        # work() never blocks on disk
        w = self.widx
        if self.filled < self.n:
            snap_c = self.ring[:self.filled].copy()
        else:
            snap_c = np.concatenate((self.ring[w:], self.ring[:w]))
        stamp = time.strftime("%Y%m%d_%H%M%S")
        meta = dict(self.meta)
        meta.update({"reason": reason, "stamp": stamp,
                     "chain_uptime_s": round(time.time() - self._t0, 1),
                     "samples": int(len(snap_c))})
        try:
            self.trigger.unlink()
        except OSError:
            pass

        def writer():
            # cs16 conversion lives HERE, off the flowgraph thread
            iq = np.empty(2 * len(snap_c), dtype=np.float32)
            iq[0::2] = snap_c.real
            iq[1::2] = snap_c.imag
            np.multiply(iq, self.scale, out=iq)
            np.clip(iq, -32767, 32767, out=iq)
            snap = iq.astype(np.int16)
            base = self.dir / f"specimen_{stamp}"
            snap.tofile(str(base) + ".cs16")
            (Path(str(base) + ".json")).write_text(json.dumps(meta, indent=1))
            print(f"[iq_ring] SPECIMEN {base.name}.cs16 "
                  f"({meta['samples']} samples, reason: {reason})",
                  flush=True)

        threading.Thread(target=writer, daemon=True).start()
