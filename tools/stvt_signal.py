"""Real-time signal-strength meter for antenna aiming.

Sniffs one ATSC 1.0 RF every N seconds and renders an in-place
terminal readout of the same metrics the scanner uses to classify
locks: total RMS power, pilot SNR, pilot sharpness, and VSB asymmetry.

Why these four:
  * pilot SNR        — narrow CW carrier vs out-of-channel noise floor.
                        The single strongest "real ATSC vs noise" feature.
  * pilot sharpness  — pilot peak vs ±100 kHz neighborhood. Distinguishes
                        a genuine carrier from a broadband bump.
  * VSB asymmetry    — 8-VSB sideband is single-sided; ~+5 dB and up
                        on a clean ATSC carrier, ~0 on OFDM / noise.
  * RMS              — total in-bandwidth power. Mostly a sanity check
                        (very low RMS means the antenna is unplugged,
                        very high RMS often means a strong out-of-band
                        intermod source — not necessarily good).

Thresholds come from tv_tuner.run_scan()'s strict + weak gates so the
labels here match what the scanner would say about the same RF.

Two modes:
  default --rf N    — refresh one channel forever (Ctrl+C to stop).
  --scan-band       — one-shot sweep of every NA ATSC RF with a
                      one-line bar per channel. Quick antenna check
                      across the whole band, no lock attempts.

SDR REQUIRED. The scheduler daemon's recordings also use the SDR — if
one is in progress, this tool will fail to open the device. Wait for
the recording to finish (or stop it explicitly) before running.
"""
from __future__ import annotations

import argparse
import math
import os
import signal as signal_mod
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# SDRplay API DLL: SoapySDR's sdrPlaySupport.dll lives in C:\Program
# Files\SDRplay\API\x64 but radioconda's env doesn't inherit that PATH
# entry, so the DLL fails to load when SoapySDR enumerates devices.
# tv_live.py applies this same snippet for the same reason. Must run
# BEFORE the sdr_sweep import (which triggers SoapySDR module load).
if sys.platform == "win32":
    _sdrplay_api_x64 = r"C:\Program Files\SDRplay\API\x64"
    if os.path.isdir(_sdrplay_api_x64):
        os.environ["PATH"] = (_sdrplay_api_x64 + os.pathsep +
                               os.environ.get("PATH", ""))
        try:
            os.add_dll_directory(_sdrplay_api_x64)
        except (AttributeError, OSError):
            pass

# SoapySDR log-level suppression. By default Soapy spams stderr with
# "[ERROR] SoapySDR::loadModule(airspySupport.dll): LoadLibrary failed"
# for every optional driver that isn't installed (airspy, bladeRF,
# HackRF, RTL-SDR, etc.) PLUS "[INFO] devIdx: 0 SerNo: ..." every time
# a device is opened. None of it is actionable when SDRplay is what
# we're targeting; it just buries the actual readout. Setting log
# level to FATAL keeps only genuine failures visible.
SOAPY_LOG_SUPPRESS_ERR: str | None = None
try:
    import SoapySDR   # noqa: E402
    SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
except Exception as exc:   # ImportError, AttributeError if Soapy too old
    SOAPY_LOG_SUPPRESS_ERR = str(exc)

# Reuse sdr_sweep's sweep() + _analyze() so the metrics are identical
# to what the scanner sees. Single source of truth for "what's on this
# frequency right now". sdr_sweep imports SoapySDR at top level — that
# might fail if SoapySDR isn't installed, so guard the import.
SWEEP_IMPORT_ERR: str | None = None
try:
    import sdr_sweep   # noqa: E402
except Exception as exc:   # ImportError, OSError from SoapySDR load, ...
    SWEEP_IMPORT_ERR = str(exc)
    sdr_sweep = None  # type: ignore

# RF -> MHz helper from tv_tuner. Pure math, no SDR touching.
from tv_tuner import rf_to_freq_mhz   # noqa: E402

# Thresholds: same numbers the scanner uses to classify locks. Splitting
# the band into STRONG / GOOD / WEAK / NONE buckets gives the user
# something more useful than a raw dB reading while aiming an antenna.
#
# STRONG  = exceeds the STRICT gate (auto-lock candidate, F1=1.0)
# GOOD    = exceeds the WEAK gate but not STRICT (--thorough catches)
# WEAK    = some carrier signature but below WEAK gate
# NONE    = no carrier signature
PILOT_SNR_STRICT = 30.0
PILOT_SNR_WEAK   = 15.0
PILOT_SHARP_STRICT = 26.25
PILOT_SHARP_WEAK   = 8.0
VSB_ASYM_STRICT = 2.4
VSB_ASYM_WEAK   = -14.0

# RMS thresholds are eyeballed (the scanner uses median+4dB, not a
# fixed cutoff). These are reasonable defaults for an SDRplay RSP1A
# at IFGR=59. Re-tune via --rms-good / --rms-strong if your hardware
# floor is elsewhere.
RMS_STRONG_DBFS = -25.0    # plenty of headroom
RMS_GOOD_DBFS   = -45.0    # decoder-comfortable
RMS_WEAK_DBFS   = -60.0    # marginal but readable


def _label_pilot_snr(v: float) -> str:
    if v >= PILOT_SNR_STRICT:
        return "STRONG"
    if v >= PILOT_SNR_WEAK:
        return "GOOD"
    if v > 5:
        return "WEAK"
    return "NONE"


def _label_pilot_sharp(v: float) -> str:
    if v >= PILOT_SHARP_STRICT:
        return "STRONG"
    if v >= PILOT_SHARP_WEAK:
        return "GOOD"
    if v > 3:
        return "WEAK"
    return "NONE"


def _label_vsb(v: float) -> str:
    if v >= 8.0:
        return "EXCELLENT"
    if v >= VSB_ASYM_STRICT:
        return "STRONG"
    if v >= VSB_ASYM_WEAK:
        return "GOOD"
    return "WEAK"


def _label_rms(v: float) -> str:
    if v >= RMS_STRONG_DBFS:
        return "STRONG"
    if v >= RMS_GOOD_DBFS:
        return "GOOD"
    if v >= RMS_WEAK_DBFS:
        return "WEAK"
    return "NONE"


# ── ASCII bar rendering ─────────────────────────────────────────────


BAR_WIDTH = 24


def _bar(frac: float) -> str:
    """Render a 0..1 fraction as a filled/empty bar. Clamps out-of-range."""
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * BAR_WIDTH))
    return "#" * filled + "-" * (BAR_WIDTH - filled)


def _pilot_snr_bar(v: float) -> str:
    # Range 0..50 dB covers everything from "noise" to "right next to the
    # tower". Anything beyond is just clamped.
    return _bar(v / 50.0)


def _pilot_sharp_bar(v: float) -> str:
    return _bar(v / 40.0)


def _vsb_bar(v: float) -> str:
    # -14..+10 -> 0..1
    return _bar((v + 14.0) / 24.0)


def _rms_bar(v: float) -> str:
    # -80..-10 dBFS -> 0..1
    return _bar((v + 80.0) / 70.0)


# ── output helpers ──────────────────────────────────────────────────


def _is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, OSError):
        return False


ANSI_CLEAR_LINE = "\x1b[2K"
ANSI_CURSOR_UP  = "\x1b[A"


def _render_block(rf: int, freq_mhz: float, antenna: str,
                   m: dict, bars: bool) -> str:
    """Build the multi-line readout for one sniff. Returns it as a single
    string with embedded newlines."""
    rms = m.get("rms_dbfs", float("-inf"))
    snr = m.get("pilot_snr_db", float("-inf"))
    sharp = m.get("pilot_sharpness_db", float("-inf"))
    vsb = m.get("vsb_asymmetry_db", float("-inf"))

    def fmt_metric(name: str, val: float, bar: str, label: str,
                   unit: str = "dB") -> str:
        if val == float("-inf") or math.isnan(val):
            val_str = "  --   "
        else:
            val_str = f"{val:+7.1f}"
        bar_str = f"  {bar}  " if bars else "  "
        return f"  {name:<14}{val_str} {unit:<5}{bar_str}{label}"

    lines = [
        f"RF {rf} ({freq_mhz:.1f} MHz)  {antenna}",
        fmt_metric("RMS:",        rms,   _rms_bar(rms),         _label_rms(rms),
                    unit="dBFS"),
        fmt_metric("Pilot SNR:",  snr,   _pilot_snr_bar(snr),   _label_pilot_snr(snr)),
        fmt_metric("Pilot Sharp:", sharp, _pilot_sharp_bar(sharp), _label_pilot_sharp(sharp)),
        fmt_metric("VSB Asym:",   vsb,   _vsb_bar(vsb),         _label_vsb(vsb)),
        "  -- Ctrl+C to stop --",
    ]
    return "\n".join(lines)


# ── monitoring modes ─────────────────────────────────────────────────


def monitor_one(rf: int, refresh_sec: float, antenna: str,
                 ifgr: float, rfgain_sel: int, bars: bool,
                 dwell_sec: float) -> int:
    """Tune one RF forever, refreshing the metrics in place."""
    if sdr_sweep is None:
        print(f"[signal] cannot import sdr_sweep: {SWEEP_IMPORT_ERR}",
              file=sys.stderr)
        print("[signal] this tool needs SoapySDR — run with radioconda "
              "Python (same as tv_live).", file=sys.stderr)
        return 2

    freq_hz = int(rf_to_freq_mhz(rf) * 1e6)
    if freq_hz == 0:
        print(f"[signal] RF {rf} is outside the ATSC range "
              f"(use 2-36 for NA)", file=sys.stderr)
        return 1
    freq_mhz = freq_hz / 1e6

    tty = _is_tty()
    first = True
    block_lines = 6  # _render_block always emits 6 lines

    # Install a Ctrl+C handler that just sets a flag so we can shut
    # down cleanly mid-sweep (otherwise the SDR stays open and the
    # next caller — including the scheduler — fails to acquire it).
    stop = False

    def on_int(_sig, _frame):
        nonlocal stop
        stop = True
    try:
        signal_mod.signal(signal_mod.SIGINT, on_int)
    except (ValueError, OSError):
        pass

    print(f"[signal] tuning RF {rf} ({freq_mhz:.1f} MHz) on '{antenna}', "
          f"refresh {refresh_sec:.1f}s")
    print(f"[signal] thresholds: pilot_snr STRICT={PILOT_SNR_STRICT}, "
          f"WEAK={PILOT_SNR_WEAK} dB")
    print()

    while not stop:
        try:
            results = sdr_sweep.sweep(
                [freq_hz],
                sample_rate=8_000_000,
                dwell_sec=dwell_sec,
                settle_sec=0.04,
                mode="atsc",
                antenna=antenna,
                ifgr=ifgr,
                rfgain_sel=rfgain_sel,
                progress=None,
            )
        except Exception as exc:
            # Most likely "SDR busy" — print and back off so we don't
            # spam retries.
            print(f"[signal] sweep failed: {exc}", file=sys.stderr)
            return 1

        if not results:
            print("[signal] sweep returned no samples", file=sys.stderr)
            time.sleep(refresh_sec)
            continue
        block = _render_block(rf, freq_mhz, antenna, results[0], bars)

        if tty and not first:
            # Move the cursor up over the previous block and rewrite.
            sys.stdout.write(ANSI_CURSOR_UP * block_lines)
            for line in block.splitlines():
                sys.stdout.write(ANSI_CLEAR_LINE + line + "\n")
        else:
            print(block)
        sys.stdout.flush()
        first = False

        # Sleep but stay responsive to SIGINT.
        elapsed = 0.0
        while elapsed < refresh_sec and not stop:
            time.sleep(min(0.1, refresh_sec - elapsed))
            elapsed += 0.1

    print("\n[signal] stopped.")
    return 0


def scan_band(refresh_per_rf_sec: float, antenna: str,
               ifgr: float, rfgain_sel: int, dwell_sec: float,
               rf_lo: int = 2, rf_hi: int = 36) -> int:
    """One-shot sniff of every NA ATSC RF, one line per channel.

    Useful as a quick "is the antenna pointed at anything?" check —
    you see all 35 RFs in ~7 seconds and the strong carriers jump out
    as long bars.
    """
    if sdr_sweep is None:
        print(f"[signal] cannot import sdr_sweep: {SWEEP_IMPORT_ERR}",
              file=sys.stderr)
        return 2

    freqs = [(rf, int(rf_to_freq_mhz(rf) * 1e6))
             for rf in range(rf_lo, rf_hi + 1)
             if rf_to_freq_mhz(rf) > 0]
    print(f"[signal] scanning RFs {rf_lo}..{rf_hi} "
          f"({len(freqs)} channels)...")
    print()

    try:
        results = sdr_sweep.sweep(
            [f for _, f in freqs],
            sample_rate=8_000_000,
            dwell_sec=dwell_sec,
            settle_sec=0.04,
            mode="atsc",
            antenna=antenna,
            ifgr=ifgr,
            rfgain_sel=rfgain_sel,
            progress=None,
        )
    except Exception as exc:
        print(f"[signal] sweep failed: {exc}", file=sys.stderr)
        return 1

    print(f"{'RF':>3}  {'MHz':>6}  {'pilot_snr':>10}  {'bar':<24}  label")
    print("-" * 60)
    for (rf, _f), m in zip(freqs, results):
        snr = m.get("pilot_snr_db", float("-inf"))
        snr_str = f"{snr:+6.1f} dB" if snr != float("-inf") else "  --"
        print(f"{rf:>3}  {rf_to_freq_mhz(rf):>6.1f}  {snr_str:>10}  "
              f"{_pilot_snr_bar(snr):<24}  {_label_pilot_snr(snr)}")
    return 0


def _bar_with_peak(frac: float, peak_frac: float) -> str:
    """Like _bar() but overlays a '>' marker at the peak-hold position so
    you can see each channel's best level while you rotate the antenna."""
    frac = max(0.0, min(1.0, frac))
    peak_frac = max(0.0, min(1.0, peak_frac))
    filled = int(round(frac * BAR_WIDTH))
    cells = list("#" * filled + "-" * (BAR_WIDTH - filled))
    pk = min(BAR_WIDTH - 1, int(round(peak_frac * BAR_WIDTH)))
    # Only show the marker when the peak is ahead of the current fill, so it
    # reads as "best so far" rather than cluttering a live-at-peak bar.
    if pk > filled:
        cells[pk] = ">"
    return "".join(cells)


def watch_band(refresh_sec: float, antenna: str, ifgr: float,
               rfgain_sel: int, dwell_sec: float,
               rf_list: list[int]) -> int:
    """LIVE all-channel strength dashboard: sweep the whole channel list
    over and over, redrawing every bar in place. Built for antenna aiming
    — watch every channel respond at once as you rotate, with a peak-hold
    '>' marker showing each channel's best level so far."""
    if sdr_sweep is None:
        print(f"[signal] cannot import sdr_sweep: {SWEEP_IMPORT_ERR}",
              file=sys.stderr)
        return 2

    freqs = [(rf, int(rf_to_freq_mhz(rf) * 1e6))
             for rf in rf_list if rf_to_freq_mhz(rf) > 0]
    if not freqs:
        print("[signal] no valid RFs to watch", file=sys.stderr)
        return 1

    stop = False

    def on_int(_sig, _frame):
        nonlocal stop
        stop = True
    try:
        signal_mod.signal(signal_mod.SIGINT, on_int)
    except (ValueError, OSError):
        pass

    peak = {rf: float("-inf") for rf, _ in freqs}
    tty = _is_tty()
    first = True
    # Frame is a constant number of lines so we can cursor-up over it.
    frame_lines = 2 + len(freqs) + 1
    passes = 0

    while not stop:
        try:
            results = sdr_sweep.sweep(
                [f for _, f in freqs],
                sample_rate=8_000_000,
                dwell_sec=dwell_sec,
                settle_sec=0.04,
                mode="atsc",
                antenna=antenna,
                ifgr=ifgr,
                rfgain_sel=rfgain_sel,
                progress=None,
            )
        except Exception as exc:
            print(f"[signal] sweep failed: {exc}", file=sys.stderr)
            return 1
        passes += 1

        # Current strongest channel, flagged with a star to draw the eye.
        best_rf, best_snr = None, float("-inf")
        for (rf, _f), m in zip(freqs, results):
            snr = m.get("pilot_snr_db", float("-inf"))
            if snr > best_snr:
                best_rf, best_snr = rf, snr

        lines = [
            f"LIVE band meter - {antenna}, IFGR={ifgr:.0f}  "
            f"(pass {passes}, Ctrl+C to stop)",
            f"{'RF':>3} {'MHz':>6} {'pilot_snr':>9}  "
            f"{'bar (> = peak so far)':<{BAR_WIDTH}}  label",
        ]
        for (rf, _f), m in zip(freqs, results):
            snr = m.get("pilot_snr_db", float("-inf"))
            if snr > peak[rf]:
                peak[rf] = snr
            snr_str = f"{snr:+5.1f}dB" if snr != float("-inf") else "  --  "
            bar = _bar_with_peak(snr / 50.0, peak[rf] / 50.0)
            label = _label_pilot_snr(snr)
            star = "*" if rf == best_rf and snr > 5 else " "
            lines.append(f"{rf:>3} {rf_to_freq_mhz(rf):>6.1f} {snr_str:>9}  "
                         f"{bar}  {label:<6}{star}")
        lines.append("  -- rotate slowly; longest bar / * = strongest --")
        frame = "\n".join(lines)

        if tty and not first:
            sys.stdout.write(ANSI_CURSOR_UP * frame_lines)
            for line in frame.splitlines():
                sys.stdout.write(ANSI_CLEAR_LINE + line + "\n")
        else:
            print(frame)
        sys.stdout.flush()
        first = False

        elapsed = 0.0
        while elapsed < refresh_sec and not stop:
            time.sleep(min(0.1, refresh_sec - elapsed))
            elapsed += 0.1

    print("\n[signal] stopped.")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--rf", type=int, default=None,
                    help="ATSC RF channel to monitor (e.g. 36). Required "
                         "unless --scan-band is given.")
    ap.add_argument("--refresh", type=float, default=1.0,
                    help="Refresh interval in seconds (default 1.0). "
                         "Each refresh costs one 100ms sniff + retune.")
    ap.add_argument("--dwell-sec", type=float, default=0.10,
                    help="Capture window per sniff (default 0.10s). "
                         "Longer = lower noise floor at the cost of "
                         "responsiveness.")
    ap.add_argument("--bars", action="store_true",
                    help="Show ASCII bar charts (default off — pure "
                         "numeric output).")
    ap.add_argument("--numeric", action="store_true",
                    help="Force numeric mode (default). Mutually exclusive "
                         "with --bars; if both pass, --bars wins.")
    ap.add_argument("--antenna", default="Antenna A",
                    help="SoapySDR antenna name (default 'Antenna A' for "
                         "SDRplay RSP1A).")
    ap.add_argument("--ifgr", type=float, default=59.0,
                    help="IF gain reduction in dB (SDRplay; default 59).")
    ap.add_argument("--rfgain-sel", type=int, default=5,
                    help="RF gain selector index (SDRplay; default 5).")
    ap.add_argument("--scan-band", action="store_true",
                    help="One-shot sniff of every NA ATSC RF (2..36) with "
                         "a one-line bar per channel.")
    ap.add_argument("--watch-band", action="store_true",
                    help="LIVE all-channel dashboard: refresh every channel "
                         "in place, forever, for antenna aiming. Shows a "
                         "peak-hold '>' marker per channel.")
    ap.add_argument("--rf-list", default=None,
                    help="Comma-separated RFs to watch with --watch-band "
                         "(e.g. '7,9,21,31,34,36'). Fewer channels = faster "
                         "refresh. Default: full NA band 2..36.")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    if args.scan_band:
        return scan_band(args.refresh, args.antenna, args.ifgr,
                          args.rfgain_sel, args.dwell_sec)

    if args.watch_band:
        if args.rf_list:
            try:
                rf_list = [int(x) for x in args.rf_list.split(",") if x.strip()]
            except ValueError:
                ap.error("--rf-list must be comma-separated integers")
        else:
            rf_list = list(range(2, 37))
        return watch_band(args.refresh, args.antenna, args.ifgr,
                          args.rfgain_sel, args.dwell_sec, rf_list)

    if args.rf is None:
        ap.error("one of --rf <N>, --scan-band, or --watch-band is required")

    return monitor_one(args.rf, args.refresh, args.antenna,
                        args.ifgr, args.rfgain_sel, args.bars,
                        args.dwell_sec)


if __name__ == "__main__":
    sys.exit(main())
