#!/usr/bin/env python3
"""stvt_hdhr.py — HDHomeRun-compatible tuner server (native-Linux edition).

Turns this box (SDR + antenna) into a network tuner that Jellyfin / Plex / Emby
auto-detect. It serves the ATSC mux as per-program MPEG-TS over HTTP, so a
decode-capable client (Jellyfin on a faster box -> transcode -> phone/TV app)
does the video work — this box only tunes and copies packets.

This is the native-Linux port of the WSL/Pi HDHomeRun server. The WSL version
delegated tuning to the web panel (tv_tuna_panel.py, :8642); this branch has no
panel, so this edition manages the tv_live.py chain DIRECTLY — exactly the way
stvt_run.sh does (spawn `python3 tv_live.py --rf N`, read the growing
tools/data/tv_live/live.ts). Single tuner: one RF (6 MHz) at a time; switching
RF re-spawns the chain and interrupts current viewers.

Endpoints (HDHomeRun HTTP API + generic M3U):
  /discover.json        device descriptor  (how servers auto-detect it)
  /lineup.json          channel list       (virtual chan + stream URL)
  /lineup_status.json   scan status
  /lineup.m3u           M3U playlist        (generic-tuner path)
  /guide.xml            minimal XMLTV       (so Jellyfin Live-TV setup completes)
  /auto/v<maj>.<min>    HDHomeRun-standard stream path
  /stream/<rf>/<minor>  our lineup-URL path (tunes on demand)

Gain: this branch's stvt_run.sh defaults to the ACTIVE-antenna gain
(IFGR=59/RFGAIN_SEL=5/Antenna A), which will NOT lock a passive antenna. The
tuner env below defaults to MAX gain (IFGR=20/RFGAIN_SEL=7/Antenna B) — the
verified working config for the passive antenna on this box — and every knob is
overridable from the environment.
"""
import json, os, socket, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]          # .../Software-TV-Tuner
TOOLS = REPO / "tools"
LIVE = TOOLS / "data" / "tv_live" / "live.ts"
sys.path.insert(0, str(TOOLS))
try:
    from default_stations import DEFAULT_STATIONS
except Exception:
    DEFAULT_STATIONS = []

PORT = int(os.environ.get("STVT_HDHR_PORT", "5004"))
DEVICE_ID = os.environ.get("STVT_HDHR_ID", "STVT0001")
FRIENDLY = os.environ.get("STVT_HDHR_NAME", "STVT Tuner")

# Tuner env: MAX gain for the passive antenna, plus the lean real-time config
# stvt_run.sh uses on this box. All overridable from the environment.
TUNE_ENV = {
    "STVT_IFGR":        os.environ.get("STVT_IFGR", "20"),
    "STVT_RFGAIN_SEL":  os.environ.get("STVT_RFGAIN_SEL", "7"),
    "STVT_ANTENNA":     os.environ.get("STVT_ANTENNA", "Antenna B"),
    "STVT_EQ":          os.environ.get("STVT_EQ", "long"),
    "STVT_RS":          os.environ.get("STVT_RS", "stock"),
    "STVT_VITERBI":     os.environ.get("STVT_VITERBI", "hard"),
    "STVT_SPS":         os.environ.get("STVT_SPS", "1.3"),
    "STVT_RRC_SYMS":    os.environ.get("STVT_RRC_SYMS", "4"),
    "STVT_TEISCRUB":    os.environ.get("STVT_TEISCRUB", "0"),
    "STVT_FPLL_FOLD":   os.environ.get("STVT_FPLL_FOLD", "1"),
    "STVT_FPLL_BLOCK_NCO": os.environ.get("STVT_FPLL_BLOCK_NCO", "1"),
}


def host_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


HOST = host_ip()
BASE = f"http://{HOST}:{PORT}"


def load_lineup():
    """Build the channel lineup from the DEFAULT_STATIONS table. Each virtual
    subchannel becomes one entry; `minor` is the subchannel index used to pick
    the MPEG program from the live PAT at stream time (see pat_programs)."""
    out = []
    for rf, virtual, callsign, network, city, subs in DEFAULT_STATIONS:
        # primary (.1) subchannel
        maj = virtual.split(".")[0]
        out.append({"GuideNumber": virtual, "minor": 1,
                    "GuideName": f"{callsign} {network}".strip(),
                    "rf": rf, "URL": f"{BASE}/stream/{rf}/1"})
        # secondary subchannels (.2, .3, ...) in table order
        for idx, (vch, name) in enumerate(subs, start=2):
            out.append({"GuideNumber": vch, "minor": idx,
                        "GuideName": name,
                        "rf": rf, "URL": f"{BASE}/stream/{rf}/{idx}"})
    # de-dup by GuideNumber
    seen = {}
    for e in out:
        seen.setdefault(e["GuideNumber"], e)
    return list(seen.values())


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def guide_xml():
    """Minimal valid XMLTV so Jellyfin's Live-TV setup completes. Channels +
    rolling generic blocks; real PSIP EPG enrichment is a later phase."""
    now = time.time()

    def ts(t):
        return time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(t))

    ch = load_lineup()
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<tv generator-info-name="stvt">']
    for e in ch:
        out.append(f'<channel id="{e["GuideNumber"]}"><display-name>'
                   f'{_esc(e["GuideNumber"])} {_esc(e["GuideName"])}'
                   f'</display-name></channel>')
    start = now - 6 * 3600
    for e in ch:
        for i in range(8):
            s = start + i * 3 * 3600
            out.append(f'<programme start="{ts(s)}" stop="{ts(s + 3 * 3600)}" '
                       f'channel="{e["GuideNumber"]}"><title>{_esc(e["GuideName"])}'
                       f'</title><desc>Live</desc></programme>')
    out.append('</tv>')
    return "\n".join(out).encode()


# ---- single-tuner chain management -------------------------------------------
_tune_lock = threading.Lock()
_cur_rf = None
_chain = None            # the tv_live.py Popen we own


def chain_alive():
    return _chain is not None and _chain.poll() is None


def pat_programs(path, scan_bytes=4_000_000):
    """Parse the PAT from the tail of live.ts -> sorted list of program numbers.
    Robust to the growing file: scan the last few MB, take the newest PAT."""
    try:
        sz = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, sz - scan_bytes))
            d = f.read()
    except OSError:
        return []
    progs = {}
    i = d.find(b"\x47")
    while i >= 0 and i + 188 <= len(d):
        if d[i] != 0x47:
            i += 1; continue
        b1, b2, b3 = d[i + 1], d[i + 2], d[i + 3]
        pid = ((b1 & 0x1f) << 8) | b2
        pusi = b1 & 0x40
        if pid == 0 and pusi:                    # PAT
            payload = i + 4
            if b3 & 0x20:                        # adaptation field present
                payload += 1 + d[payload]
            payload += 1 + d[payload]            # pointer_field
            if payload + 8 <= i + 188 and d[payload] == 0x00:
                seclen = ((d[payload + 1] & 0x0f) << 8) | d[payload + 2]
                end = min(payload + 3 + seclen - 4, i + 188)
                q = payload + 8
                while q + 4 <= end:
                    pn = (d[q] << 8) | d[q + 1]
                    pmap = ((d[q + 2] & 0x1f) << 8) | d[q + 3]
                    if pn != 0:                  # skip network PID entry
                        progs[pn] = pmap
                    q += 4
        i += 188
    return sorted(progs.keys())


def _wait_lock(rf, timeout=40):
    """Wait until the freshly-spawned chain produces a healthy locked mux:
    live.ts growing AND a sane unique-PID count (locked ~15-60; a drought is
    hundreds). Returns the sorted program list on success, [] on failure."""
    t0 = time.time(); last = -1
    while time.time() - t0 < timeout:
        if not chain_alive():
            return []
        try:
            sz = LIVE.stat().st_size
        except OSError:
            sz = 0
        if sz > 4_000_000 and sz != last:
            # count unique PIDs in the last 2 MB
            with open(LIVE, "rb") as f:
                f.seek(max(0, sz - 2_000_000)); d = f.read()
            pids = set(); j = d.find(b"\x47")
            while j >= 0 and j + 188 <= len(d):
                if d[j] == 0x47:
                    pids.add(((d[j + 1] & 0x1f) << 8) | d[j + 2]); j += 188
                else:
                    j += 1
            if 8 <= len(pids) <= 100:
                progs = pat_programs(LIVE)
                if progs:
                    return progs
            last = sz
        time.sleep(1.5)
    return []


def _spawn_chain(rf):
    """(Re)spawn tv_live on `rf` — full state reset. Caller MUST hold _tune_lock.
    Returns the sorted program list on lock, [] on failure."""
    global _cur_rf, _chain
    if _chain is not None:
        try:
            _chain.terminate(); _chain.wait(timeout=5)
        except Exception:
            try:
                _chain.kill()
            except Exception:
                pass
    subprocess.run(["pkill", "-f", "[t]v_live.py"], check=False)
    time.sleep(1)
    try:
        LIVE.unlink()
    except OSError:
        pass
    env = {**os.environ, **TUNE_ENV}
    _chain = subprocess.Popen(
        ["python3", "tv_live.py", "--rf", str(rf),
         "--rotate-gb", os.environ.get("STVT_ROTATE_GB", "8")],
        cwd=str(TOOLS), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)
    _cur_rf = rf
    return _wait_lock(rf)


def ensure_tuned(rf):
    """Tune the single SDR to `rf` if not already there. Returns the sorted
    program list (truthy) on lock, [] on failure."""
    with _tune_lock:
        if _cur_rf == rf and chain_alive():
            progs = pat_programs(LIVE)
            if progs:
                return progs
        return _spawn_chain(rf)


# ---- drought watchdog --------------------------------------------------------
DROUGHT_PIDS = int(os.environ.get("STVT_HDHR_DROUGHT_PIDS", "150"))
DROUGHT_STRIKES = int(os.environ.get("STVT_HDHR_DROUGHT_STRIKES", "3"))
SUPERVISE = os.environ.get("STVT_HDHR_SUPERVISE", "1") == "1"
_SUP_INTERVAL = int(os.environ.get("STVT_HDHR_SUP_INTERVAL", "15"))


def _live_unique_pids(nbytes=2_000_000):
    """(#unique PIDs, #packets) in the last nbytes of live.ts."""
    try:
        sz = LIVE.stat().st_size
        with open(LIVE, "rb") as f:
            f.seek(max(0, sz - nbytes)); d = f.read()
    except OSError:
        return 0, 0
    pids = set(); i = d.find(b"\x47"); n = 0
    while i >= 0 and i + 188 <= len(d):
        if d[i] == 0x47:
            pids.add(((d[i + 1] & 0x1f) << 8) | d[i + 2]); n += 1; i += 188
        else:
            i += 1
    return len(pids), n


def _supervisor():
    """Watch live.ts for a sustained decode DROUGHT — locked carrier but garbage
    output, seen as hundreds/thousands of unique PIDs (a healthy mux is ~20-50).
    On DROUGHT_STRIKES consecutive strikes, RESTART the chain: a full state reset,
    the proven cure for the MOD-12/OsO stream-phase slip that the in-chain
    re-acquire doesn't reliably clear on a slower CPU. This gives the streaming
    path the same self-healing stvt_run.sh has (a soak measured a ~12-minute
    unsupervised drought on RF36 — restart cuts that to ~1 minute)."""
    strikes = 0
    while True:
        time.sleep(_SUP_INTERVAL)
        if _cur_rf is None or not chain_alive():
            strikes = 0; continue
        uniq, n = _live_unique_pids()
        if n > 0 and uniq > DROUGHT_PIDS:
            strikes += 1
            if strikes >= DROUGHT_STRIKES:
                rf = _cur_rf
                print(f"[stvt_hdhr] DROUGHT on RF{rf} ({uniq} PIDs, "
                      f"{strikes} strikes) — restarting chain", flush=True)
                with _tune_lock:
                    if _cur_rf == rf and chain_alive():
                        _spawn_chain(rf)
                strikes = 0
        else:
            strikes = 0


_active_lock = threading.Lock()
_active = []


def stream_program(wfile, program):
    """Extract one program from the growing live.ts and stream it as MPEG-TS.
    No transcode (the client transcodes). Single tuner: kill any prior extractor
    so Jellyfin's probe/reconnect churn can't leak ffmpegs."""
    ff = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-f", "mpegts", "-i", "pipe:0",
         "-map", f"0:p:{program}", "-c", "copy", "-f", "mpegts", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    stop = threading.Event()
    with _active_lock:
        for old in _active:
            try:
                old.kill()
            except Exception:
                pass
        _active.clear()
        _active.append(ff)

    def feed():
        try:
            f = open(LIVE, "rb")
        except OSError:
            stop.set(); return
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 1_500_000))
        while not stop.is_set():
            b = f.read(65536)
            if b:
                try:
                    ff.stdin.write(b)
                except Exception:
                    break
            else:
                time.sleep(0.1)
        for c in (f.close, ff.stdin.close):
            try:
                c()
            except Exception:
                pass

    t = threading.Thread(target=feed, daemon=True); t.start()
    try:
        while True:
            chunk = ff.stdout.read(65536)
            if not chunk:
                break
            wfile.write(chunk)
    except Exception:
        pass
    finally:
        stop.set()
        try:
            ff.kill()
        except Exception:
            pass
        try:
            ff.stdout.close()
        except Exception:
            pass
        with _active_lock:
            if ff in _active:
                _active.remove(ff)
        t.join(timeout=2)


# ---- HTTP --------------------------------------------------------------------
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/discover.json"):
            return self._json({
                "FriendlyName": FRIENDLY, "Manufacturer": "STVT",
                "ModelNumber": "HDTC-2US", "FirmwareName": "stvt_atsc",
                "FirmwareVersion": "20260714", "DeviceID": DEVICE_ID,
                "DeviceAuth": "stvt", "TunerCount": 1,
                "BaseURL": BASE, "LineupURL": f"{BASE}/lineup.json"})
        if p == "/lineup_status.json":
            return self._json({"ScanInProgress": 0, "ScanPossible": 1,
                               "Source": "Antenna", "SourceList": ["Antenna"]})
        if p == "/lineup.json":
            return self._json([{"GuideNumber": e["GuideNumber"],
                                "GuideName": e["GuideName"],
                                "URL": e["URL"]} for e in load_lineup()])
        if p in ("/guide.xml", "/xmltv.xml"):
            body = guide_xml()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if p == "/lineup.m3u":
            lines = ["#EXTM3U"]
            for e in load_lineup():
                lines.append(f'#EXTINF:-1 tvg-id="{e["GuideNumber"]}" '
                             f'tvg-chno="{e["GuideNumber"]}",{e["GuideName"]}')
                lines.append(e["URL"])
            body = ("\n".join(lines) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if p.startswith("/auto/v"):
            gn = p[len("/auto/v"):]
            e = next((e for e in load_lineup() if e["GuideNumber"] == gn), None)
            if not e:
                self.send_error(404, "unknown channel"); return
            return self._do_stream(e["rf"], e["minor"])
        if p.startswith("/stream/"):
            parts = p.split("/")
            try:
                rf = int(parts[2]); minor = int(parts[3])
            except (IndexError, ValueError):
                self.send_error(400, "bad stream path"); return
            return self._do_stream(rf, minor)
        self.send_error(404)

    def _do_stream(self, rf, minor):
        progs = ensure_tuned(rf)
        if not progs:
            self.send_error(503, "tuner failed to lock"); return
        # minor (1-based subchannel index) -> program number from the live PAT.
        program = progs[minor - 1] if 1 <= minor <= len(progs) else progs[0]
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.end_headers()
        stream_program(self.wfile, program)


def main():
    if SUPERVISE:
        threading.Thread(target=_supervisor, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"[stvt_hdhr] serving on {BASE}  ({len(load_lineup())} channels)  "
          f"discover: {BASE}/discover.json  supervise={SUPERVISE}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if _chain is not None:
            try:
                _chain.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    main()
