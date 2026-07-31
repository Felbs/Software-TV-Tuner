"""analyze_curves.py — score every replay in lab/speed_build/runs on ONE
ruler: an absolute fs_err_rms target, not each arm's own plateau.

Per tag it reports
  n_fields_to(target)   fields until err stays <= target for 5 in a row
  t_to(target)          the same in seconds of stream
  cliff                 fields to clear the 15.2 dB MER cliff
                        (MER = 20*log10(5/err) => err <= 0.8690)
  plateau               median err over the last 25 % of fields
  MER                   20*log10(5/plateau)

Usage: python lab/speed_build/analyze_curves.py [--target 0.5179] [tag...]
"""
from __future__ import annotations

import argparse
import math
import re
import statistics
from pathlib import Path

RUNS = Path(__file__).resolve().parent / "runs"
RE_EQ = re.compile(r"\[eq-long t=\s*([\d.]+)s\] fs=(\d+) fs_err_rms=([\d.]+)")
CLIFF_ERR = 5.0 / (10 ** (15.2 / 20.0))     # 0.8690 -> MER 15.2 dB


def curve(p: Path):
    rows = []
    for line in p.read_text(errors="replace").splitlines():
        m = RE_EQ.search(line)
        if m:
            rows.append((float(m.group(1)), int(m.group(2)), float(m.group(3))))
    return rows


def first_stable(rows, target, run_len=5):
    run = 0
    for t, fs, err in rows:
        if err <= target:
            run += 1
            if run >= run_len:
                return fs, t
        else:
            run = 0
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, default=None,
                    help="absolute fs_err_rms bar (default: 1.10 x the "
                         "plateau of the first tag listed)")
    ap.add_argument("tags", nargs="*")
    a = ap.parse_args()

    logs = sorted(RUNS.glob("*.chain.log"))
    by_tag = {}
    for p in logs:
        tag = p.name.rsplit("_", 1)[0]
        if a.tags and tag not in a.tags:
            continue
        rows = curve(p)
        if len(rows) < 20:
            continue
        by_tag.setdefault(tag, []).append(rows)

    order = a.tags if a.tags else sorted(by_tag)
    target = a.target
    if target is None and order:
        r0 = by_tag[order[0]][0]
        tail = [e for _t, _f, e in r0[int(len(r0) * 0.75):]]
        target = statistics.median(tail) * 1.10

    print(f"absolute convergence target: fs_err_rms <= {target:.4f} "
          f"(MER >= {20*math.log10(5.0/target):.2f} dB)")
    print(f"cliff target               : fs_err_rms <= {CLIFF_ERR:.4f} "
          f"(MER 15.2 dB)\n")
    print(f"{'tag':<18} {'runs':>4} {'fields_to_target':>16} {'t_s':>7} "
          f"{'fields_to_cliff':>15} {'plateau':>8} {'MER_dB':>7}")
    for tag in order:
        if tag not in by_tag:
            continue
        f_t, t_t, f_c, plat = [], [], [], []
        for rows in by_tag[tag]:
            fs, t = first_stable(rows, target)
            fc, _ = first_stable(rows, CLIFF_ERR)
            if fs:
                f_t.append(fs)
                t_t.append(t)
            if fc:
                f_c.append(fc)
            tail = [e for _t, _f, e in rows[int(len(rows) * 0.75):]]
            plat.append(statistics.median(tail))
        med = statistics.median
        print(f"{tag:<18} {len(by_tag[tag]):>4} "
              f"{(med(f_t) if f_t else float('nan')):>16.0f} "
              f"{(med(t_t) if t_t else float('nan')):>7.2f} "
              f"{(med(f_c) if f_c else float('nan')):>15.0f} "
              f"{med(plat):>8.4f} {20*math.log10(5.0/med(plat)):>7.2f}")


if __name__ == "__main__":
    main()
