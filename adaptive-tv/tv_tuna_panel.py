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
from stvt_epg import load_epg, SCAN_PATH
from tv_lab import ts_metrics
import time_knob as tkn   # learned hour-curves (2026-07-10, cube fossil's heir)
import deep_tune as dtn   # DEEP TUNE channel doctor (2026-07-10 late)

PY = r"C:\Users\user\radioconda\python.exe"
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
CHAIN_LOG = HERE / "lab" / "panel_chain.log"
PORT = 8642

# PERSISTENT-RADIO RETUNE (2026-07-10): with STVT_PERSIST_RETUNE=1 in the
# panel's environment, cross-tower channel changes REUSE the running chain
# — the panel writes retune.cmd, tv_live.py's watcher re-points the soapy
# source in place (no 3.2 s kill, no 3.6 s driver reopen) and rotates
# live.ts. Fallback-first: no ack in 3 s, no field sync in 8 s, or a
# below-cliff quick read -> the classic kill+respawn path, byte-for-byte
# unchanged. Default OFF.
PERSIST = os.environ.get("STVT_PERSIST_RETUNE") == "1"
RETUNE_CMD = LIVE.parent / "retune.cmd"
RETUNE_ACK = LIVE.parent / "retune.ack"

# per-RF (rfgain_sel, IFGR) — reseed after ANY antenna/path change
# (AGC servos the IF gain live; rfgain_sel is the regime that matters).
# UHF values = Philips (2026-07-03); RF7 = first-ever VHF cal
# (2026-07-05, rabbit ears @ flatness-aimed position, MER 15.2 at cliff).
GAINS = {36: (3, 40), 34: (2, 32), 15: (1, 32), 7: (5, 32), 9: (5, 32),
         21: (2, 32)}
DEFAULT_GAIN = (3, 40)

# E2 v2 (2026-07-07): THOMPSON-SAMPLING antenna selection over the
# belief map (belief_map.py) — each (channel, antenna) is a posterior;
# we draw from each and tune the winner. Exploration emerges from
# uncertainty: a moved antenna (wide sd after its hardware epoch) gets
# retested automatically, a proven one gets used. The universal-tuner
# answer to "the map goes stale when hardware changes".
ANT_PORT = {"philips": "Antenna B", "rabbit": "Antenna A",
            "discone": "Antenna C"}
DEFAULT_ANT = "Antenna B"          # the Philips (2026-07-07 champion)

def antenna_for(rf):
    import random
    try:
        beliefs = json.loads((Path(__file__).parent /
                              "belief_map.json").read_text())
        cell = beliefs.get(str(rf), {})
        draws = {}
        for ant, b in cell.items():
            if ant in ANT_PORT:
                draws[ant] = random.gauss(b["mean"], max(0.3, b["sd"]))
        if draws:
            pick = max(draws, key=draws.get)
            return ANT_PORT[pick]
    except (OSError, ValueError, KeyError):
        pass
    # fallback: legacy hour-map
    try:
        ch = json.loads((Path(__file__).parent /
                         "cube_map.json").read_text())["channels"].get(str(rf))
        if ch:
            hr = str(time.localtime().tm_hour)
            ant = ch.get("owner_by_hour", {}).get(hr, ch["antenna"])
            return ANT_PORT.get(ant, DEFAULT_ANT)
    except (OSError, ValueError, KeyError):
        pass
    return DEFAULT_ANT
RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
RE_FPLL = re.compile(r"mean\|x\|=([\d.]+).*?max\|x\|=([\d.]+)\s+in_rms=([\d.]+)")
RE_RS5 = re.compile(r"last5s: pkts=(\d+) era_dec=\d+ era_ok=\d+ bad=(\d+)")
RE_CIR = re.compile(r"\[cir t=[\d.]+\] (.+)")

STATE = {"rf": None, "prog": None, "virtual": None, "name": None,
         "tuning": False, "env": {}, "stage": "", "stage_pct": 0}
LOCK = threading.Lock()
GEN = [0]

# antenna-picker persistence (2026-07-09 bug fix): the picker reset to
# auto on every panel restart, so an overnight restart silently pointed
# the next scan at the wrong antenna (a rabbit scan ran on the discone).
# Remember the last pick across restarts.
ANT_FILE = HERE / "lab" / "last_antenna.txt"


def set_antenna(ant):
    STATE["ant_override"] = ant if ant and ant != "auto" else None
    try:
        ANT_FILE.write_text(ant or "auto", encoding="utf-8")
    except OSError:
        pass


try:
    _saved = ANT_FILE.read_text(encoding="utf-8").strip()
    STATE["ant_override"] = _saved if _saved and _saved != "auto" else None
except OSError:
    STATE["ant_override"] = None

# ── channel scan (SCAN button) ─────────────────────────────────────
SCAN = {"running": False, "line": "", "pct": 0, "t0": None}

def run_scan():
    if SCAN["running"]:
        return
    SCAN.update({"running": True, "line": "stopping TV, starting scan…",
                 "pct": 2, "t0": time.time()})
    def locks_in(path):
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
            return sum(1 for c in d.get("channels", []) if c.get("lock"))
        except (OSError, json.JSONDecodeError):
            return 0
    prev = SCAN_PATH.with_name("scan_prev.json")
    try:
        if SCAN_PATH.exists():
            prev.write_text(SCAN_PATH.read_text(encoding="utf-8"),
                            encoding="utf-8")
        stop_tv()
        time.sleep(5)               # give a balance sweep time to release
        env = base_env(36)
        env["STVT_DABNOTCH"] = "0"   # scans must hear VHF-hi (RF7-13)
        env["STVT_IQ_RING"] = "0"    # no 1.1 GB E7 ring per lock-test chain
        env["STVT_PERSIST_RETUNE"] = "0"  # scan chains: no retune watchers
                                     # (many short-lived chains, one cmd file)
        # say WHICH antenna is being scanned — a sticky picker silently
        # produced a 12-minute discone scan the user thought was Philips
        _scan_ant = env.get("STVT_ANTENNA", "?")
        SCAN["line"] = f"scanning on {_scan_ant}…"
        p = subprocess.Popen([PY, "-u", str(TOOLS / "tv_tuner.py"), "--scan"],
                             env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace")
        try:
            p.stdin.write("1\n"); p.stdin.flush()
        except OSError:
            pass
        total, done = 0, 0
        for raw in p.stdout:
            line = raw.strip()
            if not line:
                continue
            SCAN["line"] = f"[{_scan_ant}] " + line[:145]
            if "phase 1" in line:
                SCAN["pct"] = 8
            m = re.search(r"full lock test on (\d+)", line)
            if m:
                total = int(m.group(1)); SCAN["pct"] = 30
            if total and line.startswith("RF "):
                done += 1
                SCAN["pct"] = 30 + int(62 * min(done, total) / total)
            if "saved to" in line:
                SCAN["pct"] = 97
        p.wait(timeout=120)
        # A dud scan (antenna mid-aim, radio wedged) must never clobber a
        # good channel map: if the fresh scan locked nothing and the prior
        # one had locks, restore the prior and stash the dud for study.
        dur = int(time.time() - SCAN["t0"]) if SCAN["t0"] else 0
        if prev.exists() and locks_in(SCAN_PATH) == 0 and locks_in(prev) > 0:
            SCAN_PATH.with_name("scan_dud.json").write_text(
                SCAN_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            SCAN_PATH.write_text(prev.read_text(encoding="utf-8"),
                                 encoding="utf-8")
            SCAN.update({"pct": 100, "t0": None,
                         "line": f"scan ({dur}s) found NO locks — kept the "
                                 "previous good channel map (dud saved aside)"})
        else:
            SCAN.update({"pct": 100, "t0": None,
                         "line": f"scan complete in {dur}s — guide refreshed"})
    except Exception as e:
        SCAN["line"] = f"scan failed: {e}"
    finally:
        SCAN["running"] = False

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
        if SCAN["running"] or BAL["on"] or FLAT["on"]:
            with WF_LOCK:
                WF["status"] = ("channel scan in progress — sweep paused"
                                if SCAN["running"] else
                                ("📏 flatness aiming in progress — sweep paused"
                                 if FLAT["on"] else
                                 "🌐 all-towers aiming in progress — sweep paused"))
            time.sleep(5); continue
        if STATE["rf"] is not None or STATE["tuning"] or METER["rf"] is not None \
                or chain_running():
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
                       or METER["rf"] is not None or BAL["on"] or FLAT["on"]
                       or SCAN["running"] or external_wants_sdr()):
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

# ── signal-finder meter (chain only, no player) ────────────────────
METER = {"rf": None}

def meter_start(rf):
    BAL["on"] = False
    FLAT["on"] = False
    with LOCK:
        GEN[0] += 1
        my_gen = GEN[0]
        METER["rf"] = None
        kill_tv(); time.sleep(2)
        if GEN[0] != my_gen:
            return
        env = base_env(rf)
        logf = open(CHAIN_LOG, "w")
        subprocess.Popen([PY, "-u", str(TOOLS / "tv_live.py"), "--rf", str(rf)],
                         env=env, stdout=logf, stderr=subprocess.STDOUT)
        STATE.update({"env": {k.replace("STVT_", ""): v for k, v in env.items()
                              if k.startswith("STVT_")}})
        METER["rf"] = rf

def meter_stop():
    with LOCK:
        GEN[0] += 1
        METER["rf"] = None
        kill_tv()

# ── all-towers balance meter: rapid pilot-power hopper ─────────────
# Aims for the FAIR spot: the tone follows the WEAKEST tower, so pitch
# only rises when the position improves for everyone. Pilot SNR proxy
# (no demod) -> several full sweeps per second across all towers.
BAL = {"on": False, "scores": {}}

def pilot_hz(rf):
    lo = (174 + (rf - 7) * 6) if rf < 14 else (470 + (rf - 14) * 6)
    return (lo + 0.30944) * 1e6

def balance_towers():
    try:
        d = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
        cars = [(c["rf"], c.get("pilot_snr_db") or 0)
                for c in d.get("channels", [])
                if not c.get("not_detected")
                and (c.get("lock") or (c.get("pilot_snr_db") or 0) >= 35)]
        cars.sort(key=lambda x: -x[1])
        return [c[0] for c in cars[:6]] or [36, 34, 15, 35]
    except (OSError, json.JSONDecodeError):
        return [36, 34, 15, 35]

def balance_loop():
    import numpy as np
    try:
        import SoapySDR
        SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
        from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
    except Exception:
        BAL["on"] = False
        return
    towers = balance_towers()
    FFT = 2048
    win = np.hanning(FFT).astype(np.float32)
    sdr = None
    try:
        for attempt in range(6):        # sweeper may take ~2s to yield
            try:
                sdr = SoapySDR.Device("driver=sdrplay")
                break
            except Exception:
                if not BAL["on"] or attempt == 5:
                    raise
                time.sleep(2)
        sdr.setSampleRate(SOAPY_SDR_RX, 0, 2_000_000)
        try: sdr.setGainMode(SOAPY_SDR_RX, 0, False)
        except Exception: pass
        sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 40.0)
        try: sdr.writeSetting("rfgain_sel", "3")
        except Exception: pass
        sdr.setAntenna(SOAPY_SDR_RX, 0, "Antenna A")
        st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        sdr.activateStream(st)
        buf = np.empty(FFT, dtype=np.complex64)
        while BAL["on"]:
            if (STATE["rf"] is not None or STATE["tuning"]
                    or SCAN["running"] or METER["rf"] is not None):
                break
            for rf in towers:
                sdr.setFrequency(SOAPY_SDR_RX, 0, pilot_hz(rf))
                time.sleep(0.02)
                acc = np.zeros(FFT)
                n = 0
                t0 = time.time()
                while time.time() - t0 < 0.12:
                    r = sdr.readStream(st, [buf], FFT, timeoutUs=150000)
                    if r.ret == FFT:
                        acc += np.abs(np.fft.fftshift(
                            np.fft.fft(buf * win)))**2
                        n += 1
                if not n:
                    continue
                psd = acc / n
                centre = float(psd[FFT // 2 - 6: FFT // 2 + 6].max())
                floor = float(np.median(psd))
                BAL["scores"][rf] = round(
                    10 * math.log10(centre / (floor + 1e-12)), 1)
        sdr.deactivateStream(st)
        sdr.closeStream(st)
    except Exception:
        pass
    finally:
        try: del sdr
        except Exception: pass
        BAL["on"] = False

def balance_start():
    if BAL["on"]:
        return
    FLAT["on"] = False
    with LOCK:
        GEN[0] += 1
        METER["rf"] = None
        kill_tv()
    BAL["scores"].clear()
    BAL["on"] = True
    time.sleep(1)
    threading.Thread(target=balance_loop, daemon=True).start()

# ── flatness meter: aim-by-ripple for channels that can't lock ─────
# RF7 taught us (IQ autopsy 2026-07-05): a channel can carry a perfect
# pilot while 27 dB canyons carve up the data band — no lock, no MER,
# no echo X-ray possible. But the RIPPLE ITSELF is measurable with no
# lock at all: FFT the band, measure smoothed max−min across the
# central 5 MHz. Flatter = decodable. The tone rises as canyons fill.
FLAT = {"on": False, "rf": None, "ripple": None}

def flat_loop(rf):
    import numpy as np
    try:
        import SoapySDR
        SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
        from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
    except Exception:
        FLAT["on"] = False
        return
    lo = (174 + (rf - 7) * 6) if rf < 14 else (470 + (rf - 14) * 6)
    center = (lo + 3.0) * 1e6
    FFT = 2048
    win = np.hanning(FFT).astype(np.float32)
    sdr = None
    try:
        for attempt in range(6):        # sweeper may take ~2s to yield
            try:
                sdr = SoapySDR.Device("driver=sdrplay")
                break
            except Exception:
                if not FLAT["on"] or attempt == 5:
                    raise
                time.sleep(2)
        sdr.setSampleRate(SOAPY_SDR_RX, 0, 8_000_000)
        sdr.setFrequency(SOAPY_SDR_RX, 0, center)
        sdr.setAntenna(SOAPY_SDR_RX, 0, "Antenna A")
        try: sdr.setGainMode(SOAPY_SDR_RX, 0, False)
        except Exception: pass
        sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 40.0)
        try:
            sdr.writeSetting("rfgain_sel", "3")
            if rf < 14:
                sdr.writeSetting("dabnotch_ctrl", "false")
        except Exception: pass
        st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        sdr.activateStream(st)
        buf = np.empty(FFT, dtype=np.complex64)
        fax = np.fft.fftshift(np.fft.fftfreq(FFT, 1 / 8e6))
        inband = np.abs(fax) < 2.5e6
        smooth = None
        while FLAT["on"]:
            if (STATE["rf"] is not None or STATE["tuning"]
                    or SCAN["running"] or METER["rf"] is not None
                    or BAL["on"]):
                break
            # Instantaneous ripple, autopsy-calibrated: canyons BREATHE, so
            # time-averaging the spectrum first fills them in and reads ~2×
            # optimistic (measured live 2026-07-05: 14 dB averaged vs 27 dB
            # instantaneous on the same channel). Instead: short 8-FFT
            # snapshots (~2 ms), ripple per snapshot, median across the
            # window — the number the decoder actually experiences.
            snap_rips = []
            t0 = time.time()
            while time.time() - t0 < 0.25:
                acc = np.zeros(FFT)
                n = 0
                for _ in range(8):
                    r = sdr.readStream(st, [buf], FFT, timeoutUs=200000)
                    if r.ret == FFT:
                        acc += np.abs(np.fft.fftshift(
                            np.fft.fft(buf * win)))**2
                        n += 1
                if not n:
                    continue
                p = 10 * np.log10(acc[inband] / n + 1e-20)
                k = np.convolve(p, np.ones(5) / 5, mode="valid")
                snap_rips.append(float(k.max() - k.min()))
            if not snap_rips:
                continue
            snap_rips.sort()
            ripple = snap_rips[len(snap_rips) // 2]
            smooth = ripple if smooth is None else 0.7 * smooth + 0.3 * ripple
            FLAT["ripple"] = round(smooth, 1)
        sdr.deactivateStream(st)
        sdr.closeStream(st)
    except Exception:
        pass
    finally:
        try: del sdr
        except Exception: pass
        FLAT["on"] = False
        FLAT["ripple"] = None

def flat_start(rf):
    BAL["on"] = False
    with LOCK:
        GEN[0] += 1
        METER["rf"] = None
        kill_tv()
    FLAT["rf"] = rf
    FLAT["ripple"] = None
    FLAT["on"] = True
    time.sleep(1)
    threading.Thread(target=flat_loop, args=(rf,), daemon=True).start()

# ── tuning / recording actions ─────────────────────────────────────
def base_env(rf):
    rfsel, ifgr = GAINS.get(rf, DEFAULT_GAIN)
    # DEEP TUNE recipe consult (2026-07-10 late): a measured recipe
    # outranks the static GAINS row; stale recipes still apply but the
    # UI says how old they are. The user's antenna pick always wins.
    recipe = dtn.load_recipe(rf)
    note = ""
    if recipe:
        try:
            rfsel = int(recipe.get("rfgain_sel", rfsel))
            ifgr = int(recipe.get("ifgr", ifgr))
        except (TypeError, ValueError):
            pass
        age = dtn.recipe_age_days(recipe)
        note = (f"deep-tune recipe: {recipe.get('antenna', '?')} rfgain "
                f"{rfsel}/IFGR {ifgr}"
                + (f" — recipe from {age:.0f} days ago"
                   if age > dtn.STALE_DAYS else ""))
    env = os.environ.copy()
    env["PATH"] = (r"C:\Program Files\SDRplay\API\x64;C:\ffmpeg\bin;"
                   + env.get("PATH", ""))
    # user override first (the user picks the antenna; the code's job
    # is to decode whatever it's given) — recipe antenna next, then
    # belief-map auto as the fallback
    _ant = (STATE.get("ant_override") or (recipe or {}).get("antenna")
            or antenna_for(rf))
    if recipe:
        if (STATE.get("ant_override")
                and STATE["ant_override"] != recipe.get("antenna")):
            note += (f" (your pick {STATE['ant_override']} wins over "
                     f"recipe {recipe.get('antenna')})")
        print(f"[recipe] RF{rf} consulted channel_recipes.json -> "
              f"antenna={_ant} rfgain_sel={rfsel} ifgr={ifgr}", flush=True)
    STATE["recipe_note"] = note
    env.update({"STVT_ANTENNA": _ant, "STVT_IFGR": str(ifgr),
                "STVT_RFGAIN_SEL": str(rfsel),
                # MEMORY-TUNE (2026-07-10): tv_watch keys its PID/program-
                # layout cache on (rf, prog); rf rides the env like the
                # other knobs (the chain ignores it)
                "STVT_RF": str(rf),
                # E7 second-opinion memory: the chain keeps its last ~35 s
                # of IQ in RAM (~1.1 GB); the 🩺 button snapshots it —
                # no second SDR session, no TV pause (7/07 v2)
                "STVT_IQ_RING": "35",
                "STVT_IQ_RING_DIR": str(HERE / "lab" / "e7_ring"),
                # hardware AGC servo: IF gain follows the antenna in
                # real time (setpoint -20 dBFS, validated 2026-07-02),
                # so aiming/moving the antenna no longer invalidates
                # the gain calibration; IFGR above is just the seed.
                "STVT_SDR_AGC": "1", "STVT_AGC_SETPOINT": "-20",
                "STVT_EQ": "long",
                "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
                # DAB band III (174-240 MHz) IS the US VHF-hi TV band —
                # the notch was amputating RF7-13 by ~20 dB (solved
                # 2026-07-04: in_rms 16.5 -> 170 on RF7 with it off).
                "STVT_DABNOTCH": "0" if rf < 14 else "1",
                # E4 (2026-07-06): erasure RS at 0 erasures = stock
                # behavior + FEC telemetry (the formerly-dark region)
                "STVT_RS": "erasure", "STVT_RS_ERASURES": "0",
                # TURBO 2B trellis pinning on ALL tunes (2026-07-10):
                # failed RS codewords get a pinned Viterbi re-decode —
                # replay A/B: 54-70% of failed packets converted on
                # marginal specimens, 0 miscorrections, ~1-4% CPU.
                # Needs the SOVA reliability plane (erasures stay 0 in
                # base: reliabilities flow, erasure budget unchanged).
                "STVT_SOVA": "1", "STVT_TURBO": "1",
                # MOD-12 GUARD (2026-07-07): the slip cure — 456 saves in
                # one night carried channel 9 to 35,546 headers. Inert on
                # clean streams by construction.
                "STVT_EQ_MOD12_GUARD": "1",
                # DD tracking EVICTED from live (2026-07-10 evening
                # bisect): DD-alone = 54 source overflows / 1.02% loss
                # vs 0 / 0.057% without — the equalizer misses the
                # real-time deadline and the SDR drops samples (the
                # "splice" glitches). DD stays OFFLINE-ONLY (e7_vote /
                # replay, no deadline) where its -41.6% impulse win is
                # real. Do NOT re-enable live without an overflow-gated
                # A/B — replay CPU was already the tell (70s/45s).
                "STVT_SPS": "1.1",
                "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1",
                "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
                "STVT_EQ_TELEM": "1",
                "STVT_EQ_CIR": "1",    # echo X-ray telemetry (H2)
                # warm-start tap cache (lever #3): per-channel LKG taps
                # persisted to disk; tune-in seeds from last good state
                "STVT_EQ_TAP_CACHE": str(HERE / "lab" / "tapcache"),
                # time-knob v2: scans append quality samples here
                "STVT_QUALITY_HISTORY": str(HERE / "lab"
                                            / "quality_history.csv")})
    return env

def kill_watch():
    """Kill only the player side (tv_watch/mpv/ffmpeg) — the chain keeps
    decoding. This is what makes same-mux hops instant."""
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'tv_watch' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue }"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "mpv.exe"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe"], capture_output=True)

def kill_tv():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'tv_live|tv_watch' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue }"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "mpv.exe"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe"], capture_output=True)

def set_stage(pct, msg):
    STATE.update({"stage": msg, "stage_pct": pct})


# ── E7 SECOND OPINION (2026-07-07 night): capture ~30 s of raw airwaves,
# decode them THREE times with different equalizer trajectories, and
# splice every clean GOP any pass rescued over the damage (e7_vote.py
# heal-merge).
#
# v2 (same night): snapshot the CHAIN'S OWN IQ ring instead of opening a
# second SDR session. The separate-capture path was chronically lossy
# (~4% sample drops after rapid open/close churn — decode-verified at
# 15.8% bad on a healthy channel) while the live chain decodes the same
# antenna clean. The ring holds the last ~35 s the viewer just watched:
# TV never pauses, no wedge, and the second opinion is on the exact
# glitches that prompted the click.
E7 = {"status": "idle"}
_MPV = r"C:\Program Files\MPV Player\mpv.exe"
E7_RING_DIR = HERE / "lab" / "e7_ring"


def e7_run(secs=30):
    if E7["status"] not in ("idle",) and not E7["status"].startswith(
            ("done", "error")):
        return
    rf, prog = STATE.get("rf"), STATE.get("prog")
    if rf is None or prog is None:
        E7["status"] = "error: tune a channel first"
        return
    healed = HERE / "lab" / "e7_healed.ts"

    def run():
        try:
            E7["status"] = "snapshotting the last ~30 s from the IQ ring"
            E7_RING_DIR.mkdir(parents=True, exist_ok=True)
            before = set(E7_RING_DIR.glob("*.cs16"))
            (E7_RING_DIR / "TRIGGER").write_text(
                "E7 second opinion", encoding="utf-8")
            cap = None
            for _ in range(45):          # ring dump lands within seconds
                time.sleep(1)
                new = [p for p in E7_RING_DIR.glob("*.cs16")
                       if p not in before and p.stat().st_size > 1_000_000]
                if new:
                    cand = max(new, key=lambda p: p.stat().st_mtime)
                    sz = cand.stat().st_size
                    time.sleep(2)
                    if cand.stat().st_size == sz:   # write finished
                        cap = cand
                        break
            if cap is None:
                E7["status"] = ("error: no ring snapshot — retune once "
                                "(chain needs the IQ ring enabled)")
                return
            E7["status"] = "decoding 3 opinions of what you just watched"
            env = dict(os.environ)
            env["STVT_DABNOTCH"] = "0" if rf < 14 else "1"
            vote_cmd = [PY, "-u", str(HERE / "e7_vote.py"), str(cap),
                        "--passes", "3", "--prog", str(prog),
                        "--out", str(healed)]
            # warm-start from the live chain's tap cache (RF9-class
            # channels burn a third of a cold 35 s replay converging)
            ant = re.sub(r"\W+", "",
                         STATE.get("ant_override") or antenna_for(rf))
            tapc = HERE / "lab" / "tapcache" / f"taps_{ant}_rf{rf}.bin"
            if tapc.exists():
                vote_cmd += ["--tapc", str(tapc)]
            r = subprocess.run(vote_cmd, capture_output=True, text=True,
                               env=env, timeout=1200)
            tail = [ln for ln in (r.stdout or "").splitlines()
                    if "frames:" in ln or "union:" in ln]
            E7["status"] = ("done: " + " · ".join(tail)[-160:]) if tail \
                else f"error: vote failed rc={r.returncode}"
            hm = re.search(r"(\d+)/(\d+) damaged GOPs healed",
                           r.stdout or "")
            n_healed = int(hm.group(1)) if hm else 0
            # NEVER auto-open a window (three user-confusions on 7/07:
            # a surprise twin next to live TV reads as a broken tuner).
            # The replay waits for the ▶ button in the science card.
            if n_healed > 0 and healed.exists() \
                    and healed.stat().st_size > 500_000:
                E7["replay"] = n_healed
                E7["status"] += (f" · {n_healed} GOP(s) healed — replay "
                                 "ready, press ▶")
            elif E7["status"].startswith("done"):
                E7["replay"] = 0
                E7["status"] += " · no rescues needed"
        except Exception as e:
            E7["status"] = f"error: {e}"

    threading.Thread(target=run, daemon=True).start()


# ── TUNA SCIENCE (2026-07-07): the invented instruments, served live ──
_SCI_CACHE = {"t": 0.0, "data": {}}
# time-knob wiring (2026-07-10): rows cache + 1-per-min telemetry recorder
_TK_CACHE = {"t": 0.0, "rows": []}
_TK_LAST_REC = [0.0]

def _tk_rows():
    # 60 s refresh: the NERD card shows the sample count ticking up
    # live as the recorder appends — "watch it learn" (user ask 7/10)
    if time.time() - _TK_CACHE["t"] > 60:
        try:
            _TK_CACHE["rows"] = tkn.load()
        except OSError:
            _TK_CACHE["rows"] = []
        _TK_CACHE["t"] = time.time()
    return _TK_CACHE["rows"]


def science_data():
    """Time knob, survival curve, guard/sheriff/dawn/oracle — cheap reads,
    5 s cache. Education layer for the NERD tab."""
    now = time.time()
    if now - _SCI_CACHE["t"] < 5:
        return _SCI_CACHE["data"]
    out = {}
    # survival curve position (measured 7/06-07: 0% @14, 50% @15.2,
    # ~85% @16.4, 99.7% @17 — logistic fit)
    cm = chain_math()
    mer = cm.get("mer_db")
    if mer:
        surv = 100.0 / (1.0 + math.exp(-(mer - 15.25) / 0.55))
        out["survival"] = {"mer": mer, "pct": round(surv),
                           "watchable": mer >= 16.0}
    # time knob v2 (2026-07-10): LEARNED hour-curves from the quality
    # history — replaces the 7/06 cube_map.json fossil (one night,
    # hardcoded antenna names). Confidence-tiered: silence over guessing.
    if STATE.get("rf") is not None:
        try:
            rows = _tk_rows()
            rf_now = STATE["rf"]
            bh = tkn.best_hours(rf_now, rows)
            c = tkn.curve(rf_now, rows)
            known = {h: b["mer"] for h, b in c.items()
                     if b.get("mer") is not None
                     and b.get("conf") in ("solid", "thin")}
            spark = ""
            if known:
                lo, hi = min(known.values()), max(known.values())
                span = max(hi - lo, 0.1)
                blocks = "▁▂▃▄▅▆▇█"
                spark = "".join(
                    blocks[round((known[h] - lo) / span * 7)]
                    if h in known else "·" for h in range(24))
            hr = time.localtime().tm_hour
            nowbin = c.get(hr, {})
            out["timeknob"] = {
                "now_owner": STATE.get("ant_override") or "?",
                "best_hour": bh[0][0] if bh else None,
                "worst_hour": bh[1][0] if bh else None,
                "swing_db": bh[2] if bh else None,
                "hint": tkn.hint(rf_now, rows),
                "spark": spark, "hour_now": hr,
                "now_mer": nowbin.get("mer"),
                "now_conf": nowbin.get("conf"),
                # the live "watch it learn" number: total samples this
                # channel has ever contributed — ticks up each minute
                "n_samples": sum(1 for r in rows if r["rf"] == rf_now)}
        except (OSError, ValueError, KeyError):
            pass
    # turbo + realtime-health telemetry for the science cards
    try:
        text = CHAIN_LOG.read_text(errors="ignore")[-60000:]
        tb = re.findall(r"\[rs_turbo[^\n]*att=(\d+) retry=\d+ resc=(\d+)"
                        r"[^\n]*skip=(\d+)[^\n]*fail_ema=([\d.]+)%", text)
        if tb:
            att, resc, skip, fema = tb[-1]
            out["turbo"] = {"att": int(att), "resc": int(resc),
                            "skip": int(skip), "fail_ema": float(fema)}
        out["oso"] = len(re.findall(r"OsO|^O", text, re.M))
    except OSError:
        pass
    # guard saves this chain-session
    try:
        txt = CHAIN_LOG.read_text(errors="ignore")
        out["guard_fires"] = txt.count("MOD12 GUARD")
        out["slips_seen"] = txt.count("MOD12 SLIP")
    except OSError:
        pass
    if E7["status"] != "idle":
        out["e7"] = E7["status"]
    # sheriff / dawn / oracle: latest events from the campaign log
    try:
        lines = (HERE / "cube_log.jsonl").read_text(
            encoding="utf-8").splitlines()
        for line in reversed(lines[-400:]):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            ev = o.get("event", "")
            if ev == "SHERIFF" and "sheriff" not in out:
                out["sheriff"] = {"action": o.get("action"), "t": o.get("t")}
            elif ev in ("dawn-score2", "dawn-score") and "dawn" not in out:
                out["dawn"] = {"score": o.get("score"),
                               "verdict": o.get("verdict"), "t": o.get("t")}
            elif ev == "beacon-oracle" and "oracle" not in out:
                out["oracle"] = {"paths": o.get("paths_db"), "t": o.get("t")}
            if all(k in out for k in ("sheriff", "dawn", "oracle")):
                break
    except OSError:
        pass
    _SCI_CACHE.update({"t": now, "data": out})
    return out

def tune(rf, prog, virtual, name, force_respawn=False):
    BAL["on"] = False
    FLAT["on"] = False
    # INSTANT SAME-MUX HOP (2026-07-05): stations on one tower share the
    # same radio signal — if the chain is already decoding this RF, only
    # the player needs to change. ~8 s instead of ~60.
    try:
        chain_fresh = (time.time() - CHAIN_LOG.stat().st_mtime) < 6
    except OSError:
        chain_fresh = False
    if (not force_respawn and rf == STATE["rf"] and not STATE["tuning"]
            and STATE["rf"] is not None and chain_fresh):
        with LOCK:
            GEN[0] += 1
            my_gen = GEN[0]
            METER["rf"] = None
            STATE.update({"prog": prog, "virtual": virtual, "name": name,
                          "tuning": True})
            set_stage(40, f"same-tower hop → {virtual} (radio already "
                          "tuned, swapping program only)")
        def hop():
            kill_watch()
            time.sleep(1)
            if GEN[0] != my_gen:
                return
            env = base_env(rf)
            cm = chain_math()
            # 15.8 (2026-07-06): 16.5 muzzled audio on signals that play
            # fine (RF36 @ 16.2 = clear picture). Video-only is for the
            # true cliff edge, not a 1 dB safety blanket.
            marginal = (cm.get("mer_db") or 99) < 15.8
            set_stage(70, f"extracting program {prog}"
                          + (" (forced-video mode)" if marginal else ""))
            watch_args = [PY, "-u", str(HERE / "tv_watch.py"), str(prog)]
            if marginal:
                watch_args.append("marginal")
            watch_log = open(HERE / "lab" / "panel_watch.log", "w")
            subprocess.Popen(watch_args, env=env,
                             stdout=watch_log, stderr=subprocess.STDOUT)
            t0 = time.time()
            while time.time() - t0 < 120:
                if GEN[0] != my_gen:
                    return
                r = subprocess.run(["tasklist", "/FI",
                                    "IMAGENAME eq mpv.exe"],
                                   capture_output=True, text=True)
                if "mpv.exe" in (r.stdout or ""):
                    break
                time.sleep(1)
            set_stage(100, "")
            STATE.update({"tuning": False})
        threading.Thread(target=hop, daemon=True).start()
        return
    # ── PERSISTENT-RADIO RETUNE (2026-07-10, STVT_PERSIST_RETUNE=1) ──
    # Cross-tower change with a live healthy chain: re-point the radio in
    # place instead of kill+respawn. Every gate falls back to the classic
    # path (force_respawn=True skips both this branch and the hop guard,
    # because after a half-retune the chain may sit on the NEW freq while
    # STATE still says the old one — only a full respawn is trustworthy).
    if (PERSIST and not force_respawn and STATE["rf"] is not None
            and rf != STATE["rf"] and not STATE["tuning"] and chain_fresh):
        with LOCK:
            GEN[0] += 1
            my_gen = GEN[0]
            METER["rf"] = None
            STATE.update({"tuning": True})
            set_stage(8, f"persistent retune → {virtual} (chain stays up, "
                         "radio re-points in place)")

        def persist():
            def fallback(reason):
                try:
                    RETUNE_CMD.unlink()   # never leave a stale command for
                except OSError:           # the NEXT chain to replay
                    pass
                if GEN[0] != my_gen:
                    return
                set_stage(10, f"persistent retune fell back ({reason}) — "
                              "classic respawn")
                tune(rf, prog, virtual, name, force_respawn=True)

            kill_watch()
            if GEN[0] != my_gen:
                return
            env = base_env(rf)
            try:
                RETUNE_ACK.unlink()
            except OSError:
                pass
            try:
                log_ofs = CHAIN_LOG.stat().st_size
            except OSError:
                log_ofs = 0
            cmd = {"rf": rf, "antenna": env["STVT_ANTENNA"],
                   "rfgain_sel": int(env["STVT_RFGAIN_SEL"]),
                   "ifgr": int(env["STVT_IFGR"]),
                   "dabnotch": 0 if rf < 14 else 1}
            tmp = RETUNE_CMD.parent / (RETUNE_CMD.name + ".tmp")
            try:
                tmp.write_text(json.dumps(cmd), encoding="utf-8")
                os.replace(tmp, RETUNE_CMD)      # atomic: no partial reads
            except OSError as e:
                fallback(f"cmd write failed: {e}")
                return
            # gate 1: ack within 3 s (a chain without the watcher — old
            # spawn, dead process — never answers)
            t0 = time.time()
            ack = None
            while time.time() - t0 < 3.0:
                if GEN[0] != my_gen:
                    return
                try:
                    ack = json.loads(RETUNE_ACK.read_text(encoding="utf-8"))
                    break
                except (OSError, ValueError):
                    time.sleep(0.1)
            if not ack:
                fallback("no ack from chain")
                return
            if not ack.get("ok"):
                fallback(f"chain says: {ack.get('error')}")
                return
            set_stage(30, f"radio re-pointed to RF{rf} in "
                          f"{time.time()-t0:.1f}s — FPLL hunting the pilot")
            # player rides in parallel: tv_watch's own gate waits for the
            # rotated live.ts to grow fresh bytes (rotation law honored —
            # the sink reopened the file at offset 0 before the ack)
            def new_text():
                try:
                    with open(CHAIN_LOG, "r", errors="ignore") as f:
                        f.seek(log_ofs)
                        return f.read()
                except OSError:
                    return ""
            watch_log = open(HERE / "lab" / "panel_watch.log", "w")
            subprocess.Popen([PY, "-u", str(HERE / "tv_watch.py"), str(prog)],
                             env=env, stdout=watch_log,
                             stderr=subprocess.STDOUT)
            # gate 2: field sync (fs_err_rms telemetry) within 8 s
            t1 = time.time()
            synced = False
            while time.time() - t1 < 8.0:
                if GEN[0] != my_gen:
                    return
                if RE_FS.search(new_text()):
                    synced = True
                    break
                time.sleep(0.4)
            if not synced:
                kill_watch()
                fallback("no field sync within 8 s")
                return
            with LOCK:
                if GEN[0] != my_gen:
                    return
                STATE.update({"rf": rf, "prog": prog, "virtual": virtual,
                              "name": name,
                              "env": {k.replace("STVT_", ""): v
                                      for k, v in env.items()
                                      if k.startswith("STVT_")}})
            set_stage(55, f"field sync on RF{rf} — equalizer retraining, "
                          f"extracting program {prog}")
            # gate 3: quick margin read (~3 s of fresh telemetry). A
            # true-cliff result goes to the classic path, which owns the
            # full recovery recipe (erasure FEC + DFE + reseed respawn).
            time.sleep(3)
            errs = [float(m.group(1))
                    for m in RE_FS.finditer(new_text())][-40:]
            mers = [20 * math.log10(5.0 / e) for e in errs if e > 0]
            mer_now = sorted(mers)[len(mers) // 2] if mers else 0.0
            if mers and mer_now < 15.0:
                kill_watch()
                fallback(f"MER {mer_now:.1f} below cliff — full recipe")
                return
            t2 = time.time()
            while time.time() - t2 < 120:
                if GEN[0] != my_gen:
                    return
                r = subprocess.run(["tasklist", "/FI",
                                    "IMAGENAME eq mpv.exe"],
                                   capture_output=True, text=True)
                if "mpv.exe" in (r.stdout or ""):
                    break
                time.sleep(1)
            set_stage(100, "")
            STATE.update({"tuning": False})

        threading.Thread(target=persist, daemon=True).start()
        return
    with LOCK:
        GEN[0] += 1
        my_gen = GEN[0]
        METER["rf"] = None
        STATE.update({"tuning": True})
        set_stage(4, "clearing the tuner — stopping old chain and player")
        kill_tv(); time.sleep(2)
        if GEN[0] != my_gen:
            return
        env = base_env(rf)
        logf = open(CHAIN_LOG, "w")
        subprocess.Popen([PY, "-u", str(TOOLS / "tv_live.py"), "--rf", str(rf)],
                         env=env, stdout=logf, stderr=subprocess.STDOUT)
        def logtxt():
            try: return CHAIN_LOG.read_text(errors="ignore")[-30000:]
            except OSError: return ""
        def player():
            # Milestone-gated startup: each stage is proven from telemetry,
            # not guessed from a timer, so the bar reflects reality and a
            # dead radio is called out instead of spinning forever.
            t0 = time.time()
            set_stage(12, "opening the radio — SoapySDR → SDRplay handshake")
            while time.time() - t0 < 45:
                if GEN[0] != my_gen: return
                t = logtxt()
                if ("sdrplay_api_Fail" in t or "Init() failed" in t
                        or "no available RSP" in t
                        or "Traceback (most recent call last)" in t):
                    set_stage(0, "RADIO FAILED — the decode chain died on "
                                 "startup. Replug the SDR or restart the "
                                 "SDRplay API service, then click the "
                                 "station again.")
                    STATE.update({"tuning": False, "rf": None, "prog": None,
                                  "virtual": None, "name": None, "env": {}})
                    return
                if "Using format" in t:
                    break
                time.sleep(1.2)
            set_stage(30, "radio streaming — FPLL hunting the ATSC pilot")
            while time.time() - t0 < 70:
                if GEN[0] != my_gen: return
                if RE_FPLL.search(logtxt()):
                    break
                time.sleep(1.2)
            # Cliff-edge autodetect: sample MER for ~12 s after lock. A
            # marginal signal gets one automatic chain restart with the
            # recovery recipe (erasure FEC + equalizer tap guard — the
            # config-shootout champion for signals riding the cliff).
            #
            # MEMORY-TUNE (2026-07-10): when the learned hour-curves say
            # this RF is confidently healthy RIGHT NOW (solid/thin bin at
            # >= 16.8, or recent same-antenna rows averaging >= 16.8), a
            # ~3 s sanity read replaces the 12 s measurement. Any doubt —
            # no samples yet, or the quick read lands below 16.8 — falls
            # back to finishing the full 12 s window, so cliff decisions
            # on genuinely marginal channels are byte-for-byte unchanged.
            meas_s = 12
            try:
                _rows = _tk_rows()
                _bin = tkn.curve(rf, _rows).get(time.localtime().tm_hour, {})
                _ant = env.get("STVT_ANTENNA")
                _recent = [r["mer"] for r in _rows
                           if r["rf"] == rf and r["ant"] == _ant
                           and r["mer"] is not None][-12:]
                if ((_bin.get("conf") in ("solid", "thin")
                     and (_bin.get("mer") or 0) >= 16.8)
                        or (len(_recent) >= 5
                            and sum(_recent) / len(_recent) >= 16.8)):
                    meas_s = 3
            except Exception:
                pass
            set_stage(45, "phase locked — measuring decode margin"
                          + (" (memory: healthy history — quick check)"
                             if meas_s < 12 else ""))
            t_m = time.time()
            while time.time() - t_m < meas_s:
                if GEN[0] != my_gen: return
                time.sleep(1)
            errs = [float(mm.group(1))
                    for mm in RE_FS.finditer(logtxt())][-40:]
            mers = [20 * math.log10(5.0 / e) for e in errs if e > 0]
            mer_now = sum(mers) / len(mers) if mers else 0.0
            if meas_s < 12 and (not mers or mer_now < 16.8):
                # memory said healthy, the quick read disagrees — finish
                # the full measurement before any cliff-mode decision
                set_stage(45, "quick check inconclusive — measuring the "
                              "full decode margin")
                while time.time() - t_m < 12:
                    if GEN[0] != my_gen: return
                    time.sleep(2)
                errs = [float(mm.group(1))
                        for mm in RE_FS.finditer(logtxt())][-40:]
                mers = [20 * math.log10(5.0 / e) for e in errs if e > 0]
                mer_now = sum(mers) / len(mers) if mers else 0.0
            cliff_mode = bool(mers) and mer_now < 16.5
            if cliff_mode:
                set_stage(52, f"MER {mer_now:.1f} dB — engaging cliff-edge "
                              "recovery (erasure FEC + tap guard)")
                # 2026-07-07 FINAL FORM — the two-mode chain: healthy
                # signals ride lean (full stack = ~20% throughput tax,
                # 5-round gauntlet), cliff signals get the whole proven
                # arsenal: SOVA trellis-doubt erasures (174 rescues/75s),
                # DFE (+58 hdr/sample sub-cliff), reseed, quality reset.
                # Guard is default-on everywhere already.
                env.update({"STVT_RS": "erasure", "STVT_RS_ERASURES": "14",
                            "STVT_SOVA": "1",
                            "STVT_EQ_DFE": "1",
                            # anchor promoted 7/07: on RF7 canyon replay,
                            # DFE+anchor halves bad pkts (46%->21%) and
                            # adds +51% frames vs no-DFE; inert elsewhere
                            "STVT_EQ_DFE_ANCHOR": "1",
                            # DD EVICTED from cliff too (7/10 evening):
                            # live DD = 54 source overflows / +1% splice
                            # loss (misses the real-time deadline). Its
                            # replay wins (-31% RF9 breather) were
                            # measured with no deadline; live the tax
                            # exceeds the win. Offline E7/replay only.
                            "STVT_EQ_RESEED": "1",
                            "STVT_EQ_QUALITY_BAD_RMS": "8"})
                if GEN[0] != my_gen: return
                kill_tv(); time.sleep(2)
                if GEN[0] != my_gen: return
                logf2 = open(CHAIN_LOG, "w")
                subprocess.Popen([PY, "-u", str(TOOLS / "tv_live.py"),
                                  "--rf", str(rf)],
                                 env=env, stdout=logf2,
                                 stderr=subprocess.STDOUT)
                STATE.update({"env": {k.replace("STVT_", ""): v
                                      for k, v in env.items()
                                      if k.startswith("STVT_")}})
                t_r = time.time()
                while time.time() - t_r < 60:
                    if GEN[0] != my_gen: return
                    if RE_FPLL.search(logtxt()):
                        break
                    time.sleep(1.5)
            t0 = time.time()          # fresh budget for the stream gates
            # MEMORY-TUNE: on a healthy signal with a PID-cache hit,
            # tv_watch enforces its own 6 MB live.ts gate before
            # extracting — waiting here too is pure double-waiting.
            # Cliff/harvest paths keep the full gate (conservative).
            _pc_hit = False
            try:
                _pc = json.loads((HERE / "lab" / "pid_cache.json")
                                 .read_text(encoding="utf-8"))
                _pc_hit = f"{rf}:{prog}" in _pc
            except (OSError, ValueError):
                pass
            skip_gate = (_pc_hit and not cliff_mode
                         and not (mers and mer_now < 15.0))
            set_stage(60, "equalizer converging — buffering the "
                          "transport stream"
                          + (" (memory: layout cached — handing straight "
                             "to the extractor)" if skip_gate else ""))
            # Gate on GROWTH since this chain started, not absolute size —
            # the previous tune's leftover live.ts is big and freshly
            # written, which used to pass this gate instantly (false 75%).
            try:
                base_sz = LIVE.stat().st_size
            except OSError:
                base_sz = 0
            while not skip_gate and time.time() - t0 < 110:
                if GEN[0] != my_gen: return
                try:
                    stt = LIVE.stat()
                    if stt.st_size < base_sz:
                        base_sz = stt.st_size      # chain truncated/rotated
                    if (stt.st_size - base_sz > 6_000_000
                            and time.time() - stt.st_mtime < 5):
                        break
                except OSError:
                    pass
                time.sleep(1.5)
            if GEN[0] != my_gen: return
            # watchability gate (2026-07-07, survival-curve law): below
            # ~15.0 sustained, headers are countable but frames are not
            # assemblable — warn honestly instead of a silent cone.
            watch_note = ""
            if mers and mer_now < 15.0:
                watch_note = (" — BELOW WATCHABLE (curve says 16+): "
                              "expect stills/black; check the map for "
                              "this channel's best hour")
            set_stage(75, f"stream proven — extracting program {prog}, "
                          "launching player"
                          + (" (forced-video mode)" if cliff_mode else "")
                          + watch_note)
            # HARVEST MODE (2026-07-07): below ~17 the raw stream carries
            # visible tears (user's 16.8 channel 9: play-freeze-glitch).
            # The harvester feeds the player ONLY complete clean GOPs —
            # motion at a gentler rhythm instead of corruption.
            # (harvest-live scoped to true-cliff only 2026-07-07 late:
            # its splice stream confuses mpv's prober at probe time —
            # bench-proven on finished files, live variant needs demuxer
            # work; above 15 the proven tv_watch path plays with sound)
            if mers and mer_now < 15.0:
                watch_args = [PY, "-u", str(HERE / "harvest_player.py"),
                              str(TOOLS / "data" / "tv_live" / "live.ts"),
                              "--prog", str(prog), "--follow"]
                set_stage(75, f"stream proven — HARVEST MODE "
                              f"(MER {mer_now:.1f}: only true frames pass)")
            else:
                watch_args = [PY, "-u", str(HERE / "tv_watch.py"), str(prog)]
                if cliff_mode and mer_now < 15.8:
                    watch_args.append("marginal")
            watch_log = open(HERE / "lab" / "panel_watch.log", "w")
            subprocess.Popen(watch_args,
                             env=env, stdout=watch_log,
                             stderr=subprocess.STDOUT)
            player_up = False
            while time.time() - t0 < 150:
                if GEN[0] != my_gen: return
                r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq mpv.exe"],
                                   capture_output=True, text=True)
                if "mpv.exe" in (r.stdout or ""):
                    player_up = True
                    break
                time.sleep(1)
            if player_up:
                set_stage(100, "")
            else:
                set_stage(0, "PLAYER never appeared — the signal is likely "
                             "below decode. Aim with 🎯 SIGNAL FINDER, then "
                             "tune again.")
            STATE.update({"tuning": False})
        threading.Thread(target=player, daemon=True).start()
        knobs = {k.replace("STVT_", ""): v for k, v in env.items()
                 if k.startswith("STVT_")}
        STATE.update({"rf": rf, "prog": prog, "virtual": virtual,
                      "name": name, "env": knobs})

def stop_tv():
    BAL["on"] = False           # ...and a running all-towers balance sweep
    FLAT["on"] = False
    with LOCK:
        GEN[0] += 1
        METER["rf"] = None      # scan/stop must also release a running meter
        kill_tv()
        STATE.update({"rf": None, "prog": None, "virtual": None,
                      "name": None, "tuning": False, "env": {},
                      "stage": "", "stage_pct": 0, "recipe_note": ""})

# ── 🔬 DEEP TUNE: the channel doctor (2026-07-10 late) ─────────────
# Baseline -> antenna race -> gain grid -> disease verdict -> recipe.
# Engine lives in deep_tune.py; this wrapper wires the panel's env,
# stage line, cancel (any user tune bumps GEN and aborts us cleanly)
# and the quality history (source="deeptune").
DT = {"on": False, "status": "", "pct": 0, "verdict": "", "vt": 0.0}


def deeptune_start():
    if DT["on"]:
        return "deep tune already running"
    if STATE.get("rf") is None or STATE.get("tuning"):
        return "tune a channel first (and let it settle), then DEEP TUNE"
    rf, prog = STATE["rf"], STATE["prog"]
    virt, name = STATE.get("virtual"), STATE.get("name") or ""
    e = STATE.get("env") or {}
    cur_ant = (e.get("ANTENNA") or STATE.get("ant_override")
               or antenna_for(rf))
    g = GAINS.get(rf, DEFAULT_GAIN)
    try:
        cur_gains = (int(e.get("RFGAIN_SEL", g[0])), int(e.get("IFGR", g[1])))
    except (TypeError, ValueError):
        cur_gains = g
    ants = sorted(set(ANT_PORT.values()))     # the picker's antenna list

    def run():
        with LOCK:
            GEN[0] += 1
            my_gen = GEN[0]
            METER["rf"] = None
        DT.update({"on": True, "status": "starting", "pct": 0, "verdict": ""})
        STATE.update({"tuning": True})        # sweeper/trainer stand down
        kill_tv()                             # doctor takes the whole tuner
        time.sleep(2)

        def status(pct, msg):
            DT.update({"status": msg, "pct": pct})
            set_stage(pct, "DEEP TUNE (click any station to abort) — " + msg)

        def env_b(ant, rsel, ifgr):
            env = base_env(rf)
            env.update({"STVT_ANTENNA": ant, "STVT_RFGAIN_SEL": str(rsel),
                        "STVT_IFGR": str(ifgr), "STVT_IQ_RING": "0",
                        "STVT_PERSIST_RETUNE": "0"})
            return env

        def rec_row(ant, met):
            try:
                tkn.record({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "rf": rf, "ant": ant, "mer": met.get("mer_med"),
                            "loss_pct": met.get("loss_pct"),
                            "source": "deeptune"})
            except (OSError, ValueError):
                pass

        def hours():
            try:
                rows = tkn.load()
                h = tkn.hint(rf, rows)
                if h:
                    return h + " (Knob of Time)"
                bh = tkn.best_hours(rf, rows)
                if bh and bh[0]:
                    return (f"usually best ~{bh[0][0]}h, worst ~{bh[1][0]}h "
                            f"({bh[2]} dB swing) (Knob of Time)")
            except Exception:
                pass
            return None

        eng = dtn.DeepTune(rf, ants, cur_ant, cur_gains, env_b, CHAIN_LOG,
                           status=status,
                           cancelled=lambda: GEN[0] != my_gen,
                           record=rec_row, hour_hint=hours)
        try:
            res = eng.run()
        except Exception as exc:
            res = {"status": "error", "verdict": f"deep tune failed: {exc}"}
        if res.get("status") == "cancelled" or GEN[0] != my_gen:
            # the user's click owns the tuner now — vanish quietly
            DT.update({"on": False, "status": "cancelled — your tune wins",
                       "pct": 0})
            return
        DT.update({"on": False, "verdict": res.get("verdict", ""),
                   "vt": time.time(), "status": res.get("status", "")})
        set_stage(100, "")
        STATE.update({"tuning": False})
        if res.get("status") == "done":
            # come back to the channel with the fresh recipe applied
            # (base_env consults channel_recipes.json on this tune)
            tune(rf, prog, virt, name, force_respawn=True)

    threading.Thread(target=run, daemon=True).start()
    return "deep tune started — baseline, antenna race, gain grid (~5-12 min)"


def record(virtual, title):
    p = subprocess.run([PY, str(TOOLS / "stvt_schedule.py"), "add-show",
                        virtual, title], capture_output=True, text=True,
                       timeout=60)
    return (p.stdout + p.stderr).strip()[-500:]

# ── flight recorder: every watching/metering second becomes data ───
# One JSONL row every 10 s while the chain runs. Over days this builds
# the per-channel, per-gain, per-time-of-day picture that manual
# calibration campaigns approximate in one evening.
FLIGHT = HERE / "lab" / "flight_recorder.jsonl"

def flight_recorder():
    while True:
        time.sleep(10)
        try:
            if STATE["rf"] is not None:
                mode, rf = "watch", STATE["rf"]
            elif METER["rf"] is not None:
                mode, rf = "meter", METER["rf"]
            else:
                continue
            knobs = {k: v for k, v in (STATE.get("env") or {}).items()
                     if k in ("IFGR", "RFGAIN_SEL", "SDR_AGC",
                              "AGC_SETPOINT", "ANTENNA")}
            rec = {"iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "mode": mode, "rf": rf, "prog": STATE["prog"],
                   "knobs": knobs}
            rec.update(live_math())
            with open(FLIGHT, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass

def chain_math():
    """MER/level from the chain log only — cheap enough for 1 Hz polling
    (no live.ts reads), which is what aim-while-watching needs."""
    out = {}
    try:
        text = CHAIN_LOG.read_text(errors="ignore")[-40000:]
        errs = [float(m.group(1)) for m in RE_FS.finditer(text)][-24:]
        if errs:
            mers = [20 * math.log10(5.0 / e) for e in errs if e > 0]
            if mers:
                srt = sorted(mers)
                out["mer_db"] = round(srt[len(srt) // 2], 2)
                out["mer_last"] = round(mers[-1], 2)
        fp = RE_FPLL.findall(text)
        if fp:
            mn, mx, ir = fp[-1]
            out.update({"mean_x": float(mn), "max_x": float(mx),
                        "in_rms": float(ir)})
        cm = RE_CIR.findall(text)
        if cm:
            echoes = []
            for pair in cm[-1].strip().strip(",").split(","):
                if ":" in pair:
                    d, db = pair.split(":")
                    try:
                        echoes.append([int(d), float(db)])
                    except ValueError:
                        pass
            if echoes:
                out["cir"] = echoes
    except OSError:
        pass
    return out

def live_math():
    out = {}
    try:
        text = CHAIN_LOG.read_text(errors="ignore")[-40000:]
        errs = [float(m.group(1)) for m in RE_FS.finditer(text)][-24:]
        if errs:
            mers = [20 * math.log10(5.0 / e) for e in errs if e > 0]
            if mers:
                # median, not mean: convergence transients right after a
                # (re)tune dragged the mean down and read as false alarm
                srt = sorted(mers)
                out["mer_db"] = round(srt[len(srt) // 2], 2)
                out["mer_last"] = round(mers[-1], 2)
        fp = RE_FPLL.findall(text)
        if fp:
            mn, mx, ir = fp[-1]
            out.update({"mean_x": float(mn), "max_x": float(mx),
                        "in_rms": float(ir)})
        # live packet loss (last ~30 s of RS windows) — the one metric
        # that can't alias: RF9's fades breathe faster than the 41 Hz
        # MER sampling, so median MER reads "flawless" while 10%/min of
        # packets die (2026-07-10, the badge-lied-about-RF9 lesson)
        rs5 = RE_RS5.findall(text)[-6:]
        if rs5:
            pk = sum(int(p) for p, _ in rs5)
            bd = sum(int(b) for _, b in rs5)
            if pk:
                out["loss_pct"] = round(100.0 * bd / pk, 2)
    except OSError:
        pass
    m = ts_metrics(20) if STATE["rf"] is not None else None
    if m:
        out.update({"hdrs_s": round(m["hdrs_s"], 1),
                    "gaps_min": round(m["gaps_min"], 1),
                    "real_pct": round(m["real_pct"])})
    # feed the learned hour-curves: one row a minute while tuned — the
    # model is just this file accruing (universality: ant is an opaque
    # label from the user's own picker, never a name in code)
    if (STATE.get("rf") is not None
            and (out.get("mer_db") is not None
                 or out.get("loss_pct") is not None)
            and time.time() - _TK_LAST_REC[0] > 60):
        _TK_LAST_REC[0] = time.time()
        try:
            tkn.record({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "rf": STATE["rf"],
                        "ant": STATE.get("ant_override") or "?",
                        "mer": out.get("mer_db"),
                        "loss_pct": out.get("loss_pct"),
                        "source": "panel"})
        except (OSError, ValueError):
            pass
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
.pbar{height:9px;background:#101c30;border-radius:5px;margin-top:7px;overflow:hidden;border:1px solid #1a2c48}
.pbar div{height:100%;background:linear-gradient(90deg,#1e5fae,#67d18a);transition:width .9s;border-radius:5px}
.scanbtn{float:right;background:#1a4a3a;color:#8fe0b0;border:1px solid #2a6a52;
padding:7px 16px;border-radius:8px;cursor:pointer;font-size:13px}
.scanbtn:hover{background:#226349}.scanbtn:disabled{opacity:.4;cursor:default}
.failbanner{color:#e77;font-weight:600}
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
<button id="tabN" onclick="showTab('N')">STATS FOR NERDS</button>
<button id="tabF" onclick="showTab('F')">🎯 SIGNAL FINDER</button>
<button id="scanBtn" class="scanbtn" onclick="startScan()">📡 SCAN CHANNELS</button></div>
<div id="status">loading…</div>
<div id="pageG">
<div style="margin:4px 0 8px;font-size:12px">📡 antenna:
<select id="antpick" onchange="fetch('/api/antenna',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({antenna:this.value})})" style="background:#123;color:#cde;border:1px solid #356;padding:2px 6px">
<option value="auto">auto (belief map)</option>
<option value="Antenna B">Philips (B)</option>
<option value="Antenna A">rabbit ears (A)</option>
<option value="Antenna C">discone (C)</option>
</select>
<span style="color:#8aa">— you pick the antenna, the code decodes whatever it's given</span></div>
<div id="grid">loading guide…</div></div>
<div id="pageN" style="display:none">
  <div class="cards" id="mathcards"></div>
  <div class="cards" id="knobcards"></div>
  <h3 style="margin:14px 0 4px">🔬 TUNA SCIENCE — the invented instruments, live
    <button onclick="fetch('/api/e7',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(()=>{})"
      style="margin-left:12px;font-size:12px" title="Snapshot the last ~30s you just watched from the chain's IQ ring, decode it 3 ways, splice the best of every pass. TV keeps playing.">🩺 SECOND OPINION (E7)</button>
  </h3>
  <div class="cards" id="sciencecards"></div>
  <div id="scinotes" style="font-size:11px;color:#8aa;line-height:1.5;max-width:900px"></div>
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
<div id="pageF" style="display:none">
  <div style="max-width:900px;margin:0 auto;text-align:center">
    <div id="fchans" style="margin-bottom:14px;color:#7f96b3;font-size:12px">loading channels…</div>
    <div id="fbig" style="font-size:110px;font-weight:800;line-height:1;margin:10px 0;color:#5f7591">—</div>
    <div id="fpeak" style="color:#e7c96a;font-size:14px;margin-bottom:4px">&nbsp;</div>
    <div id="fsub" style="color:#7f96b3;font-size:13px;margin-bottom:10px">pick a channel to start metering</div>
    <div style="background:#101c30;border-radius:8px;height:26px;position:relative;margin:0 30px 6px">
      <div id="fbar" style="height:100%;width:0%;border-radius:8px;transition:width .7s,background .7s"></div>
      <div style="position:absolute;top:-4px;bottom:-4px;width:2px;background:#e7c96a;left:calc((15.2 - 8)/14*100%)"></div>
      <div style="position:absolute;top:30px;left:calc((15.2 - 8)/14*100% - 30px);color:#e7c96a;font-size:10px">cliff 15.2</div>
    </div>
    <div style="color:#5f7591;font-size:10px;margin:14px 30px 12px;display:flex;justify-content:space-between"><span>8 dB</span><span>22 dB</span></div>
    <div id="fcirwrap" style="background:#05080f;border:1px solid #26436b;border-radius:10px;padding:8px;margin:0 30px 12px">
      <div style="color:#7f96b3;font-size:11px;text-align:left;margin-bottom:4px">📡 ECHO X-RAY — the channel's impulse response (main path at 0, every other bar is a reflection)</div>
      <canvas id="fcir" width="1100" height="150" style="width:100%"></canvas>
      <div style="color:#5f7591;font-size:10px;text-align:left;margin-top:3px">x = delay in µs (1 µs ≈ 300 m of extra path) · bar height = echo strength in dB vs the main path · <span style="color:#67d18a">green = main</span> · <span style="color:#e7c96a">yellow = pre-echo</span> · <span style="color:#e77">red = post-echo</span> — aim to shrink the red and yellow, not just raise the number</div>
    </div>
    <button id="ftone" class="tune" style="font-size:14px;padding:8px 20px" onclick="toggleTone()">🔊 tone: OFF</button>
    <button id="fchirp" class="tune" style="font-size:14px;padding:8px 20px;background:#1a4a3a" onclick="toggleChirp()">🔔 record chirp: ON</button>
    <button class="stop" style="font-size:14px;padding:8px 20px" onclick="stopMeter()">stop metering</button>
    <div class="edu" style="text-align:left;margin-top:18px"><b>How to aim an antenna with this:</b>
    pick the channel you care about, turn the tone on, then move / rotate / extend
    the antenna <b>slowly</b> (multipath cells are inches wide) and listen:
    <b>higher pitch = more decode margin</b>. The number is MER — the equalizer's own
    measurement of how clean the 8-VSB constellation is. Below the yellow cliff line
    television does not exist; above ~17 dB you have comfortable margin. Red→green
    tracks the same scale. Park the antenna where the pitch peaks, wait 5 seconds to
    confirm it holds (signals breathe), then go watch TV. Remember each channel has
    its own sweet spot — aim on the one you'll watch.</div>
  </div>
</div>
<div id="toast"></div>
<script>
let TAB='G';
function showTab(t){TAB=t;
for(const p of ['G','N','F']){
document.getElementById('page'+p).style.display=t===p?'':'none';
document.getElementById('tab'+p).className=t===p?'on':'';}}
function toast(m){const el=document.getElementById('toast');el.textContent=m;el.style.display='block';
setTimeout(()=>el.style.display='none',6000)}
async function tune(rf,prog,virt,name){
const antSel=document.getElementById('antpick').value;
toast('tuning '+virt+' '+name+' — ~30s to picture'
 +(antSel==='auto'?' · antenna: auto (belief map)':' · antenna: '+antSel)
 +(rf<14?' (VHF — DAB-notch fix 2026-07-04)':''));
await fetch('/api/tune',{method:'POST',body:JSON.stringify({rf,prog,virt,name,antenna:antSel})})}
async function stopTv(){await fetch('/api/stop',{method:'POST'});toast('TV stopped — tuner idle, waterfall resumes')}
async function rec(virt,title){toast('scheduling '+title+' …');
const r=await fetch('/api/record',{method:'POST',body:JSON.stringify({virt,title})});toast(await r.text())}
function pbar(p){return `<div class="pbar"><div style="width:${p||0}%"></div></div>`}
async function startScan(){
toast('📡 channel scan starting — TV stops, takes ~3-6 min, guide refreshes itself when done');
await fetch('/api/scan',{method:'POST'})}
let BUSY=false, WAS_SCANNING=false;
async function refreshStatus(){try{const s=await (await fetch('/api/status')).json();
// reflect the persisted antenna in the picker (unless you're mid-select)
const ap=document.getElementById('antpick');
if(ap&&s.ant&&document.activeElement!==ap&&ap.value!==s.ant)ap.value=s.ant;
const scanning=s.scan&&s.scan.running;
BUSY=s.tuning||scanning;
document.getElementById('scanBtn').disabled=BUSY;
let h;
if(scanning){WAS_SCANNING=true;
 const m=Math.floor(s.scan.elapsed/60),sec=(''+s.scan.elapsed%60).padStart(2,'0');
 h=`📡 <b>scanning for channels…</b> ${m}:${sec} · <span style="color:#7f96b3">${s.scan.line||''}</span>`+pbar(s.scan.pct);}
else{
 if(WAS_SCANNING){WAS_SCANNING=false;loadGrid();toast('📡 scan complete — guide refreshed')}
 if(s.tuning) h='⏳ <b>tuning '+(s.virtual||'')+' '+(s.name||'')+'</b> — '+(s.stage||'starting…')+pbar(s.stage_pct);
 else if(s.tuned&&s.stage&&s.stage.startsWith('PLAYER')) h='🔴 <span class="failbanner">'+s.stage+'</span>'+
  ` <button class="stop" onclick="stopTv()">stop</button>`;
 else if(s.tuned){
  // live watchability from actual packet loss (last 30 s) — MER medians
  // alias on fast faders (RF9 lesson 7/10: "flawless" badge, 10% loss)
  const lp=s.loss_pct;
  const lchip=lp==null?'':(lp<0.1?` · <b style="color:#67d18a">✓ clean stream</b>`
   :lp<1?` · <b style="color:#e7c96a">⚡ occasional glitches — ${lp}% packets lost</b>`
   :lp<5?` · <b style="color:#e77">📉 glitchy right now — ${lp}% packets lost</b>`
   :` · <b style="color:#e77">📉 heavy glitching — ${lp}% packets lost (this channel breathes; try it again this evening)</b>`);
  h=`watching <b>${s.virtual} ${s.name||''}</b> (RF${s.rf} p${s.prog})`+
  ` · <b>${s.hdrs_s??'—'}</b> hdrs/s · <b>${s.gaps_min??'—'}</b> gaps/min · ${s.real_pct??'—'}% real`+lchip+
  ` <button class="stop" onclick="stopTv()">stop TV</button>`+
  ` <button class="tune" style="background:#3a2a4a;border:1px solid #5a4a7a" onclick="deepTune()" title="The channel doctor: baseline, antenna race, gain grid, disease verdict — writes a recipe future tunes reuse. ~5-12 min; click any station to abort.">🔬 DEEP TUNE</button>`;
  if(s.hdrs_s===0&&(s.real_pct||0)<5) h+=' <span style="color:#e7c96a">⚠ locked but nothing decoding — signal is under the cliff, aim with 🎯</span>';}
 else if(s.stage&&s.stage.startsWith('RADIO FAILED')) h='🔴 <span class="failbanner">'+s.stage+'</span>';
 else h='tuner idle — waterfall live on NERD tab · click a station to tune · 📡 SCAN refreshes the guide';}
if(s.deeptune&&!s.deeptune.on&&s.deeptune.verdict)
 h+='<div style="color:#8fe0b0;font-size:12px;margin-top:5px">🔬 <b>DEEP TUNE verdict:</b> '+s.deeptune.verdict+'</div>';
if(s.recipe_note)
 h+='<div style="color:#7f96b3;font-size:11px;margin-top:3px">📋 '+s.recipe_note+'</div>';
document.getElementById('status').innerHTML=h;}catch(e){}}
async function deepTune(){
toast('🔬 DEEP TUNE — the channel doctor takes the tuner: baseline, antenna race, gain grid. Verdict + recipe when done. Click any station to abort.');
const r=await fetch('/api/deeptune',{method:'POST',body:'{}'});toast(await r.json())}
async function loadGrid(){const g=await (await fetch('/api/grid')).json();
let h='<table><tr><th>station</th>';g.slots.forEach(s=>h+='<th>'+s+'</th>');h+='<th></th></tr>';
let lastRf=null;
g.rows.forEach(r=>{
if(r.rf!==lastRf){lastRf=r.rf;
const s=r.snr||0,pct=Math.max(0,Math.min(100,Math.round((s-20)/35*100)));
const col=pct>=70?'#67d18a':(pct>=45?'#e7c96a':'#e77');
const blocks='█'.repeat(Math.round(pct/10))+'░'.repeat(10-Math.round(pct/10));
// decode QUALITY from measured MER — the honest "will it look good?",
// separate from raw signal strength (the % bar). Only shown when the
// scan actually measured it. Thresholds = the survival curve.
let mtag='';
if(r.mer!=null){const m=r.mer,p10=r.mer_p10;
// a healthy MEDIAN hides impulse/breather channels — if the low tail
// plunges toward/past the cliff, say so (Fox RF31 lesson, 7/10)
const bursty=(p10!=null&&m>=16&&(m-p10)>=1.2&&p10<16.2);
// measured loss at scan time outranks any MER label: fast faders (RF9)
// alias the MER sampling and read "flawless" while packets die (7/10)
const lossy=(r.loss!=null&&r.loss>=0.3);
const mc=lossy?(r.loss>=3?'#e77':'#e7c96a'):bursty?'#e7c96a':(m>=16.5?'#67d18a':(m>=15.2?'#e7c96a':'#e77'));
const mw=lossy?(r.loss>=3?'📉 glitchy — '+r.loss+'% packets lost at scan':'⚡ some glitches — '+r.loss+'% lost at scan')
 :bursty?'⚡ bursty — glitches despite strong signal':(m>=16.5?'flawless':(m>=16?'watchable':(m>=15.2?'marginal · glitchy':'below cliff')));
mtag=`&nbsp;<span style="color:${mc};font-weight:700">◉ ${mw}</span>`+
`<span style="color:#5f7591;font-size:11px"> (MER ${m.toFixed(1)}${p10!=null?' / dips '+p10.toFixed(1):''} dB)</span>`;}
h+=`<tr><td colspan="${g.slots.length+2}" style="background:#0d1626;border-top:2px solid #26436b;padding:7px 6px">`+
`📡 <b>tower RF${r.rf}</b> &nbsp;<span style="color:${col};font-family:monospace">${blocks}</span> `+
`<span style="color:${col};font-weight:700">${pct}%</span>`+mtag+
`<span style="color:#5f7591;font-size:11px"> · pilot ${s?s.toFixed(0):'—'} dB over the noise floor`+
(s?` = <b>${Math.round(Math.pow(10,s/10)).toLocaleString()}×</b> the static`:'')+
(r.rms!=null&&g.floor!=null?` · level ${r.rms.toFixed(1)} dBFS / floor ${g.floor.toFixed(1)} dBFS`:'')+
` · one transmitter shared by every station below</span></td></tr>`}
const bc=r.tune==='+'?'b-plus':(r.tune==='~'?'b-tilde':'b-x');
h+=`<tr><td class="ch"><span class="badge ${bc}">${r.tune}</span>`+
`<button class="tune" onclick='tune(${r.rf},${r.prog},"${r.virtual}","${r.callsign}")'>${r.virtual}</button> ${r.callsign}</td>`;
r.cells.forEach((c,i)=>{h+=`<td class="${i===0?'now':''}">`+(c.cont?`<span class="cont">&raquo; ${c.title}</span>`
:(c.title?`<span class="show">${c.title}</span>`:'<span class="cont">—</span>'))+'</td>'});
const nowT=(r.cells[0]&&r.cells[0].title)||'';
h+=`<td>${nowT?`<button class="rec" onclick='rec("${r.virtual}",${JSON.stringify(nowT)})'>REC</button>`:''}</td></tr>`});
h+='</table>';
h+='<div class="edu" style="margin-top:10px"><b>Reading the tower meters:</b> every ATSC transmitter sends a '+
'constant marker tone (the <b>pilot</b>) beside its data. We measure how far it rises above the radio static '+
'(the <b>noise floor</b>) in decibels — a power ratio, where every +10 dB means 10× the power '+
'(so 30 dB = 1,000×, 48 dB = 63,000×). The % maps that onto what it takes to watch TV: '+
'<span style="color:#e77">25 dB — carrier barely detectable</span> · '+
'<span style="color:#e7c96a">35 dB — lock sometimes possible</span> · '+
'<span style="color:#67d18a">45 dB+ — solid television</span>. '+
'Note the pilot % is signal <i>strength</i> only — it proves the tower is <i>reaching</i> us, but whether the '+
'data survives the trip is <b>decode quality</b>, shown by the <b>◉ tag</b> (from measured MER when a scan '+
'captured it: green ≥16.5 dB flawless, yellow marginal, red below the ~15.2 dB cliff). A tower can read a '+
'middling strength % yet still be flawless to watch — quality is the honest number. Absolute levels are in <b>dBFS</b> — decibels below the '+
'receiver\\'s full-scale ceiling (0 dBFS = the loudest sound the converter can hear; −60 is quiet static). '+
'Diagnostic gold: if the <b>floor itself</b> rises from one scan to the next, the noise came to <i>you</i> '+
'(new interference, a powered amp, USB hash) — the towers didn\\'t get weaker.</div>';
document.getElementById('grid').innerHTML=h;buildFinderChans()}
// ── signal finder: eyes (big number, red→green) + ears (tone pitch) ──
let METER_RF=null, AC=null, OSC=null, GN=null, TONE=false, OSCS={}, NULLPOLLS=0;
async function startBalance(){METER_RF='ALL';PEAK=null;
document.getElementById('fpeak').innerHTML='&nbsp;';
if(!AC)AC=new (window.AudioContext||window.webkitAudioContext)();
const b=document.getElementById('fbig');b.textContent='—';b.style.color='#5f7591';
document.getElementById('fsub').textContent='🌐 balance mode — sweeping every tower, first chord in ~5 s';
await fetch('/api/balance',{method:'POST',body:JSON.stringify({on:true})})}
async function buildFinderChans(){try{
const cars=await (await fetch('/api/carriers')).json();
if(!cars.length){document.getElementById('fchans').textContent='no scan data — run 📡 SCAN CHANNELS first';return}
let h=`<button class="tune" style="margin:2px;background:#1a4a3a;border:1px solid #2a6a52;font-weight:700" onclick="startBalance()">🌐 ALL TOWERS (fair spot)</button> `+
`<button id="fmodebtn" class="tune" style="margin:2px;background:#3a2a4a;border:1px solid #5a4a7a" onclick="toggleFmode()">mode: 📶 MER</button>`+
` · <b>or aim on one carrier</b> (strongest first): `;
cars.slice(0,16).forEach(c=>{
const col=c.lock?'#67d18a':(c.snr>=35?'#e7c96a':'#5f7591');
h+=`<button class="tune" style="margin:2px;border:1px solid ${col}" onclick="startMeter(${c.rf})">RF${c.rf}${c.cs?' '+c.cs:''} · ${c.snr}dB${c.lock?' ✓':''}</button>`});
h+='<div style="margin-top:6px;color:#5f7591">✓ green = locked in last scan · yellow = strong carrier worth hunting · grey = faint · VHF (RF 13 and below) can be metered — a good aim there helps us crack the VHF recipe</div>';
document.getElementById('fchans').innerHTML=h}catch(e){}}
let PEAK=null, CHIRP=true, FMODE='MER';
function toggleFmode(){FMODE=(FMODE==='MER')?'FLAT':'MER';
const b=document.getElementById('fmodebtn');
if(b)b.textContent=FMODE==='MER'?'mode: 📶 MER':'mode: 📏 FLATNESS';
toast(FMODE==='MER'?'MER mode — needs a lock; the decode-margin dial'
:'FLATNESS mode — no lock needed; measures the band\\'s ripple canyons. Built for RF7-class channels: aim to FILL the canyons (lower dB = better)')}
function toggleChirp(){CHIRP=!CHIRP;
document.getElementById('fchirp').textContent='🔔 record chirp: '+(CHIRP?'ON':'OFF')}
function chirp(){if(!AC)return;const o=AC.createOscillator(),g=AC.createGain();
o.type='sine';o.connect(g);g.connect(AC.destination);
o.frequency.setValueAtTime(880,AC.currentTime);
o.frequency.setValueAtTime(1320,AC.currentTime+0.07);
g.gain.setValueAtTime(0.25,AC.currentTime);
g.gain.exponentialRampToValueAtTime(0.001,AC.currentTime+0.22);
o.start();o.stop(AC.currentTime+0.25)}
async function startMeter(rf){PEAK=null;
document.getElementById('fpeak').innerHTML='&nbsp;';
if(!AC)AC=new (window.AudioContext||window.webkitAudioContext)();
if(FMODE==='FLAT'){METER_RF='FLAT';
document.getElementById('fsub').textContent='📏 flatness meter starting on RF'+rf+' — first ripple reading in ~5 s';
await fetch('/api/flat',{method:'POST',body:JSON.stringify({rf})});return}
METER_RF=rf;
document.getElementById('fsub').textContent='starting chain on RF'+rf+' — first number in ~15-25 s';
await fetch('/api/meter',{method:'POST',body:JSON.stringify({rf})})}
async function stopMeter(){METER_RF=null;toneOff();
const b=document.getElementById('fbig');b.textContent='—';b.style.color='#5f7591';
document.getElementById('fsub').textContent='meter stopped';
await fetch('/api/balance',{method:'POST',body:JSON.stringify({on:false})});
await fetch('/api/flat',{method:'POST',body:JSON.stringify({})});
await fetch('/api/meter/stop',{method:'POST'})}
function toneOff(){TONE=false;if(OSC){try{OSC.stop()}catch(e){}OSC=null}
for(const k in OSCS){try{OSCS[k].o.stop()}catch(e){}}OSCS={};
document.getElementById('ftone').textContent='🔊 tone: OFF'}
function toggleTone(){if(TONE){toneOff();return}
TONE=true;if(!AC)AC=new (window.AudioContext||window.webkitAudioContext)();
if(METER_RF!=='ALL'){
OSC=AC.createOscillator();GN=AC.createGain();GN.gain.value=0.12;
OSC.type='sine';OSC.frequency.value=200;OSC.connect(GN);GN.connect(AC.destination);OSC.start();}
document.getElementById('ftone').textContent='🔊 tone: ON'}
async function pollMeter(){if(TAB!=='F')return;
try{const m=await (await fetch('/api/meter')).json();
if(m.watching)METER_RF=null;
if(METER_RF===null&&!m.watching)return;
if(METER_RF==='FLAT'){
if(!m.flat||m.flat.ripple===null||m.flat.ripple===undefined)return;
const rip=m.flat.ripple;
// score: lower ripple = better; hue green under ~8 dB, red past ~25
const good=Math.max(0,Math.min(1,(28-rip)/22)), hue=good*120;
const big=document.getElementById('fbig');big.textContent=rip.toFixed(1);
big.style.color=`hsl(${hue},75%,55%)`;
const score=35-rip;
if(PEAK===null||score>PEAK+0.3){const beat=PEAK!==null;PEAK=score;
document.getElementById('fpeak').innerHTML='🏆 flattest so far: <b>'+(35-PEAK).toFixed(1)+' dB ripple</b>';
if(beat&&CHIRP)chirp()}
document.getElementById('fsub').innerHTML=`📏 RF${m.flat.rf} band ripple <b>${rip.toFixed(1)} dB</b> — `+
(rip<8?'<span style="color:#67d18a">flat — a lock is plausible, try MER mode</span>'
:(rip<15?'<span style="color:#e7c96a">wavy — getting close, keep moving</span>'
:'<span style="color:#e77">deep canyons — multipath is shredding the data band</span>'));
const bar=document.getElementById('fbar');
bar.style.width=Math.max(0,Math.min(100,good*100))+'%';
bar.style.background=`hsl(${hue},70%,45%)`;
if(TONE&&OSC)OSC.frequency.setTargetAtTime(180+good*1320,AC.currentTime,0.1);
return}
if(METER_RF==='ALL'){
if(!m.balance)return;
const ent=Object.entries(m.balance).map(([rf,v])=>[parseInt(rf),v]);
if(!ent.length)return;
const minE=ent.reduce((a,b)=>b[1]<a[1]?b:a);
const minV=minE[1], hue=Math.max(0,Math.min(1,(minV-15)/30))*120;
const big=document.getElementById('fbig');big.textContent=minV.toFixed(1);
big.style.color=`hsl(${hue},75%,55%)`;
if(PEAK===null||minV>PEAK+0.2){const beat=PEAK!==null;PEAK=minV;
document.getElementById('fpeak').innerHTML='🏆 best fair-spot score: <b>'+PEAK.toFixed(1)+'</b> (weakest tower)';
if(beat&&CHIRP)chirp()}
let bh='';ent.sort((a,b)=>a[0]-b[0]);
for(const [rf,v] of ent){const hpct=Math.max(4,Math.min(100,(v-10)/40*100));
const hu=Math.max(0,Math.min(1,(v-15)/30))*120;
bh+=`<div style="display:inline-block;width:62px;margin:0 5px;text-align:center">`+
`<div style="height:90px;display:flex;align-items:flex-end;justify-content:center">`+
`<div style="width:34px;height:${hpct}%;background:hsl(${hu},70%,50%);border-radius:4px 4px 0 0;transition:height .4s"></div></div>`+
`<div style="color:#9fb4d0;font-size:11px">RF${rf}</div>`+
`<div style="color:hsl(${hu},70%,60%);font-weight:700;font-size:12px">${v.toFixed(0)}</div></div>`}
document.getElementById('fsub').innerHTML='weakest tower: <b>RF'+minE[0]+'</b> — each tower sings its own note; '+
'<b>one clean unison note = the fair spot</b>, a low drone = someone starving'+
'<div style="margin-top:8px">'+bh+'</div>';
const bar=document.getElementById('fbar');
bar.style.width=Math.max(0,Math.min(100,(minV-10)/40*100))+'%';
bar.style.background=`hsl(${hue},70%,45%)`;
if(TONE){const n=ent.length;
for(const [rf,v] of ent){
if(!OSCS[rf]){const o=AC.createOscillator(),g=AC.createGain();
g.gain.value=0.11/Math.max(1,n);o.type='sine';o.connect(g);g.connect(AC.destination);o.start();OSCS[rf]={o,g}}
OSCS[rf].o.frequency.setTargetAtTime(180+Math.max(0,Math.min(1,(v-10)/40))*1320,AC.currentTime,0.1)}}
return}
if(m.rf===null){NULLPOLLS++;
if(NULLPOLLS>4){if(TONE)toneOff();METER_RF=null;
document.getElementById('fsub').textContent='meter released (tuner took over) — pick a channel to meter again'}
return}
NULLPOLLS=0;
if(m.mer_last===undefined)return;
const v=m.mer_last, hue=Math.max(0,Math.min(1,(v-8)/12))*120;
const big=document.getElementById('fbig');big.textContent=v.toFixed(1);
big.style.color=`hsl(${hue},75%,55%)`;
if(PEAK===null||v>PEAK+0.05){const beat=PEAK!==null;PEAK=v;
document.getElementById('fpeak').innerHTML='🏆 session best: <b>'+PEAK.toFixed(2)+' dB</b>'+(PEAK>=15.2?' — over the cliff!':'');
if(beat&&CHIRP)chirp()}
document.getElementById('fsub').innerHTML=
(m.watching?`🔴 <b>LIVE — watching ${m.virtual||('RF'+m.rf)}</b> · aim while the picture plays · `:`RF${m.rf} · `)+
`MER <b>${v.toFixed(2)} dB</b> · level ${m.in_rms??'—'} · `+
(v>=15.2?'<span style="color:#67d18a">ABOVE the cliff — TV exists here</span>'
:'<span style="color:#e77">below the cliff — keep aiming ('+(15.2-v).toFixed(1)+' dB to go)</span>');
const bar=document.getElementById('fbar');
bar.style.width=Math.max(0,Math.min(100,(v-8)/14*100))+'%';
bar.style.background=`hsl(${hue},70%,45%)`;
if(TONE&&OSC)OSC.frequency.setTargetAtTime(180+Math.max(0,Math.min(1,(v-6)/16))*1320,AC.currentTime,0.08);
if(m.cir)drawCir(m.cir);
}catch(e){}}
// ── echo X-ray renderer: channel impulse response as living bars ──
const SYM_US=0.0929;   // one ATSC symbol in microseconds
function drawCir(echoes){
const c=document.getElementById('fcir');if(!c)return;const g=c.getContext('2d');
const W=c.width,H=c.height,DBFLOOR=-40;
g.fillStyle='#05080f';g.fillRect(0,0,W,H);
let lo=-3,hi=6;   // µs view, auto-grow to data
for(const [d,db] of echoes){const us=d*SYM_US;if(us<lo)lo=us-1;if(us>hi)hi=us+1;}
const xOf=us=>(us-lo)/(hi-lo)*W;
// grid: 2 µs ticks + labels
g.strokeStyle='#101c30';g.fillStyle='#5f7591';g.font='10px monospace';
for(let u=Math.ceil(lo/2)*2;u<=hi;u+=2){const x=xOf(u);
g.beginPath();g.moveTo(x,0);g.lineTo(x,H-14);g.stroke();
g.fillText(u+'µs',x-12,H-3)}
// dB rings
for(const db of [-10,-20,-30]){const y=((-db)/(-DBFLOOR))*(H-16);
g.strokeStyle='rgba(38,67,107,0.5)';g.beginPath();g.moveTo(0,y);g.lineTo(W,y);g.stroke();
g.fillText(db+'dB',4,y-2)}
// bars: height maps 0..-30 dB
for(const [d,db] of echoes){
const us=d*SYM_US,x=xOf(us);
const h=Math.max(3,(1-Math.max(DBFLOOR,db)/DBFLOOR)*(H-16));
g.fillStyle=d===0?'#67d18a':(d<0?'#e7c96a':'#e77');
g.fillRect(x-2,(H-16)-h,4,h);}
}
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
// ── TUNA SCIENCE cards ──
let sc='';const SC=s.science||{};
if(SC.survival){const p=SC.survival.pct;
sc+=card('SURVIVAL CURVE',p+'%','packets that live · '+(SC.survival.watchable?'WATCHABLE':'below 16 = not watchable'),p>=85?'good':(p>=50?'warn':'bad'))}
if(SC.timeknob){const tk=SC.timeknob;
const tkv=(tk.now_mer!=null?tk.now_mer.toFixed(1)+' dB':(tk.spark?'—':'learning'));
let tks=(tk.spark?'<span style="font-family:monospace;letter-spacing:1px">'+tk.spark+'</span> 24h curve · ':'');
tks+=(tk.best_hour!=null?'best ~'+tk.best_hour+'h · worst ~'+tk.worst_hour+'h ('+tk.swing_db+' dB swing)':'learning this channel\\'s hours');
if(tk.hint)tks+=' · '+tk.hint;
tks+=' · <b>'+(tk.n_samples||0)+'</b> samples and counting (+1/min while you watch) · '+(tk.now_owner||'?');
sc+=card('🕰 KNOB OF TIME — learned, this hour',tkv,tks)}
if(SC.turbo){const tb=SC.turbo;
const tbs=(tb.fail_ema>=4?'⚠ standing down — channel failing beyond rescue ('+tb.fail_ema+'%)':'fail rate '+tb.fail_ema+'% · '+tb.att+' attempted'+(tb.skip>0?' · '+tb.skip+' skipped (stampede gate)':''));
sc+=card('TURBO RESCUE',tb.resc,tbs+' — packets brought back from the dead by pinned re-decode')}
if(SC.oso!=null)sc+=card('REALTIME HEALTH',SC.oso===0?'✓ 0':'⚠ '+SC.oso,SC.oso===0?'sample overflows this session — the decoder is beating the deadline':'sample overflows — the decoder missed its realtime deadline; something is stealing CPU');
if(SC.guard_fires!==undefined)sc+=card('MOD-12 GUARD',SC.guard_fires,'slips healed this session',SC.guard_fires>20?'warn':'good');
if(SC.sheriff)sc+=card('FEC SHERIFF',SC.sheriff.action,'last action · '+SC.sheriff.t);
if(SC.dawn)sc+=card('DAWN FORECAST',SC.dawn.score,SC.dawn.verdict);
if(SC.e7){sc+=card('E7 SECOND OPINION',SC.e7.length>34?SC.e7.slice(0,34)+'…':SC.e7,SC.e7,SC.e7.startsWith('done')?'good':(SC.e7.startsWith('error')?'bad':'warn'));
window.e7play=function(){fetch("/api/e7/play",{method:"POST"})};
if(SC.e7.includes('replay ready'))sc+='<div style="margin:4px 0"><button onclick="e7play()">▶ watch healed replay (recording, not live)</button></div>'}
if(SC.oracle&&SC.oracle.paths)sc+=card('BEACON ORACLE',Object.entries(SC.oracle.paths).map(([k,v])=>k.slice(0,4)+':'+(v===null?'—':v)).join(' '),'path dB vs baseline · '+SC.oracle.t);
document.getElementById('sciencecards').innerHTML=sc;
document.getElementById('scinotes').innerHTML=
'<b>Survival curve</b>: packet survival vs MER is an S-curve (50% at 15.2 dB, watchable TV needs 16+) — measured live on YOUR rig, not copied from a datasheet. '+
'<b>Time knob</b>: which antenna owns which channel flips by hour (ionosphere, foliage, temperature inversions) — the map learns YOUR hours and picks accordingly. '+
'<b>Mod-12 guard</b>: a stream hiccup rotates the viterbi\\'s 12-decoder grid (was: minutes of garbage); the guard drops ≤11 segments and heals it in one field. '+
'<b>FEC sheriff</b>: Reed-Solomon truth polices the adaptive layers — surgery, then the viterbi scalpel, then restart. '+
'<b>Dawn forecast</b>: the nearest weather balloon\\'s refractivity profile predicts tropospheric-ducting windows — distant towers can appear at sunrise. '+
'<b>Beacon oracle</b>: strong local FM stations double as free path-sounders — a hot path toward a distant city says go fish for that city\\'s TV. '+
'<b>DFE anchor</b>: the echo-canceller\\'s feedback taps trained against the pilot anchor — on multipath-torn channels it halved bad packets and added +51% frames in replay trials; rides automatically in cliff mode. '+
'<b>GMD ladder</b>: Forney\\'s 1966 iterate-between-the-codes algorithm, powered by the viterbi\\'s own per-byte doubt (SOVA) — tries 2,4,…16 erasures and keeps the first answer that passes four independent truth checks. '+
'<b>E7 second opinion</b>: decode the same airwaves twice with different equalizer trajectories and each pass rescues DIFFERENT picture groups — the healed merge splices clean twins over damage (never drops a frame).';
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
loadGrid();
(async function statusLoop(){await refreshStatus();
setTimeout(statusLoop,BUSY?1800:8000)})();
setInterval(loadGrid,300000);
setInterval(refreshNerd,3000);setInterval(refreshWf,1200);
setInterval(pollMeter,1000);
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
    floor = None
    rms_by_rf = {}
    try:
        d = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
        floor = d.get("noise_floor_dbfs")
        rms_by_rf = {c["rf"]: c.get("rms_dbfs")
                     for c in d.get("channels", [])}
    except (OSError, json.JSONDecodeError):
        pass
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
                     "mer": ch.get("mer_med"), "mer_p10": ch.get("mer_p10"),
                     "loss": ch.get("loss_pct"),
                     "rms": rms_by_rf.get(ch["rf"]),
                     "cells": cells})
    return {"slots": slot_labels, "rows": rows, "floor": floor}


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
            st["ant"] = STATE.get("ant_override") or "auto"
            st["tuned"] = STATE["rf"] is not None and not STATE["tuning"]
            st["stage"] = STATE.get("stage") or ""
            st["stage_pct"] = STATE.get("stage_pct") or 0
            st["scan"] = {"running": SCAN["running"], "line": SCAN["line"],
                          "pct": SCAN["pct"],
                          "elapsed": int(time.time() - SCAN["t0"])
                                     if SCAN["t0"] else 0}
            st["deeptune"] = {"on": DT["on"], "status": DT["status"],
                              "pct": DT["pct"],
                              "verdict": DT["verdict"]
                              if not DT["on"]
                              and time.time() - DT["vt"] < 1800 else ""}
            st["recipe_note"] = STATE.get("recipe_note") or ""
            if st["tuned"]:
                st.update(live_math())
            self._send(json.dumps(st))
        elif self.path == "/api/meter":
            # aim-while-watching: when no dedicated meter runs but TV is
            # playing, serve the live chain's telemetry instead — that's
            # how rabbit ears are really aimed: with the picture on.
            rf = METER["rf"] if METER["rf"] is not None else STATE["rf"]
            out = {"rf": rf,
                   "watching": METER["rf"] is None and STATE["rf"] is not None,
                   "virtual": STATE["virtual"] if METER["rf"] is None else None}
            if rf is not None:
                out.update(chain_math())
            if BAL["on"] and BAL["scores"]:
                out["balance"] = BAL["scores"]
            if FLAT["on"]:
                out["flat"] = {"rf": FLAT["rf"], "ripple": FLAT["ripple"]}
            self._send(json.dumps(out))
        elif self.path == "/api/carriers":
            try:
                data = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
                cars = [{"rf": c["rf"],
                         "snr": round(c.get("pilot_snr_db") or 0, 1),
                         "lock": bool(c.get("lock")),
                         "cs": c.get("callsign") or ""}
                        for c in data.get("channels", [])
                        if not c.get("not_detected")]
                cars.sort(key=lambda x: -x["snr"])
                self._send(json.dumps(cars))
            except (OSError, json.JSONDecodeError, KeyError):
                self._send("[]")
        elif self.path == "/api/nerd":
            self._send(json.dumps({"rf": STATE["rf"], "prog": STATE["prog"],
                                   "knobs": STATE.get("env") or {},
                                   "live": live_math(),
                                   "science": science_data()}))
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
        # robust input handling (2026-07-09 stress-test finding): a
        # malformed body used to raise in json.loads and drop the
        # connection. Validate first, answer 400 cleanly instead.
        try:
            n = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            req = json.loads(raw or b"{}")
            if not isinstance(req, dict):
                raise ValueError("body is not a JSON object")
        except (ValueError, json.JSONDecodeError):
            self._send('"bad request: body must be a JSON object"')
            return
        if self.path == "/api/tune":
            try:
                rf, prog, virt = req["rf"], req["prog"], req["virt"]
                int(rf); int(prog)          # type sanity
            except (KeyError, TypeError, ValueError):
                self._send('"bad request: tune needs numeric rf, prog, virt"')
                return
            set_antenna(req.get("antenna"))
            threading.Thread(target=tune,
                             args=(rf, prog, virt, req.get("name", "")),
                             daemon=True).start()
            self._send('"tuning"')
        elif self.path == "/api/stop":
            threading.Thread(target=stop_tv, daemon=True).start()
            self._send('"stopped"')
        elif self.path == "/api/scan":
            threading.Thread(target=run_scan, daemon=True).start()
            self._send('"scanning"')
        elif self.path == "/api/meter":
            try:
                rf = int(req["rf"])
            except (KeyError, TypeError, ValueError):
                self._send('"bad request: meter needs a numeric rf"')
                return
            threading.Thread(target=meter_start, args=(rf,),
                             daemon=True).start()
            self._send('"metering"')
        elif self.path == "/api/meter/stop":
            BAL["on"] = False
            threading.Thread(target=meter_stop, daemon=True).start()
            self._send('"meter stopped"')
        elif self.path == "/api/balance":
            if req.get("on"):
                threading.Thread(target=balance_start, daemon=True).start()
                self._send('"balance on"')
            else:
                BAL["on"] = False
                self._send('"balance off"')
        elif self.path == "/api/flat":
            rf = req.get("rf")
            if rf:
                try:
                    rf = int(rf)
                except (TypeError, ValueError):
                    self._send('"bad request: flat needs a numeric rf"')
                    return
                threading.Thread(target=flat_start, args=(rf,),
                                 daemon=True).start()
                self._send('"flatness on"')
            else:
                FLAT["on"] = False
                self._send('"flatness off"')
        elif self.path == "/api/record":
            virt, title = req.get("virt"), req.get("title")
            if not virt or not title:
                self._send('"bad request: record needs virt and title"')
                return
            out = record(virt, title)
            self._send(out or "scheduled", "text/plain; charset=utf-8")
        elif self.path == "/api/deeptune":
            self._send(json.dumps(deeptune_start()))
        elif self.path == "/api/e7":
            try:
                secs = int(req.get("secs", 30))
            except (TypeError, ValueError):
                secs = 30
            e7_run(secs)
            self._send('"second opinion started"')
        elif self.path == "/api/antenna":
            ant = req.get("antenna")
            set_antenna(ant)
            self._send(json.dumps(f"antenna: {ant or 'auto'} (saved; "
                                  "applies to next tune/scan)"))
        elif self.path == "/api/e7/play":
            hp = HERE / "lab" / "e7_healed.ts"
            if hp.exists() and hp.stat().st_size > 500_000:
                subprocess.Popen(
                    [_MPV, str(hp), "--force-window=yes", "--keep-open=yes",
                     "--geometry=45%",
                     "--title=E7 REPLAY of the last 30 s (not live TV)"])
                self._send('"replay opened"')
            else:
                self._send('"no healed replay yet"')
        else:
            self.send_error(404)


class Panel(ThreadingHTTPServer):
    # default backlog of 5 refused connections under a burst (stress-test
    # finding 2026-07-09: 57/244 concurrent requests got WinError 10061).
    request_queue_size = 128
    daemon_threads = True


def chain_doctor():
    """CHAIN AUTO-HEAL (2026-07-10): the SDRplay API service crashed 8x
    in 7 days (Windows event log, 0xc0000005) — each crash silently
    kills the live chain (no traceback; the service auto-restarts, so
    everything looks normal afterward). This was the week's phantom:
    dead chains -> looping/stalled players -> "glitchy TV". The doctor
    converts each occurrence into a ~40 s self-heal with an honest
    banner. Bounded retries per the recovery-loop law."""
    import psutil
    heals = []
    misses = 0
    while True:
        time.sleep(20)
        try:
            if (STATE.get("rf") is None or STATE.get("tuning")
                    or SCAN.get("running") or BAL.get("on")
                    or FLAT.get("on")):
                misses = 0
                continue
            alive = 0
            for pr in psutil.process_iter(["name", "cmdline"]):
                try:
                    if pr.info["name"] == "python.exe" and any(
                            "tv_live" in (c or "")
                            for c in (pr.info["cmdline"] or [])):
                        alive += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if alive > 0:
                misses = 0
                continue
            misses += 1
            if misses < 2:
                continue                      # 40 s confirmation
            misses = 0
            now = time.time()
            heals[:] = [h for h in heals if now - h < 3600]
            if len(heals) >= 4:
                set_stage(0, "chain keeps dying (SDRplay service "
                             "crashing) — auto-heal limit reached; "
                             "restart the SDRplay service or replug")
                continue
            heals.append(now)
            rf, prog = STATE.get("rf"), STATE.get("prog")
            virt, name = STATE.get("virtual"), STATE.get("name", "")
            set_stage(5, "chain died (SDRplay service hiccup) — "
                         "self-healing: retuning")
            threading.Thread(target=tune, args=(rf, prog, virt, name),
                             daemon=True).start()
        except Exception:
            pass


if __name__ == "__main__":
    # STVT_PANEL_NOSWEEP=1: skip the spectrum sweeper (which opens the
    # SDR) so the API can be tested without contending for the radio.
    if not os.environ.get("STVT_PANEL_NOSWEEP"):
        threading.Thread(target=sweeper, daemon=True).start()
    threading.Thread(target=flight_recorder, daemon=True).start()
    threading.Thread(target=chain_doctor, daemon=True).start()
    print(f"TV Tuna panel: http://localhost:{PORT}", flush=True)
    Panel(("127.0.0.1", PORT), H).serve_forever()
