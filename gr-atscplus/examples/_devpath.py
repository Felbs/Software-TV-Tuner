# SPDX-License-Identifier: GPL-3.0-or-later
"""Run from the source tree before `make install`: grafts this tree's pure-Python blocks onto the
INSTALLED gnuradio.atscplus package (the compiled blocks stay the installed ones)."""
import importlib
import os

from gnuradio import atscplus

_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python", "atscplus")
if _py not in atscplus.__path__:
    atscplus.__path__.append(_py)
for _name in ("tei_scrub", "eq_probe"):
    if not hasattr(atscplus, _name):
        setattr(atscplus, _name, getattr(importlib.import_module("gnuradio.atscplus." + _name), _name))
atscplus.dd_mer_db = importlib.import_module("gnuradio.atscplus.eq_probe").dd_mer_db


def __getattr_qt(name):
    if name in ("vsb_panel", "video_pane"):
        cls = getattr(importlib.import_module("gnuradio.atscplus." + name), name)
        setattr(atscplus, name, cls)
        return cls
    raise AttributeError(name)


if not hasattr(atscplus, "__getattr__"):
    atscplus.__getattr__ = __getattr_qt
