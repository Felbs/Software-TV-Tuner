#!/usr/bin/env python3
"""stvt_surf_info.py — build the rich channel banner for the surfer and push it
to the running mpv's OSD via its IPC socket.

Pulls three things together for the channel you just landed on:
  1. network name + virtual channel + callsign       (from scan.json PSIP)
  2. what's airing right now                          (EIT, via stvt_epg)
  3. signal strength + decode health                 (pilot SNR + tei_pct from the scan)

Usage:
  stvt_surf_info.py --rf 34 --program 3 --virtual 4.1 --callsign WRC-HD \
                    --sock /tmp/mpv-cc.sock
"""
from __future__ import annotations
import argparse, json, pathlib, socket, time

SCAN = pathlib.Path.home() / ".tv_tuner" / "scan.json"


def load_scan() -> dict:
    try:
        return json.loads(SCAN.read_text())
    except Exception:
        return {}


def chan_for_rf(scan: dict, rf: int) -> dict:
    for c in scan.get("channels", []):
        if c.get("rf") == rf and c.get("lock"):
            return c
    return {}


def snr_bars(snr_db: float | None) -> tuple[str, int]:
    """Map pilot SNR (dB) to a 5-segment filled/empty meter (WiFi/battery style).
    Receivable OTA channels cluster in a narrow high band (~50-68 dB measured),
    so the scale is spread across THAT range — not 0-68 — to make real
    differences visible: a 5-bar channel and a 2-bar channel look obviously
    different even though both lock. Returns (gauge_string, level 0-5)."""
    if snr_db is None:
        return "·····", 0
    # thresholds tuned to the measured receivable range (DC locals 54-66 dB)
    thresholds = [50, 54, 58, 62, 66]   # dB for bars 1..5
    lvl = sum(1 for t in thresholds if snr_db >= t)
    return "█" * lvl + "░" * (5 - lvl), lvl


def now_playing(rf: int, program: int) -> tuple[str, str]:
    """Return (network, current_show_title) from the EIT via stvt_epg.
    Falls back to ('', '') when no guide data exists for this program."""
    try:
        import stvt_epg
        chans, _ = stvt_epg.load_epg()
        now = int(time.time())
        for r in chans:
            if r.get("rf") == rf and int(r.get("program", -1)) == int(program):
                net = r.get("network") or ""
                ev = stvt_epg.event_at(r.get("events") or [], now)
                return net, (ev.get("title") if ev else "")
    except Exception:
        pass
    return "", ""


def mpv_show(sock: str, text: str, ms: int = 5000) -> None:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(2)
        s.connect(sock)
        cmd = {"command": ["show-text", text, ms]}
        s.sendall((json.dumps(cmd) + "\n").encode())
        s.close()
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, required=True)
    ap.add_argument("--program", type=int, required=True)
    ap.add_argument("--virtual", default="?")
    ap.add_argument("--callsign", default="?")
    ap.add_argument("--sock", default="/tmp/mpv-cc.sock")
    ap.add_argument("--print", action="store_true", help="print banner instead of sending to mpv")
    a = ap.parse_args()

    scan = load_scan()
    c = chan_for_rf(scan, a.rf)
    snr = c.get("pilot_snr_db")
    tei = c.get("tei_pct")
    net_hint = c.get("network_hint") or ""

    net, show = now_playing(a.rf, a.program)
    net = net or net_hint  # EIT network name preferred, scan hint as fallback

    # Line 1: channel identity + network
    line1 = f"{a.virtual}  {a.callsign}"
    if net and net not in a.callsign:
        line1 += f"  ·  {net}"

    # Line 2: what's on now
    line2 = f"Now: {show}" if show else "Now: (no guide data)"

    # Line 3: signal strength + decode health
    bars, _lvl = snr_bars(snr)
    snr_txt = f"{snr:.0f} dB" if isinstance(snr, (int, float)) else "?"
    if tei is None:
        health = ""
    elif tei == 0:
        health = "  ·  decode clean"
    elif tei < 2:
        health = f"  ·  decode {tei:.1f}% err"
    else:
        health = f"  ·  decode {tei:.0f}% ERR"
    line3 = f"Signal {bars} {snr_txt}{health}"

    banner = f"{line1}\n{line2}\n{line3}"
    if a.print:
        print(banner)
    else:
        mpv_show(a.sock, banner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
