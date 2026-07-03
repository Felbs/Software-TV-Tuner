"""tv_tuna_panel.py — the TV Tuna control panel (local web GUI).

http://localhost:8642
  GUIDE tab   — EPG grid as buttons: click a station to tune, REC to DVR,
                tuneability badges + per-tower signal meters.
  NERD tab    — "stats for nerds": every tuning knob and its value, live
                decode math (MER, input level, stream health), and a wide
                UHF waterfall (dark=quiet, bright=power) with SDR-style
                tuning cursor: center target + channel-edge lines.
                One tuner = one 8 MHz window: while TV plays, the sweep
                pauses (banner explains) and resumes when the radio idles.
"""
import json, math, os, re, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = Path(r"Z:\src\magic-tv-decoder\tools")
sys.path.insert(0, str(TOOLS)); sys.path.insert(0, str(HERE))
from stvt_epg import load_epg
from tv_lab import ts_metrics

PY = r"C:\Users\user\radioconda\python.exe"
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
CHAIN_LOG = HERE / "lab" / "panel_chain.log"
PORT = 8642

GAINS = {36: (3, 40), 34: (2, 32), 15: (1, 32)}
DEFAULT_GAIN = (3, 40)
RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
RE_FPLL = re.compile(r"mean\|x\|=([\d.]+).*?max\|x\|=([\d.]+)\s+in_rms=([\d.]+)")

STATE = {"rf": None, "prog": None, "virtual": None, "name": None,
         "tuning": False, "env": {}}
LOCK = threading.Lock()
GEN = [0]

# ── waterfall sweeper ──────────────────────────────────────────────
WF = {"rows": [], "freqs": None, "status": "starting", "row_id": 0}
WF_LOCK = threading.Lock()
SWEEP_LO, SWEEP_HI, HOP = 473e6, 605e6, 6e6   # UHF TV band, 6 MHz hops

def chain_running():
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                        "| Where-Object { $_.CommandLine -match 'tv_live' }).Count"],
                       capture_output=True, text=True, timeout=20)
    try: return int((r.stdout or "0").strip() or 0) > 0
    except ValueError: return False

def sweeper():
    import numpy as np
    try:
        import SoapySDR
        SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
        from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
    except Exception as e:
        with WF_LOCK: WF["status"] = f"SoapySDR unavailable: {e}"
        return
    FFT = 1024
    win = np.hanning(FFT).astype(np.float32)
    hops = []
    f = SWEEP_LO
    while f <= SWEEP_HI + 1:
        hops.append(f); f += HOP
    keep = int(FFT * (HOP / 8e6))          # central 6 of 8 MHz per hop
    lo_k = (FFT - keep) // 2
    freqs = []
    for h in hops:
        freqs += list((h + (np.arange(keep) - keep / 2) * (8e6 / FFT)) / 1e6)
    with WF_LOCK: WF["freqs"] = [round(x, 2) for x in freqs]
    while True:
        if STATE["rf"] is not None or STATE["tuning"] or chain_running():
            with WF_LOCK:
                WF["status"] = ("tuner busy watching " +
                                str(STATE.get("virtual") or "TV") +
                                " — sweep paused (one tuner = one 8 MHz window)")
            time.sleep(5); continue
        try:
            sdr = SoapySDR.Device("driver=sdrplay")
        except Exception:
            with WF_LOCK: WF["status"] = "radio busy — waiting"
            time.sleep(5); continue
        try:
            sdr.setSampleRate(SOAPY_SDR_RX, 0, 8_000_000)
            try: sdr.setGainMode(SOAPY_SDR_RX, 0, False)
            except Exception: pass
            sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 40.0)
            try: sdr.writeSetting("rfgain_sel", "3")
            except Exception: pass
            sdr.setAntenna(SOAPY_SDR_RX, 0, "Antenna A")
            st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
            sdr.activateStream(st)
            buf = np.empty(FFT, dtype=np.complex64)
            def external_wants_sdr():
                # any fresh live.ts write means some chain (ours or an
                # external tool) is using / about to use the radio
                try:
                    return time.time() - LIVE.stat().st_mtime < 6
                except OSError:
                    return False
            while not (STATE["rf"] is not None or STATE["tuning"]
                       or external_wants_sdr()):
                row = []
                t_row = time.strftime("%H:%M:%S")
                for h in hops:
                    sdr.setFrequency(SOAPY_SDR_RX, 0, h)
                    time.sleep(0.025)
                    acc = np.zeros(FFT); n = 0
                    t0 = time.time()
                    while time.time() - t0 < 0.055:
                        srr = sdr.readStream(st, [buf], FFT, timeoutUs=120000)
                        if srr.ret == FFT:
                            acc += np.abs(np.fft.fftshift(
                                np.fft.fft(buf * win)))**2
                            n += 1
                    psd = acc / max(1, n)
                    db = 10 * np.log10(psd[lo_k:lo_k + keep] + 1e-12)
                    row += list(db)
                row = np.array(row)
                row -= np.percentile(row, 10)          # floor-relative
                row = np.clip(row / 45.0, 0, 1)        # 0..1 for colormap
                with WF_LOCK:
                    WF["rows"].append({"t": t_row,
                                       "v": [round(float(x), 3) for x in row]})
                    WF["rows"][:-90] = []
                    WF["row_id"] += 1
                    WF["status"] = "sweeping 473–611 MHz"
            sdr.deactivateStream(st); sdr.closeStream(st)
        except Exception as e:
            with WF_LOCK: WF["status"] = f"sweep error: {e}"
            time.sleep(4)
        finally:
            try: del sdr
            except Exception: pass
        time.sleep(1)

# ── tuning / recording actions ─────────────────────────────────────
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
                "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
                "STVT_EQ_TELEM": "1"})
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
        STATE.update({"tuning": True})
        kill_tv(); time.sleep(2)
        if GEN[0] != my_gen:
            return
        env = base_env(rf)
        logf = open(CHAIN_LOG, "w")
        subprocess.Popen([PY, "-u", str(TOOLS / "tv_live.py"), "--rf", str(rf)],
                         env=env, stdout=logf, stderr=subprocess.STDOUT)
        def player():
            time.sleep(26)
            if GEN[0] != my_gen:
                return
            subprocess.Popen([PY, "-u", str(HERE / "tv_watch.py"), str(prog)],
                             env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            STATE.update({"tuning": False})
        threading.Thread(target=player, daemon=True).start()
        knobs = {k.replace("STVT_", ""): v for k, v in env.items()
                 if k.startswith("STVT_")}
        STATE.update({"rf": rf, "prog": prog, "virtual": virtual,
                      "name": name, "env": knobs})

def stop_tv():
    with LOCK:
        GEN[0] += 1
        kill_tv()
        STATE.update({"rf": None, "prog": None, "virtual": None,
                      "name": None, "tuning": False, "env": {}})

def record(virtual, title):
    p = subprocess.run([PY, str(TOOLS / "stvt_schedule.py"), "add-show",
                        virtual, title], capture_output=True, text=True,
                       timeout=60)
    return (p.stdout + p.stderr).strip()[-500:]

def live_math():
    out = {}
    try:
        text = CHAIN_LOG.read_text(errors="ignore")[-40000:]
        errs = [float(m.group(1)) for m in RE_FS.finditer(text)][-24:]
        if errs:
            mers = [20 * math.log10(5.0 / e) for e in errs if e > 0]
            if mers:
                out["mer_db"] = round(sum(mers) / len(mers), 2)
                out["mer_last"] = round(mers[-1], 2)
        fp = RE_FPLL.findall(text)
        if fp:
            mn, mx, ir = fp[-1]
            out.update({"mean_x": float(mn), "max_x": float(mx),
                        "in_rms": float(ir)})
    except OSError:
        pass
    m = ts_metrics(20) if STATE["rf"] is not None else None
    if m:
        out.update({"hdrs_s": round(m["hdrs_s"], 1),
                    "gaps_min": round(m["gaps_min"], 1),
                    "real_pct": round(m["real_pct"])})
    return out

# ── the page ───────────────────────────────────────────────────────
PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>TV Tuna 🐟</title><style>
body{font-family:Segoe UI,system-ui,sans-serif;background:#05080f;color:#dce6f2;margin:0;padding:12px}
h1{font-size:20px;margin:0 0 2px}.sub{color:#7f96b3;font-size:12px;margin-bottom:10px}
.tabs{margin-bottom:12px}.tabs button{background:#152238;color:#9fb4d0;border:1px solid #26436b;
padding:7px 18px;border-radius:8px 8px 0 0;cursor:pointer;font-size:13px}
.tabs button.on{background:#1e5fae;color:#fff;border-color:#1e5fae}
#status{background:#0d1626;border:1px solid #26436b;border-radius:10px;padding:9px 14px;margin-bottom:12px;font-size:14px}
#status b{color:#67d18a}
table{border-collapse:collapse;width:100%;font-size:12px}
th{position:sticky;top:0;background:#05080f;text-align:left;padding:6px;color:#7f96b3;border-bottom:1px solid #26436b}
td{padding:4px 6px;border-bottom:1px solid #101c30;vertical-align:top}
.ch{white-space:nowrap;font-weight:600}
.badge{display:inline-block;width:14px;text-align:center;border-radius:4px;margin-right:5px;font-weight:700}
.b-plus{background:#124a2a;color:#67d18a}.b-tilde{background:#4a3b12;color:#e7c96a}.b-x{background:#4a1a1a;color:#e77}
button{cursor:pointer;border:0;border-radius:6px;padding:3px 9px;font-size:11px}
.tune{background:#1e5fae;color:#fff;margin-right:4px}.tune:hover{background:#2f79d4}
.rec{background:#8a2635;color:#fff}.rec:hover{background:#b03040}
.stop{background:#333e52;color:#dce6f2;margin-left:8px}
.show{color:#c7d5e8}.cont{color:#5f7591}.now{outline:1px solid #2f79d4;border-radius:4px}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#152238;
border:1px solid #2f79d4;border-radius:8px;padding:10px 16px;display:none;max-width:70%;white-space:pre-wrap;font-size:12px;z-index:9}
.cards{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.card{background:#0d1626;border:1px solid #26436b;border-radius:10px;padding:8px 12px;min-width:96px}
.card .k{color:#7f96b3;font-size:10px;text-transform:uppercase;letter-spacing:.5px}
.card .v{font-size:18px;font-weight:700;color:#e8f1fc;margin-top:2px}
.card .u{font-size:10px;color:#5f7591}
.good{color:#67d18a!important}.warn{color:#e7c96a!important}.bad{color:#e77!important}
#wfwrap{background:#000;border:1px solid #26436b;border-radius:10px;padding:8px}
#wfstatus{font-size:11px;color:#7f96b3;margin-bottom:6px}
canvas{width:100%;image-rendering:pixelated;display:block;border-radius:4px}
.edu{color:#5f7591;font-size:11px;margin-top:8px;line-height:1.6}
.edu b{color:#9fb4d0}
</style></head><body>
<h1>TV Tuna 🐟 <span style="font-weight:400;color:#7f96b3">control panel</span></h1>
<div class="sub">one tuner &middot; six towers &middot; every knob measured, nothing guessed</div>
<div class="tabs"><button id="tabG" class="on" onclick="showTab('G')">GUIDE</button>
<button id="tabN" onclick="showTab('N')">STATS FOR NERDS</button></div>
<div id="status">loading…</div>
<div id="pageG"><div id="grid">loading guide…</div></div>
<div id="pageN" style="display:none">
  <div class="cards" id="mathcards"></div>
  <div class="cards" id="knobcards"></div>
  <div id="wfwrap"><div id="wfstatus">waterfall starting…</div>
    <canvas id="wfchan" width="1480" height="34"></canvas>
    <canvas id="wf" width="1480" height="380" style="cursor:grab"></canvas>
    <canvas id="wfaxis" width="1480" height="22"></canvas>
    <div style="color:#5f7591;font-size:10px;margin-top:3px">scroll = zoom &middot; middle-drag (or shift-drag) = pan &middot; double-click = reset view</div></div>
  <div class="edu"><b>How to read this:</b> the waterfall paints the whole UHF TV band —
  each vertical stripe is a frequency, brightness is received power, time scrolls downward.
  A television transmitter looks like a wide bright <b>plateau exactly 6&nbsp;MHz across</b>
  (that's one "channel" carrying 5&ndash;10 stations). Narrow bright needles are not TV —
  they're other services or interference. The blue directory bar on top maps each channel's
  tenant stations — but here's the secret: <b>subchannels don't own slices of spectrum</b>.
  All of a channel's stations share the whole 6 MHz <b>by taking turns, packet by packet,
  thousands of times a second</b> (time multiplexing) — the segments are a directory,
  not a frequency map. The <b>yellow center line</b> marks where the tuner's
  local oscillator is parked; the two <b>dashed edge lines</b> show the 6 MHz slice the matched
  filter accepts — everything outside them is rejected before decoding. Knobs above:
  <b>MER</b> is decode margin (the cliff is 15.2 dB — below it, television stops existing),
  <b>in_rms</b> is signal level after the front end, <b>max|x|</b> near 1.57 means clipping,
  <b>gaps/min</b> counts stream holes the eye would see as glitches.</div>
</div>
<div id="toast"></div>
<script>
let TAB='G';
function showTab(t){TAB=t;document.getElementById('pageG').style.display=t==='G'?'':'none';
document.getElementById('pageN').style.display=t==='N'?'':'none';
document.getElementById('tabG').className=t==='G'?'on':'';
document.getElementById('tabN').className=t==='N'?'on':'';}
function toast(m){const el=document.getElementById('toast');el.textContent=m;el.style.display='block';
setTimeout(()=>el.style.display='none',6000)}
async function tune(rf,prog,virt,name){
if(rf<14){toast('⚠ '+virt+' '+name+' rides RF'+rf+' — VHF. The scanner locks it but the play chain needs a VHF gain recipe we have not cracked yet (tomorrow\\'s research). Pick a UHF station (4.x, 5.x, 14.x, 20.x, 44.x, 66.x, 68.x).');return}
toast('tuning '+virt+' '+name+' — ~30s to picture');
await fetch('/api/tune',{method:'POST',body:JSON.stringify({rf,prog,virt,name})})}
async function stopTv(){await fetch('/api/stop',{method:'POST'});toast('TV stopped — tuner idle, waterfall resumes')}
async function rec(virt,title){toast('scheduling '+title+' …');
const r=await fetch('/api/record',{method:'POST',body:JSON.stringify({virt,title})});toast(await r.text())}
async function refreshStatus(){try{const s=await (await fetch('/api/status')).json();
let h;
if(s.tuning) h='⏳ <b>tuning '+(s.virtual||'')+' '+(s.name||'')+'…</b> chain locking + player launch ≈ 30 s';
else if(s.tuned) h=`watching <b>${s.virtual} ${s.name||''}</b> (RF${s.rf} p${s.prog})`+
 ` · <b>${s.hdrs_s??'—'}</b> hdrs/s · <b>${s.gaps_min??'—'}</b> gaps/min · ${s.real_pct??'—'}% real`+
 ` <button class="stop" onclick="stopTv()">stop TV</button>`;
else h='tuner idle — waterfall live on NERD tab · click a station to tune';
document.getElementById('status').innerHTML=h;}catch(e){}}
async function loadGrid(){const g=await (await fetch('/api/grid')).json();
let h='<table><tr><th>station</th>';g.slots.forEach(s=>h+='<th>'+s+'</th>');h+='<th></th></tr>';
g.rows.forEach(r=>{const bc=r.tune==='+'?'b-plus':(r.tune==='~'?'b-tilde':'b-x');
const s=r.snr||0;const bars=s>=55?'▂▄▆█':s>=48?'▂▄▆':s>=40?'▂▄':s>0?'▂':'';
const scol=s>=55?'#67d18a':s>=48?'#a8d167':s>=40?'#e7c96a':'#e77';
h+=`<tr><td class="ch"><span class="badge ${bc}">${r.tune}</span>`+
`<button class="tune" onclick='tune(${r.rf},${r.prog},"${r.virtual}","${r.callsign}")'>${r.virtual}</button> ${r.callsign} `+
`<span style="color:${scol};letter-spacing:1px" title="pilot SNR">${bars}</span>`+
`<span style="color:#5f7591;font-size:10px"> ${s?s+'dB':''}</span></td>`;
r.cells.forEach((c,i)=>{h+=`<td class="${i===0?'now':''}">`+(c.cont?`<span class="cont">&raquo; ${c.title}</span>`
:(c.title?`<span class="show">${c.title}</span>`:'<span class="cont">—</span>'))+'</td>'});
const nowT=(r.cells[0]&&r.cells[0].title)||'';
h+=`<td>${nowT?`<button class="rec" onclick='rec("${r.virtual}",${JSON.stringify(nowT)})'>REC</button>`:''}</td></tr>`});
h+='</table>';document.getElementById('grid').innerHTML=h}
function card(k,v,u,cls){return `<div class="card"><div class="k">${k}</div>`+
`<div class="v ${cls||''}">${v}</div><div class="u">${u||''}</div></div>`}
async function refreshNerd(){if(TAB!=='N')return;
try{const s=await (await fetch('/api/nerd')).json();
let mc='';const L=s.live||{};
if(L.mer_db!==undefined){const c=L.mer_db>=17?'good':(L.mer_db>=15.2?'warn':'bad');
mc+=card('MER',L.mer_db,'dB · cliff 15.2',c)}
if(L.in_rms!==undefined)mc+=card('in_rms',L.in_rms,'front-end level');
if(L.max_x!==undefined)mc+=card('max|x|',L.max_x,(L.max_x>=1.5?'CLIPPING':'headroom'),L.max_x>=1.5?'bad':'good');
if(L.hdrs_s!==undefined)mc+=card('video',L.hdrs_s,'headers / s');
if(L.gaps_min!==undefined)mc+=card('gaps',L.gaps_min,'per min',L.gaps_min<=3?'good':(L.gaps_min<=12?'warn':'bad'));
if(L.real_pct!==undefined)mc+=card('stream',L.real_pct+'%','real packets');
document.getElementById('mathcards').innerHTML=mc||card('tuner','idle','click a station');
let kc='';const K=s.knobs||{};
['RF','PROG','RFGAIN_SEL','IFGR','EQ','VITERBI','RS','SPS','RRC_SYMS','RFNOTCH','DABNOTCH','EQ_LKG'].forEach(k=>{
if(s.knobs && (k in K || k==='RF'||k==='PROG')){
const v=k==='RF'?s.rf:(k==='PROG'?s.prog:K[k]);if(v!==undefined&&v!==null)kc+=card(k,v,'')}});
document.getElementById('knobcards').innerHTML=kc;
}catch(e){}}
// ── waterfall v2: client row buffer + pan/zoom + channel overlay ──
let lastRow=0, wfFreqs=null, wfChans=null, tunedMhz=null;
let ROWS=[];                       // newest first, full-band values
const MAXROWS=220, rowH=3;
let vf0=null, vf1=null;            // visible frequency window (MHz)
const cv=document.getElementById('wf'),ctx=cv.getContext('2d');
const ax=document.getElementById('wfaxis'),axx=ax.getContext('2d');
const cb=document.getElementById('wfchan'),cbx=cb.getContext('2d');
function color(v){const r=v<0.6?0:Math.round((v-0.6)/0.4*255);
const g=v<0.3?Math.round(v/0.3*40):Math.round(40+((v-0.3)/0.7*215));
const b=v<0.3?Math.round(30+v/0.3*180):Math.round(210+(v-0.3)/0.7*45);
return `rgb(${r},${g},${Math.min(255,b)})`}
function fullRange(){return [wfFreqs[0], wfFreqs[wfFreqs.length-1]]}
function xOf(mhz){return (mhz-vf0)/(vf1-vf0)*cv.width}
function redraw(){if(!wfFreqs||!ROWS.length)return;
const W=cv.width,H=cv.height,n=wfFreqs[wfFreqs.length-1]-wfFreqs[0];
const N=ROWS[0].v.length, ff0=wfFreqs[0];
const i0=Math.max(0,Math.floor((vf0-ff0)/n*N)), i1=Math.min(N,Math.ceil((vf1-ff0)/n*N));
ctx.fillStyle='#000';ctx.fillRect(0,0,W,H);
const nr=Math.min(ROWS.length,Math.floor(H/rowH));
for(let r=0;r<nr;r++){const v=ROWS[r].v;
for(let x=0;x<W;x++){const idx=i0+Math.floor(x*(i1-i0)/W);
ctx.fillStyle=color(v[idx]||0);ctx.fillRect(x,r*rowH,1,rowH)}}
drawOverlays()}
function drawOverlays(){const W=cv.width;
// axis
axx.clearRect(0,0,W,ax.height);axx.fillStyle='#7f96b3';axx.font='10px monospace';
const span=vf1-vf0, step=span>90?12:(span>40?6:(span>12?3:1));
for(let m=Math.ceil(vf0/step)*step;m<vf1;m+=step){const x=xOf(m);
axx.fillRect(x,0,1,4);axx.fillText(m,x-11,16)}
// channel directory bar with subchannel carve-outs
cbx.clearRect(0,0,W,cb.height);
if(wfChans){for(const rf in wfChans){const c=wfChans[rf];
if(c.hi<vf0||c.lo>vf1)continue;
const x0=Math.max(0,xOf(c.lo)),x1=Math.min(W,xOf(c.hi));
cbx.fillStyle='rgba(30,95,174,0.25)';cbx.fillRect(x0,0,x1-x0,cb.height);
cbx.strokeStyle='#26436b';cbx.strokeRect(x0,0,x1-x0,cb.height);
const nsub=c.subs.length,segW=(x1-x0)/Math.max(1,nsub);
cbx.font='9px monospace';
for(let i=0;i<nsub;i++){cbx.strokeStyle='rgba(38,67,107,0.7)';
cbx.beginPath();cbx.moveTo(x0+i*segW,cb.height/2);cbx.lineTo(x0+i*segW,cb.height);cbx.stroke();
if(segW>34){cbx.fillStyle='#9fb4d0';
cbx.fillText(c.subs[i].split(' ')[0],x0+i*segW+2,cb.height-4)}}
if(x1-x0>44){cbx.fillStyle='#dce6f2';cbx.font='bold 10px monospace';
cbx.fillText('RF'+rf+' '+c.label,x0+3,12)}}}
// channel boundary lines on the waterfall
ctx.save();ctx.strokeStyle='rgba(127,150,179,0.22)';
if(wfChans)for(const rf in wfChans){const c=wfChans[rf];
[[c.lo],[c.hi]].forEach(([m])=>{if(m>=vf0&&m<=vf1){const x=xOf(m);
ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,cv.height);ctx.stroke()}})}
// tuning cursor: center target + channel-edge lines
if(tunedMhz!==null&&tunedMhz>=vf0&&tunedMhz<=vf1){const cx=xOf(tunedMhz);
ctx.strokeStyle='rgba(255,211,77,0.95)';ctx.beginPath();ctx.moveTo(cx,0);ctx.lineTo(cx,cv.height);ctx.stroke();
axx.fillStyle='#ffd34d';axx.fillText('▲ tuned',cx-18,21);
const half=3/(vf1-vf0)*W;ctx.strokeStyle='rgba(255,211,77,0.5)';ctx.setLineDash([4,4]);
ctx.beginPath();ctx.moveTo(cx-half,0);ctx.lineTo(cx-half,cv.height);ctx.stroke();
ctx.beginPath();ctx.moveTo(cx+half,0);ctx.lineTo(cx+half,cv.height);ctx.stroke();ctx.setLineDash([])}
ctx.restore()}
async function refreshWf(){if(TAB!=='N')return;
try{const w=await (await fetch('/api/waterfall?since='+lastRow)).json();
document.getElementById('wfstatus').textContent=w.status+(w.tuned_mhz?'   ·   tuned: '+w.tuned_mhz+' MHz':'');
if(w.freqs&&!wfFreqs){wfFreqs=w.freqs;[vf0,vf1]=fullRange()}
if(w.chans)wfChans=w.chans;
tunedMhz=w.tuned_mhz?parseFloat(w.tuned_mhz):null;
if(w.rows&&w.rows.length&&wfFreqs){
for(const row of w.rows)ROWS.unshift(row);
ROWS=ROWS.slice(0,MAXROWS);lastRow=w.row_id}
redraw()}catch(e){}}
// pan / zoom
let panning=false,panX=0,panF0=0,panF1=0;
cv.addEventListener('wheel',e=>{e.preventDefault();if(!wfFreqs)return;
const [F0,F1]=fullRange();const mx=vf0+(e.offsetX/cv.clientWidth)*(vf1-vf0);
const z=e.deltaY>0?1.25:0.8;let nf0=mx-(mx-vf0)*z,nf1=mx+(vf1-mx)*z;
if(nf1-nf0>F1-F0){nf0=F0;nf1=F1}
if(nf1-nf0<2){return}
vf0=Math.max(F0,nf0);vf1=Math.min(F1,nf1);redraw()},{passive:false});
cv.addEventListener('mousedown',e=>{if(e.button===1||(e.button===0&&e.shiftKey)){
e.preventDefault();panning=true;panX=e.clientX;panF0=vf0;panF1=vf1;cv.style.cursor='grabbing'}});
window.addEventListener('mousemove',e=>{if(!panning)return;
const [F0,F1]=fullRange();const df=(panX-e.clientX)/cv.clientWidth*(panF1-panF0);
let nf0=panF0+df,nf1=panF1+df;
if(nf0<F0){nf0=F0;nf1=F0+(panF1-panF0)}if(nf1>F1){nf1=F1;nf0=F1-(panF1-panF0)}
vf0=nf0;vf1=nf1;redraw()});
window.addEventListener('mouseup',()=>{panning=false;cv.style.cursor='grab'});
cv.addEventListener('dblclick',()=>{[vf0,vf1]=fullRange();redraw()});
loadGrid();refreshStatus();
setInterval(refreshStatus,8000);setInterval(loadGrid,300000);
setInterval(refreshNerd,3000);setInterval(refreshWf,1200);
</script></body></html>"""

RF_MHZ = {rf: 473 + (rf - 14) * 6 + 3 for rf in range(14, 37)}  # center MHz

def chan_map():
    """rf -> {lo_mhz, hi_mhz, label, subs:[virtual+callsign,...]} for the
    waterfall overlay. Educational note: subchannels TIME-share the 6 MHz."""
    channels, _ = load_epg()
    by_rf = {}
    for ch in channels:
        rf = ch["rf"]
        d = by_rf.setdefault(rf, {"lo": 470 + (rf - 14) * 6,
                                  "hi": 476 + (rf - 14) * 6,
                                  "subs": []})
        d["subs"].append(f"{ch['virtual']} {ch['callsign']}")
    for rf, d in by_rf.items():
        d["label"] = d["subs"][0].split(" ", 1)[1] if d["subs"] else f"RF{rf}"
    return by_rf


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
            st = {k: STATE[k] for k in ("rf", "prog", "virtual", "name", "tuning")}
            st["tuned"] = STATE["rf"] is not None and not STATE["tuning"]
            if st["tuned"]:
                st.update(live_math())
            self._send(json.dumps(st))
        elif self.path == "/api/nerd":
            self._send(json.dumps({"rf": STATE["rf"], "prog": STATE["prog"],
                                   "knobs": STATE.get("env") or {},
                                   "live": live_math()}))
        elif self.path.startswith("/api/waterfall"):
            since = 0
            if "since=" in self.path:
                try: since = int(self.path.split("since=")[1])
                except ValueError: pass
            with WF_LOCK:
                new = WF["rows"][-(WF["row_id"] - since):] \
                      if WF["row_id"] > since else []
                resp = {"status": WF["status"], "row_id": WF["row_id"],
                        "rows": new[-60:], "freqs": WF["freqs"] if since == 0 else None,
                        "chans": chan_map() if since == 0 else None}
            rf = STATE["rf"]
            if rf in RF_MHZ:
                resp["tuned_mhz"] = RF_MHZ[rf]
            self._send(json.dumps(resp))
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
        elif self.path == "/api/stop":
            threading.Thread(target=stop_tv, daemon=True).start()
            self._send('"stopped"')
        elif self.path == "/api/record":
            out = record(req["virt"], req["title"])
            self._send(out or "scheduled", "text/plain; charset=utf-8")
        else:
            self.send_error(404)


if __name__ == "__main__":
    threading.Thread(target=sweeper, daemon=True).start()
    print(f"TV Tuna panel: http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
