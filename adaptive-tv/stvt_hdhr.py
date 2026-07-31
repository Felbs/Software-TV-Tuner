#!/usr/bin/env python3
"""stvt_hdhr.py — HDHomeRun-compatible tuner server.

Turns the Pi (SDR + antenna) into a network tuner that Jellyfin / Plex / Emby
auto-detect. It serves the ATSC mux as per-program MPEG-TS over HTTP, so a
decode-capable client (Jellyfin server on the PC → transcode → iOS app) does
the video work — the Pi only tunes. This is the fix for the Pi's skip problem:
the Pi has no hardware MPEG-2 decode, so it should never decode, only tune.

Endpoints (HDHomeRun HTTP API + generic M3U):
  /discover.json        device descriptor  (how servers auto-detect it)
  /lineup.json          channel list       (virtual chan + stream URL)
  /lineup_status.json   scan status
  /lineup.m3u           M3U playlist        (generic-tuner path)
  /stream/<rf>/<prog>   live MPEG-TS of one program (tunes on demand)

Single tuner: one RF (6 MHz) at a time. All sub-channels of the tuned RF and
multiple clients on it are fine; switching to a different RF re-tunes and
interrupts current viewers. Streaming reads the chain's live.ts FILE (never the
live GR chain — a slow consumer used to back-pressure and stall the decode), so
consumers are fully decoupled. On tune it goes headless (kills the Pi's local
mpv) so the whole CPU goes to the tuner chain → clean stream.
"""
import json, os, socket, subprocess, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
SCAN = HOME / "Software-TV-Tuner/adaptive-tv/lab/scans/Antenna_B.json"
LIVE = HOME / "Software-TV-Tuner/tools/data/tv_live/live.ts"
PANEL = "http://localhost:8642"
PORT = int(os.environ.get("STVT_HDHR_PORT", "5004"))
DEVICE_ID = os.environ.get("STVT_HDHR_ID", "STVT0001")
FRIENDLY = os.environ.get("STVT_HDHR_NAME", "STVT Tuner")


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
    """Build the channel lineup from the antenna scan's PSIP tables."""
    try:
        d = json.load(open(SCAN))
    except Exception:
        return []
    out = []
    for r in d.get("scan", {}).get("channels", []):
        if not r.get("lock"):
            continue
        rf = r["rf"]
        for ch in (r.get("psip") or {}).get("channels", []):
            maj, mn, pn = ch.get("major"), ch.get("minor"), ch.get("program_number")
            if maj is None or pn is None:
                continue
            gn = f"{maj}.{mn}"
            name = ch.get("short_name") or r.get("callsign") or f"RF{rf}"
            out.append({"GuideNumber": gn, "GuideName": name,
                        "URL": f"{BASE}/stream/{rf}/{pn}",
                        "rf": rf, "prog": pn, "virt": gn})
    # de-dup by GuideNumber (a program can appear once)
    seen = {}
    for e in out:
        seen.setdefault(e["GuideNumber"], e)
    return list(seen.values())


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def guide_xml():
    """Minimal but valid XMLTV so Jellyfin's Live TV setup completes. Channels
    + rolling generic blocks; real PSIP EPG enrichment is a later phase."""
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
        for i in range(8):          # 8 x 3h blocks = -6h .. +18h
            s = start + i * 3 * 3600
            out.append(f'<programme start="{ts(s)}" stop="{ts(s + 3 * 3600)}" '
                       f'channel="{e["GuideNumber"]}"><title>{_esc(e["GuideName"])}'
                       f'</title><desc>Live</desc></programme>')
    out.append('</tv>')
    return "\n".join(out).encode()


# ---- single-tuner management -------------------------------------------------
_tune_lock = threading.Lock()


def panel_get(path):
    try:
        return json.load(urllib.request.urlopen(PANEL + path, timeout=8))
    except Exception:
        return {}


def panel_tune(rf, prog, virt, name):
    body = json.dumps({"rf": rf, "prog": prog, "virt": virt,
                       "name": name, "antenna": "Antenna B"}).encode()
    req = urllib.request.Request(PANEL + "/api/tune", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception:
        pass


def ensure_tuned(rf, prog, virt, name):
    """Tune the single SDR to rf if not already there. Returns True on lock."""
    with _tune_lock:
        st = panel_get("/api/status")
        if st.get("tuned") and st.get("rf") == rf:
            return True
        panel_tune(rf, prog, virt, name)
        # headless: free the Pi's CPU from local video decode (pkill -x = exact
        # process name 'mpv', never matches this python — avoids the pkill-self
        # gotcha). We watch on the client, not the Pi.
        subprocess.run(["pkill", "-x", "mpv"], check=False)
        for _ in range(25):
            time.sleep(2)
            st = panel_get("/api/status")
            if st.get("tuned") and not st.get("tuning") and st.get("rf") == rf:
                time.sleep(2)
                return True
        return False


_active_lock = threading.Lock()
_active = []          # running extractor Popens (single tuner → keep one)


def stream_program(wfile, prog):
    """Extract one program from the growing live.ts and stream it as MPEG-TS.

    Feeds ffmpeg from ~the live edge of live.ts (file, not the chain) and copies
    program `prog` through untouched. No transcode here — the client transcodes.
    Single tuner: killing any prior extractor on a new request keeps exactly one
    running, so Jellyfin's probe/reconnect churn can't leak ffmpegs.
    """
    ff = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-f", "mpegts", "-i", "pipe:0",
         "-map", f"0:p:{prog}", "-c", "copy", "-f", "mpegts", "pipe:1"],
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
        f.seek(max(0, f.tell() - 1_500_000))   # start near live edge (gets PSI fast)
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
            wfile.write(chunk)          # raises when the client disconnects
    except Exception:
        pass
    finally:
        stop.set()
        try:
            ff.kill()                   # SIGKILL — terminate() let ffmpeg linger
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
                "FirmwareVersion": "20260713", "DeviceID": DEVICE_ID,
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
        if p.startswith("/auto/v"):        # HDHomeRun-standard stream path
            gn = p[len("/auto/v"):]
            e = next((e for e in load_lineup() if e["GuideNumber"] == gn), None)
            if not e:
                self.send_error(404, "unknown channel"); return
            return self._do_stream(e["rf"], e["prog"], e["virt"], e["GuideName"])
        if p.startswith("/stream/"):        # our own lineup-URL path
            parts = p.split("/")
            try:
                rf = int(parts[2]); prog = int(parts[3])
            except (IndexError, ValueError):
                self.send_error(400, "bad stream path"); return
            e = next((e for e in load_lineup()
                      if e["rf"] == rf and e["prog"] == prog), None)
            virt = e["virt"] if e else f"{rf}.{prog}"
            name = e["GuideName"] if e else f"RF{rf}"
            return self._do_stream(rf, prog, virt, name)
        self.send_error(404)

    def _do_stream(self, rf, prog, virt, name):
        if not ensure_tuned(rf, prog, virt, name):
            self.send_error(503, "tuner failed to lock"); return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.end_headers()                  # streamed: no Content-Length
        stream_program(self.wfile, prog)


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"[stvt_hdhr] serving on {BASE}  ({len(load_lineup())} channels)  "
          f"discover: {BASE}/discover.json", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
