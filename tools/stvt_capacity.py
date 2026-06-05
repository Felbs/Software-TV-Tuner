"""DVR Capacity Report — exactly what this rig can record simultaneously.

Answers four questions definitively:

1. AUDIO TRACKS WITHIN ONE PROGRAM
   How many simultaneously? -> all of them.
   Multirec uses ffmpeg -c copy, which is byte-for-byte passthrough.
   Every audio stream inside a program (English main, Spanish SAP,
   Descriptive Video Service, etc.) is captured automatically. You
   pick the language at PLAYBACK time, not at record time.

2. PROGRAMS WITHIN ONE MUX (different sub-channels, same RF channel)
   How many simultaneously? -> all of them in that mux.
   One SDR tuning gives us the full 19.39 Mbit/s ATSC multiplex.
   Multirec fans out N parallel ffmpeg outputs via -map 0:p:N from a
   single live.ts. Proven up to 8 programs (RF 35 ION mux). The
   theoretical ceiling is whatever the broadcaster fits in their mux.

3. DIFFERENT MUXES (e.g. NBC's RF 34 AND Fox's RF 36 at once)
   How many simultaneously? -> exactly ONE.
   One SDR can hold one tuning at a time. To record across muxes you
   would need either (a) a second SDR or (b) a wider-bandwidth SDR
   capable of capturing multiple 6 MHz blocks at once (RSPdx maxes
   at ~10 MHz, just enough for one mux + guard).

4. CLOSED CAPTIONS, DATA SERVICES
   Always captured. CEA-608/708 captions live in MPEG-2 video user_data
   bytes, which -c copy preserves. EIT/PSIP data is on shared PIDs that
   get copied with each program.

Usage:
    python tools/stvt_capacity.py             # human-readable report
    python tools/stvt_capacity.py --json      # machine-readable
    python tools/stvt_capacity.py --mux 35    # focus on one mux
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCAN_PATH = Path(os.path.expanduser("~")) / ".tv_tuner" / "scan.json"

# Hard system limits — these come from the rig, not the broadcaster
ATSC_MUX_MBIT = 19.39          # ATSC 1.0 ceiling per 6 MHz channel
SDR_TUNINGS = 1                # RSPdx is single-tuner
SDR_BANDWIDTH_MHZ = 10         # max contiguous IQ window
ATSC_CHANNEL_WIDTH_MHZ = 6     # one TV mux


def load_scan() -> dict:
    if not SCAN_PATH.exists():
        return {}
    try:
        return json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def analyze_mux(chan: dict) -> dict:
    """Per-mux stats."""
    callsign = chan.get("callsign") or "?"
    rf = chan.get("rf")
    progs = chan.get("programs") or []
    psip = chan.get("psip") or {}
    psip_by_pnum = {p.get("program_number"): p
                    for p in (psip.get("channels") or [])
                    if p.get("program_number")}
    prog_stats = []
    total_audio = 0
    has_sap = False
    has_dvs = False
    for p in progs:
        pnum = p.get("program_num") or p.get("program_id")
        match = psip_by_pnum.get(pnum) or {}
        major = match.get("major")
        minor = match.get("minor", 1)
        virtual = f"{major}.{minor}" if major else f"{rf}.{pnum}"
        short = match.get("short_name") or callsign
        audio_streams = p.get("audio_streams") or []
        langs = [a.get("lang") or "?" for a in audio_streams]
        has_eng = any(l == "eng" for l in langs)
        has_spa = any(l == "spa" for l in langs)
        if has_eng and has_spa:
            has_sap = True
        # DVS often appears as eng with service_type=2; we don't have
        # service_type in scan.json today, so flag any program with
        # 3+ audio tracks as "likely has DVS or extra".
        if len(audio_streams) >= 3:
            has_dvs = True
        prog_stats.append({
            "program_num":  pnum,
            "virtual":      virtual,
            "callsign":     short,
            "video_codec":  p.get("video_codec") or "?",
            "video_height": p.get("video_height") or 0,
            "video_fps":    p.get("video_fps") or 0,
            "audio_langs":  langs,
            "audio_count":  len(audio_streams),
        })
        total_audio += len(audio_streams)
    return {
        "rf":              rf,
        "callsign":        callsign,
        "program_count":   len(progs),
        "total_audio_tracks": total_audio,
        "has_sap":         has_sap,
        "has_likely_dvs":  has_dvs,
        "programs":        prog_stats,
        "pat_count":       chan.get("pat_count") or 0,
    }


def render_text(report: dict, mux_filter: int | None) -> str:
    out = []
    out.append("=" * 78)
    out.append("  STVT DVR CAPACITY REPORT")
    out.append("=" * 78)
    out.append("")
    out.append("HARD LIMITS (your rig)")
    out.append("-" * 78)
    out.append(f"  Tuners:                {SDR_TUNINGS}  "
               f"(one SDRplay RSPdx)")
    out.append(f"  Concurrent muxes:      {SDR_TUNINGS}  "
               f"(= one RF channel at a time)")
    out.append(f"  SDR bandwidth ceiling: ~{SDR_BANDWIDTH_MHZ} MHz  "
               f"(fits one {ATSC_CHANNEL_WIDTH_MHZ} MHz ATSC mux + guard)")
    out.append(f"  Mux throughput ceiling: {ATSC_MUX_MBIT:.2f} Mbit/s "
               f"(ATSC 1.0 spec, shared by all programs in that mux)")
    out.append("")
    out.append("WHAT YOU CAN RECORD SIMULTANEOUSLY")
    out.append("-" * 78)
    out.append("  Within one program:  ALL audio tracks (English + SAP + DVS),")
    out.append("                       closed captions, data services. Pick")
    out.append("                       language at PLAYBACK, not at record.")
    out.append("  Within one mux:      ALL programs (= every sub-channel on")
    out.append("                       that RF). Multirec fans out one ffmpeg")
    out.append("                       output per program from one tv_live.")
    out.append("                       Proven up to 8 simultaneous (RF 35).")
    out.append("  Across muxes:        ONE mux at a time. A second SDR (or a")
    out.append("                       wider-band SDR) would unlock this.")
    out.append("")
    muxes = report["muxes"]
    if mux_filter is not None:
        muxes = [m for m in muxes if m["rf"] == mux_filter]
    if not muxes:
        out.append("  (no muxes locked in scan — run `tv_tuner.py --scan`)")
        return "\n".join(out)
    out.append(f"YOUR MUXES ({len(muxes)} locked, from last scan)")
    out.append("-" * 78)
    out.append(f"  {'RF':<4} {'Callsign':<10} {'#prog':>5} "
               f"{'#audio':>6} {'SAP':>4} {'DVS?':>5}  Recordable")
    for m in muxes:
        sap = "yes" if m["has_sap"] else "-"
        dvs = "maybe" if m["has_likely_dvs"] else "-"
        recordable = f"{m['program_count']} programs at once"
        out.append(f"  {m['rf']:<4} {m['callsign']:<10} "
                   f"{m['program_count']:>5} {m['total_audio_tracks']:>6} "
                   f"{sap:>4} {dvs:>5}  {recordable}")
    out.append("")
    out.append("DENSEST MUX (best 'record everything' target)")
    out.append("-" * 78)
    best = max(muxes, key=lambda m: m["program_count"])
    out.append(f"  RF {best['rf']} {best['callsign']}  "
               f"-> {best['program_count']} programs, "
               f"{best['total_audio_tracks']} aggregate audio tracks")
    for p in best["programs"]:
        langs = "/".join(p["audio_langs"]) or "?"
        sap_tag = "  [SAP]" if "spa" in p["audio_langs"] and "eng" in p["audio_langs"] else ""
        out.append(f"    {p['virtual']:<5} {p['callsign']:<10}  "
                   f"{p['video_codec']} {p['video_height']}p   "
                   f"audio: {langs}{sap_tag}")
    out.append("")
    out.append(f"  To record this entire mux at once:")
    out.append(f"    python tools/stvt_multirec.py --rf {best['rf']} "
               f"--duration 60")
    out.append("")
    out.append("PER-MUX DETAIL")
    out.append("-" * 78)
    for m in muxes:
        out.append(f"  RF {m['rf']} {m['callsign']}  "
                   f"({m['program_count']} programs)")
        for p in m["programs"]:
            langs = "/".join(p["audio_langs"]) or "?"
            sap_tag = "  [SAP]" if (
                "spa" in p["audio_langs"] and "eng" in p["audio_langs"]
            ) else ""
            out.append(f"    {p['virtual']:<5} {p['callsign']:<10}  "
                       f"{p['video_codec']} {p['video_height']}p   "
                       f"{p['audio_count']} audio: {langs}{sap_tag}")
        out.append("")
    out.append("ANSWERS TO COMMON 'CAN I RECORD ___' QUESTIONS")
    out.append("-" * 78)
    out.append("  Channel + its SAP track at the same time?")
    out.append("    -> YES, always. SAP is just another audio stream in the")
    out.append("       same program; multirec captures all audio streams.")
    out.append("  All sub-channels of one station (e.g. 5.1, 5.2, 5.3)?")
    out.append("    -> YES, they all share one mux. One multirec captures")
    out.append("       all of them simultaneously.")
    out.append("  Two completely separate stations (e.g. NBC + Fox)?")
    out.append("    -> NO. NBC is on RF 34 and Fox is on RF 36 — separate")
    out.append("       muxes need separate tunings, and you have one SDR.")
    out.append("  Multiple shows on the same channel/mux at the same time?")
    out.append("    -> YES via `rmux` in the controller (records the whole")
    out.append("       mux for the duration). DON'T queue two separate")
    out.append("       single-program recordings on the same mux — the")
    out.append("       second one collides with the first's SDR ownership.")
    out.append("  An FM/AM radio broadcast?")
    out.append("    -> Not in this build. The pipeline is ATSC-only. The")
    out.append("       SDR hardware CAN tune those bands, but we have no")
    out.append("       FM/AM demodulator chain. Separate project.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--mux", type=int, default=None,
                    help="Limit report to one RF channel")
    ap.add_argument("--json", action="store_true",
                    help="Machine-readable output")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    scan = load_scan()
    if not scan:
        print("[capacity] no scan.json — run `tv_tuner.py --scan` first",
              file=sys.stderr)
        return 1
    muxes = []
    for c in scan.get("channels", []):
        if c.get("not_detected"):
            continue
        if not c.get("callsign") or c.get("callsign") in ("?", "None"):
            continue
        if not (c.get("programs") or []):
            continue
        muxes.append(analyze_mux(c))
    muxes.sort(key=lambda m: m["rf"])

    report = {
        "hard_limits": {
            "tuners":                 SDR_TUNINGS,
            "concurrent_muxes":       SDR_TUNINGS,
            "sdr_bandwidth_mhz":      SDR_BANDWIDTH_MHZ,
            "mux_throughput_mbit_s":  ATSC_MUX_MBIT,
        },
        "muxes": muxes,
        "max_simultaneous_programs": (
            max((m["program_count"] for m in muxes), default=0)
        ),
    }

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(render_text(report, args.mux))
    return 0


if __name__ == "__main__":
    sys.exit(main())
