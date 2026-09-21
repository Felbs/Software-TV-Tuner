#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""Runs a grcc-generated flowgraph from this source tree (before `make install`), with the
pure-Python blocks grafted on, a site radio lock honoured, and - for a Qt flowgraph - an
unattended run of N seconds ending in a window screenshot.

  python run_grc.py build/grc/atsc1_replay.py -c capture.cf32 -o out.ts
  python run_grc.py build/grc/atsc1_live_qt.py --seconds 60 --png shot.png -- -f <Hz> -a "<port>" -g 30 \\
         [--call "src.set_gain(0,'IFGR',46)"]

Everything after `--` is passed to the flowgraph's own command line. `--call` runs a method
on a block after construction (device-specific gain elements, for instance).
If the radio is shared, RXTUNE_LOCK (see gr-rxtune) names a site lock module and is honoured."""
import argparse
import contextlib
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _devpath  # noqa: E402,F401


def site_lock(owner):
    if not os.environ.get("RXTUNE_LOCK"):
        return contextlib.nullcontext(None)
    from rxtune import lock
    return lock.from_env(owner=owner, priority=60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("module")
    ap.add_argument("--seconds", type=float, default=0.0, help="Qt: stop after this long (0 = until closed)")
    ap.add_argument("--png")
    ap.add_argument("--size", default="1500x1000")
    ap.add_argument("--call", action="append", default=[], metavar="BLOCK.METHOD(ARGS)")
    a, rest = ap.parse_known_args()
    if rest and rest[0] == "--":
        rest = rest[1:]

    spec = importlib.util.spec_from_file_location("fg", a.module)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    name = os.path.splitext(os.path.basename(a.module))[0]
    cls = getattr(mod, name)
    sys.argv = [a.module] + rest
    options = mod.argument_parser().parse_args(rest)
    kw = {k: v for k, v in vars(options).items()}
    qt = hasattr(mod, "Qt")

    with site_lock("atscplus-grc") as lk:
        if not qt:
            tb = cls(**kw)
            for call in a.call:
                eval("tb." + call, {"tb": tb})                     # noqa: S307  (the operator's own command line)
            tb.start()
            tb.wait()
            return 0
        from PyQt5 import Qt, QtCore
        app = Qt.QApplication(sys.argv[:1])
        tb = cls(**kw)
        for call in a.call:
            eval("tb." + call, {"tb": tb})                         # noqa: S307
        w, h = (int(x) for x in a.size.split("x"))
        tb.resize(w, h)
        tb.start()
        tb.show()
        t0 = time.time()

        def finish():
            timer.stop()
            if a.png:
                g = tb.frameGeometry()          # the SCREEN region: a widget grab cannot see a native video surface
                app.primaryScreen().grabWindow(0, g.x(), g.y(), g.width(), g.height()).save(a.png)
            tb.stop()
            tb.wait()
            app.quit()

        def tick():
            if lk is not None:
                lk.heartbeat()
                if lk.should_yield():
                    finish()
                    return
            if a.seconds and time.time() - t0 > a.seconds:
                finish()

        timer = QtCore.QTimer()
        timer.timeout.connect(tick)
        timer.start(500)
        app.aboutToQuit.connect(lambda: (tb.stop(), tb.wait()))
        app.exec_()
    p = getattr(tb, "panel", None)
    if p is not None:
        st = dict(p.stats)
        print(f"ran {time.time() - t0:.0f} s: MER {p.mer if p.mer is None else round(p.mer, 1)} dB, "
              f"TS packets {st.get('packets', 0)}, uncorrectable {st.get('scrubbed', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
