"""mer_gain_cal.py — MER-driven gain calibration: the closed-loop replacement for
ladder-guessing. Sweeps the (rfgain_sel x IFGR) grid, scores each cell by the
equalizer's own fs_err_rms -> MER dB, and reports the grid + winner. A cell needs
only ~14 s because MER is continuous — no waiting for the binary decode cliff.

Usage:
    python mer_gain_cal.py --rf 31 [--antenna "Antenna A"] [--secs 14]
"""
import argparse, math, os, re, subprocess, time
from pathlib import Path

PY = r"C:\Users\user\radioconda\python.exe"
TV_LIVE = Path(r"Z:\src\magic-tv-decoder\tools\tv_live.py")
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
SDRPLAY_DLL = r"C:\Program Files\SDRplay\API\x64"
LOG = Path(os.environ.get("TEMP", ".")) / "mer_gain_cal.log"

CLIFF_DB, TRAIN_RMS = 15.2, 5.0
RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
RE_FPLL = re.compile(r"mean\|x\|=([\d.]+).*?in_rms=([\d.]+)")
RE_MAXX = re.compile(r"max\|x\|=([\d.]+)")

# grid: rfgain_sel (0=max RF gain .. 6=min) x IFGR (20=max IF gain .. 59=min)
GRID = [(rf, ifgr) for rf in (2, 3, 4, 5) for ifgr in (32, 40, 48)]


def parse_cells(spec):
    """--cells "2:34,2:36,1:40" -> [(2,34),(2,36),(1,40)]"""
    out = []
    for part in spec.split(","):
        rf, ifgr = part.split(":")
        out.append((int(rf), int(ifgr)))
    return out


def mer_db(err):
    return 20.0 * math.log10(TRAIN_RMS / err) if err > 0 else 40.0


def env_for(antenna, ifgr, rfsel, biast=False, rf=99):
    e = os.environ.copy()
    e["PATH"] = SDRPLAY_DLL + os.pathsep + e.get("PATH", "")
    if biast:
        e["STVT_BIAST"] = "1"
    e.update({
        "STVT_ANTENNA": antenna, "STVT_IFGR": str(ifgr),
        "STVT_RFGAIN_SEL": str(rfsel), "STVT_EQ": "long",
        "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
        # DAB band III = US VHF-hi; the notch must never touch RF7-13
        # (2026-07-04 discovery — this hardcoded "1" blinded VHF cals)
        "STVT_DABNOTCH": "0" if rf < 14 else "1",
        "STVT_RS": "stock", "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8",
        "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
        "STVT_EQ_TELEM": "1",
    })
    return e


def headers():
    if not LIVE.exists(): return 0
    with open(LIVE, "rb") as f:
        return f.read().count(b"\x00\x00\x01\xb3")


def run_cell(rf, antenna, ifgr, rfsel, secs, biast=False):
    if LIVE.exists():
        try: LIVE.unlink()
        except OSError: pass
    with open(LOG, "w") as lf:
        ch = subprocess.Popen([PY, "-u", str(TV_LIVE), "--rf", str(rf)],
                              env=env_for(antenna, ifgr, rfsel, biast, rf=rf),
                              stdout=lf, stderr=subprocess.STDOUT)
        time.sleep(secs)
        ch.terminate()
        try: ch.wait(timeout=6)
        except Exception: ch.kill()
    text = LOG.read_text(errors="ignore")
    errs = [float(m.group(1)) for m in RE_FS.finditer(text)]
    # skip the first third (equalizer convergence), average the rest
    tail = errs[len(errs) // 3:] if len(errs) >= 3 else errs
    mer = sum(mer_db(e) for e in tail) / len(tail) if tail else 0.0
    fpll = RE_FPLL.findall(text)
    irms = float(fpll[-1][1]) if fpll else 0.0
    mxs = [float(m.group(1)) for m in RE_MAXX.finditer(text)]
    railing = sum(1 for m in mxs if m > 1.5)
    return mer, irms, railing, headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=31)
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--secs", type=int, default=14)
    ap.add_argument("--cells", default="", help='fine sweep, e.g. "2:34,2:36,1:40"')
    ap.add_argument("--biast", action="store_true", help="power a bias-T LNA")
    args = ap.parse_args()
    grid = parse_cells(args.cells) if args.cells else GRID

    print(f"  MER gain calibration RF{args.rf} {args.antenna} "
          f"({len(grid)} cells x {args.secs}s)\n")
    print(f"  {'rfgain':>7}{'IFGR':>6}{'MER dB':>8}{'margin':>8}{'in_rms':>8}"
          f"{'rail':>6}{'hdrs':>6}")
    results = []
    for rfsel, ifgr in grid:
        mer, irms, rail, h = run_cell(args.rf, args.antenna, ifgr, rfsel,
                                      args.secs, args.biast)
        note = " OVERLOAD" if rail > 3 else (" ***" if mer >= CLIFF_DB else "")
        print(f"  {rfsel:>7}{ifgr:>6}{mer:>8.2f}{mer-CLIFF_DB:>+8.2f}{irms:>8.1f}"
              f"{rail:>6}{h:>6}{note}")
        results.append((mer, rfsel, ifgr, h))
        time.sleep(2)
    results.sort(reverse=True)
    best = results[0]
    print(f"\n  winner: rfgain={best[1]} IFGR={best[2]}  MER={best[0]:.2f} dB "
          f"({best[0]-CLIFF_DB:+.2f} vs cliff)  hdrs={best[3]}")


if __name__ == "__main__":
    main()
