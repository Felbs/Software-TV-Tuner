"""telemetry_check.py — run the REAL consumers' regexes against a chain log
from the speed-1 build and assert every load-bearing telemetry line still
parses. Renaming or reformatting an emitter is the #1 way to silently darken
a dial, so this is a gate, not a courtesy.

Every pattern below is copied verbatim from the tool that owns it:
  adaptive-tv/tv_tuna_panel.py  RE_FS RE_FPLL RE_RS5 RE_CIR, rs_turbo, OsO
  tools/quality_tuner.py        RELOCKS_RE ALIGNED_RE
  lab/wl_live_gate2.py          the OsO / overflow counter
  lab/day_program_729.py        the in_rms split and the ffmpeg frame= regex
  tools/tv_dual.py              _RE_LONG (on stvt-2.0-wl; the tag it needs)
  tools/stvt_docs_guard.py      the four contract tags (run separately)

Usage: python lab/speed_build/telemetry_check.py <chain.log> [more logs...]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# ── verbatim from adaptive-tv/tv_tuna_panel.py ───────────────────────────
RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
RE_FPLL = re.compile(r"mean\|x\|=([\d.]+).*?max\|x\|=([\d.]+)\s+in_rms=([\d.]+)")
RE_RS5 = re.compile(r"last5s: pkts=(\d+) era_dec=\d+ era_ok=\d+ bad=(\d+)")
RE_CIR = re.compile(r"\[cir t=[\d.]+\] (.+)")
RE_TURBO = re.compile(r"\[rs_turbo[^\n]*att=(\d+) retry=\d+ resc=(\d+)")
# ── verbatim from tools/quality_tuner.py ─────────────────────────────────
RELOCKS_RE = re.compile(r"sync_soft FINAL.*relocks=(\d+)")
ALIGNED_RE = re.compile(r"sync_soft FINAL.*segs_aligned=\d+ \(([\d.]+)%\)")
# ── verbatim from lab/wl_live_gate2.py ───────────────────────────────────
RE_OSO = re.compile(r"\bOs?O\b|overflow", re.I)
# ── verbatim from tools/tv_dual.py (stvt-2.0-wl) ─────────────────────────
RE_DUAL_LONG = re.compile(
    r"\[eq-long t=\s*([\d.]+)s\] fs=(\d+) fs_err_rms=([\d.]+)")

# name -> (pattern, required?)  required = the dial goes dark without it
CHECKS = [
    ("panel RE_FS (the MER dial)", RE_FS, True),
    ("panel RE_FPLL (in_rms / max|x|)", RE_FPLL, True),
    ("panel RE_RS5 (loss %)", RE_RS5, False),
    ("panel RE_CIR (echo viewer)", RE_CIR, False),
    ("panel rs_turbo (rescue counter)", RE_TURBO, False),
    ("quality_tuner RELOCKS_RE", RELOCKS_RE, True),
    ("quality_tuner ALIGNED_RE", ALIGNED_RE, True),
    ("tv_dual _RE_LONG (paired MER series)", RE_DUAL_LONG, True),
]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    bad = 0
    for arg in sys.argv[1:]:
        p = Path(arg)
        txt = p.read_text(errors="replace")
        print(f"\n=== {p.name} ({len(txt)} bytes) ===")
        for name, rx, required in CHECKS:
            hits = rx.findall(txt)
            state = "OK " if hits else ("BREAK" if required else "absent")
            if not hits and required:
                bad += 1
            sample = ""
            if hits:
                sample = f"  first={hits[0]!r}  n={len(hits)}"
            print(f"  [{state:>6}] {name}{sample}")
        # day_program_729's in_rms extraction (a split(), not a regex)
        rms = None
        for line in txt.splitlines():
            if "in_rms=" in line:
                rms = line.split("in_rms=")[1].split()[0]
        print(f"  [{'OK ' if rms else 'BREAK':>6}] day_program_729 in_rms "
              f"split -> {rms}")
        if not rms:
            bad += 1
        print(f"  [  info] OsO/overflow count (must be 0 to promote live): "
              f"{len(RE_OSO.findall(txt))}")
        # speed-1's own additive lines — informational, nothing parses them yet
        for tag in ("[eq-long] WARM START", "[eq-long] COLD START",
                    "[eq-long] DATA RECYCLING", "cache persisted on stop",
                    "SHERIFF cmd"):
            n = txt.count(tag)
            if n:
                print(f"  [  new ] {tag}: {n}")
    print(f"\n{'TELEMETRY INTACT' if bad == 0 else f'{bad} TELEMETRY BREAKS'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
