"""tv_tuna_panel.py — the TV Tuna control panel (local web GUI).

Serves http://localhost:8642 : the EPG grid as buttons — click a station
to TUNE it (chain + solo player launch), click a show's REC to schedule
it on the DVR — plus a live status bar (station, stream health).

Backed by the same machinery as everything else: scan.json via
stvt_epg.load_epg (tuneability badges included), stvt_schedule add-show
for recording, tv_live + tv_watch for the screen.
"""
import json, os, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = Path(r"Z:\src\magic-tv-decoder\tools")
sys.path.insert(0, str(TOOLS)); sys.path.insert(0, str(HERE))
from stvt_epg import load_epg                      # grid + badges
from tv_lab import ts_metrics                      # liveness-guarded health

PY = r"C:\Users\user\radioconda\python.exe"
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
PORT = 8642

# per-RF calibrated gain (Philips profile era); default is a safe middle
GAINS = {36: (3, 40), 34: (2, 32), 15: (1, 32)}
DEFAULT_GAIN = (3, 40)

STATE = {"rf": None, "prog": None, "virtual": None, "name": None}
LOCK = threading.Lock()
GEN = [0]          # tune generation — rapid clicks cancel superseded launches


def base_env(rf):
    rfsel, ifgr = GAINS.get(rf, DEFAULT_GAIN)
    env = os.environ.copy()
    env["PATH"] = (r"C:\Program Files\SDRplay\API\x64;C:\ffmpeg\bin;"
                   + env.get("PATH", ""))
    env.update({"STVT_ANTENNA": "Antenna A", "STVT_IFGR": str(ifgr),
                "STVT_RFGAIN_SEL": str(rfsel), "STVT_EQ": "long",
                "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
                "STVT_DABNOTCH": "1", "STVT_RS": "stock", "STVT_SPS": "1.1",
                "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1",
                "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0"})
    return env


def kill_tv():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'tv_live|tv_watch' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue }"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "mpv.exe"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe"], capture_output=True)


def tune(rf, prog, virtual, name):
    with LOCK:
        GEN[0] += 1
        my_gen = GEN[0]
        kill_tv(); time.sleep(1)
        if GEN[0] != my_gen:
            return                      # superseded by a newer click
        env = base_env(rf)
        subprocess.Popen([PY, "-u", str(TOOLS / "tv_live.py"), "--rf", str(rf)],
                         env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        def player():
            time.sleep(26)
            if GEN[0] != my_gen:
                return                  # a newer tune took over meanwhile
            subprocess.Popen([PY, "-u", str(HERE / "tv_watch.py"), str(prog)],
                             env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        threading.Thread(target=player, daemon=True).start()
        STATE.update({"rf": rf, "prog": prog, "virtual": virtual, "name": name})


def record(virtual, title):
    p = subprocess.run([PY, str(TOOLS / "stvt_schedule.py"), "add-show",
                        virtual, title], capture_output=True, text=True,
                       timeout=60)
    return (p.stdout + p.stderr).strip()[-500:]


PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>TV Tuna 🐟</title><style>
body{font-family:Segoe UI,system-ui,sans-serif;background:#0b1220;color:#dce6f2;margin:0;padding:14px}
h1{font-size:20px;margin:0 0 4px} .sub{color:#7f96b3;font-size:12px;margin-bottom:12px}
#status{background:#152238;border:1px solid #26436b;border-radius:10px;padding:10px 14px;margin-bottom:14px;font-size:14px}
#status b{color:#67d18a}
table{border-collapse:collapse;width:100%;font-size:12px}
th{position:sticky;top:0;background:#0b1220;text-align:left;padding:6px;color:#7f96b3;border-bottom:1px solid #26436b}
td{padding:4px 6px;border-bottom:1px solid #17263e;vertical-align:top}
.ch{white-space:nowrap;font-weight:600}
.badge{display:inline-block;width:14px;text-align:center;border-radius:4px;margin-right:5px;font-weight:700}
.b-plus{background:#124a2a;color:#67d18a}.b-tilde{background:#4a3b12;color:#e7c96a}.b-x{background:#4a1a1a;color:#e77}
button{cursor:pointer;border:0;border-radius:6px;padding:3px 9px;font-size:11px}
.tune{background:#1e5fae;color:#fff;margin-right:4px}.tune:hover{background:#2f79d4}
.rec{background:#8a2635;color:#fff}.rec:hover{background:#b03040}
.show{color:#c7d5e8}.cont{color:#5f7591}
.now{outline:1px solid #2f79d4;border-radius:4px}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#152238;
border:1px solid #2f79d4;border-radius:8px;padding:10px 16px;display:none;max-width:70%;white-space:pre-wrap;font-size:12px}
</style></head><body>
<h1>TV Tuna 🐟 <span style="font-weight:400;color:#7f96b3">control panel</span></h1>
<div class="sub">click a station to tune &middot; click REC on a show to schedule the DVR &middot;
+ tuneable &middot; ~ weak &middot; x out of reach</div>
<div id="status">loading status…</div>
<div id="grid">loading guide…</div>
<div id="toast"></div>
<script>
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';
setTimeout(()=>t.style.display='none',6000)}
async function tune(rf,prog,virt,name){toast('tuning '+virt+' '+name+' — ~30s to picture');
await fetch('/api/tune',{method:'POST',body:JSON.stringify({rf,prog,virt,name})})}
async function rec(virt,title){toast('scheduling '+title+' …');
const r=await fetch('/api/record',{method:'POST',body:JSON.stringify({virt,title})});
toast(await r.text())}
async function refreshStatus(){try{const s=await (await fetch('/api/status')).json();
document.getElementById('status').innerHTML = s.tuned ?
 `watching <b>${s.virtual} ${s.name||''}</b> (RF${s.rf} p${s.prog}) &nbsp;·&nbsp; `+
 `stream: <b>${s.hdrs_s??'—'}</b> hdrs/s &nbsp; <b>${s.gaps_min??'—'}</b> gaps/min &nbsp; ${s.real_pct??'—'}% real`
 : 'not tuned — click a station';}catch(e){}}
async function loadGrid(){const g=await (await fetch('/api/grid')).json();
let h='<table><tr><th>station</th>';g.slots.forEach(s=>h+='<th>'+s+'</th>');h+='<th></th></tr>';
g.rows.forEach(r=>{const bc=r.tune==='+'?'b-plus':(r.tune==='~'?'b-tilde':'b-x');
const s=r.snr||0; const bars=s>=55?'▂▄▆█':s>=48?'▂▄▆':s>=40?'▂▄':s>0?'▂':'';
const scol=s>=55?'#67d18a':s>=48?'#a8d167':s>=40?'#e7c96a':'#e77';
h+=`<tr><td class="ch"><span class="badge ${bc}">${r.tune}</span>`+
`<button class="tune" onclick='tune(${r.rf},${r.prog},"${r.virtual}","${r.callsign}")'>${r.virtual}</button> ${r.callsign} `+
`<span title="pilot SNR" style="color:${scol};letter-spacing:1px">${bars}</span>`+
`<span style="color:#5f7591;font-size:10px"> ${s?s+'dB':''}</span></td>`;
r.cells.forEach((c,i)=>{h+=`<td class="${i===0?'now':''}">`+(c.cont?`<span class="cont">&raquo; ${c.title}</span>`
:(c.title?`<span class="show">${c.title}</span>`:'<span class="cont">—</span>'))+'</td>';});
const nowT=(r.cells[0]&&r.cells[0].title)||'';
h+=`<td>${nowT?`<button class="rec" onclick='rec("${r.virtual}",${JSON.stringify(nowT)})'>REC</button>`:''}</td></tr>`});
h+='</table>';document.getElementById('grid').innerHTML=h}
loadGrid();refreshStatus();setInterval(refreshStatus,10000);setInterval(loadGrid,300000);
</script></body></html>"""


def grid_json():
    channels, _ = load_epg()
    now = time.time()
    slot0 = int(now // 1800) * 1800
    slots = [slot0 + i * 1800 for i in range(4)]
    slot_labels = [time.strftime("%I:%M %p", time.localtime(s)).lstrip("0")
                   for s in slots]
    rows = []
    for ch in channels:
        cells = []
        prev_title = None
        for s in slots:
            ev = next((e for e in ch["events"]
                       if e["start_unix"] <= s < e["start_unix"] + e["length"]), None)
            title = (ev or {}).get("title") or ""
            title = "".join(c if 32 <= ord(c) < 0x2500 else "?" for c in title)[:34]
            cells.append({"title": title, "cont": bool(title) and title == prev_title})
            prev_title = title
        rows.append({"rf": ch["rf"], "prog": ch["program"],
                     "virtual": ch["virtual"], "callsign": ch["callsign"],
                     "tune": ch.get("tune", " "), "snr": ch.get("snr_db"),
                     "cells": cells})
    return {"slots": slot_labels, "rows": rows}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.path == "/":
            self._send(PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/grid":
            self._send(json.dumps(grid_json()))
        elif self.path == "/api/status":
            st = dict(STATE)
            st["tuned"] = st["rf"] is not None
            m = ts_metrics(20) if st["tuned"] else None
            if m:
                st.update({"hdrs_s": round(m["hdrs_s"], 1),
                           "gaps_min": round(m["gaps_min"], 1),
                           "real_pct": round(m["real_pct"])})
            self._send(json.dumps(st))
        else:
            self.send_error(404)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/tune":
            threading.Thread(target=tune,
                             args=(req["rf"], req["prog"], req["virt"],
                                   req.get("name", "")), daemon=True).start()
            self._send('"tuning"')
        elif self.path == "/api/record":
            out = record(req["virt"], req["title"])
            self._send(out or "scheduled", "text/plain; charset=utf-8")
        else:
            self.send_error(404)


if __name__ == "__main__":
    print(f"TV Tuna panel: http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
