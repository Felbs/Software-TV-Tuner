"""ui_lab.py — workday lab (2026-07-09): UI stress test + video-quality sweep.

Two phases, run once, then the quality sweep loops until 18:00.

PHASE A — UI/API STRESS (~20 min): hammer the panel's HTTP API to find
bugs the way a chaotic user or a flaky network would. Malformed bodies,
missing fields, impossible channels, endpoints called out of order,
concurrency floods, rapid tune/stop/hop/antenna cycles — after each, the
panel MUST still answer and MUST NOT leak orphaned tv_live/mpv processes.
Every anomaly is logged as a bug candidate (fixes happen on review, not
blindly mid-run).

PHASE B — VIDEO QUALITY SWEEP (until 18:00): for every channel on every
antenna port, tune the chain headlessly and measure the HONEST decode
quality over a fixed window — real frames + decode errors via ffmpeg
null-sink, plus MER median/low-tail and RS loss. Repeated cycles give a
quality-vs-time-of-day picture too. Panel is restored at the end.

STOP file: lab/ui_lab/STOP.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import overnight_cube as oc                      # noqa: E402

PY = sys.executable
PANEL = "http://127.0.0.1:8642"
LAB = HERE / "lab" / "ui_lab"
LAB.mkdir(parents=True, exist_ok=True)
STOP = LAB / "STOP"
LOG = HERE / "ui_lab.jsonl"
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
END_AT = os.environ.get("UI_LAB_END", "18:00")

GAINS = {36: (3, 40), 34: (2, 32), 15: (1, 32), 7: (5, 32), 9: (5, 32),
         21: (2, 32), 35: (2, 32)}
DEFAULT_GAIN = (3, 40)
# port map: rabbit=A, philips=B, discone=C
SWEEP = [("Antenna B", "philips", [36, 34, 15, 9, 21, 7]),
         ("Antenna A", "rabbit", [21, 34, 35, 9, 7]),
         ("Antenna C", "discone", [7, 9])]


def log_event(o):
    o["t"] = time.strftime("%H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(o) + "\n")
    print(f"[ui_lab {o['t']}] {json.dumps(o)[:200]}", flush=True)


def stopped():
    return STOP.exists()


def past_end():
    now = time.localtime()
    h, m = map(int, END_AT.split(":"))
    return now.tm_hour * 60 + now.tm_min >= h * 60 + m


def req(method, path, body=None, timeout=12):
    """-> (code, text, secs). code is int or 'ERR'. Never raises."""
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    r = urllib.request.Request(
        PANEL + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), \
                round(time.time() - t0, 2)
    except urllib.error.HTTPError as e:
        return e.code, str(e)[:80], round(time.time() - t0, 2)
    except Exception as e:
        return "ERR", str(e)[:80], round(time.time() - t0, 2)


def proc_count(pattern):
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"@(Get-WmiObject Win32_Process -Filter \"Name='python.exe'\" "
             f"| Where-Object {{$_.CommandLine -match '{pattern}'}}).Count"],
            capture_output=True, text=True, timeout=30)
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return -1


def mpv_count():
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-Process mpv -ErrorAction SilentlyContinue).Count"],
            capture_output=True, text=True, timeout=30)
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return -1


def panel_alive():
    code, _, _ = req("GET", "/api/status", timeout=6)
    return code == 200


def ensure_panel():
    if panel_alive():
        return True
    log_event({"event": "panel-restart", "why": "not responding"})
    env = dict(os.environ)
    env["PATH"] = r"C:\Program Files\SDRplay\API\x64;" + env.get("PATH", "")
    subprocess.Popen([PY, "-u", str(HERE / "tv_tuna_panel.py")], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(2)
        if panel_alive():
            return True
    return False


def kill_radio():
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-WmiObject Win32_Process -Filter \"Name='python.exe'\" | "
         "Where-Object {$_.CommandLine -match 'tv_live|tv_watch'} | "
         # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign); loop also sweeps mpv/ffmpeg players
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
         # kill-ok: player/pipeline consumer (mpv/ffmpeg/tail), not the SDR holder
         "Get-Process mpv -ErrorAction SilentlyContinue | Stop-Process -Force"],
        capture_output=True, timeout=30)


# ── PHASE A: UI/API stress ─────────────────────────────────────────
def phase_ui_stress():
    bugs = []
    if not ensure_panel():
        log_event({"event": "FATAL", "why": "panel won't start for stress"})
        return bugs
    base_live = proc_count("tv_live")
    log_event({"event": "stress-start", "baseline_tv_live": base_live})

    # A1 — malformed / edge requests. Panel must answer gracefully
    # (2xx or a clean 4xx), never drop the connection (ERR/5xx = missing
    # input validation), and must survive each one.
    edge = [
        ("POST", "/api/tune", b"{}", "tune: no fields"),
        ("POST", "/api/tune", b'{"rf":99,"prog":1,"virt":"99.1","name":"x"}',
         "tune: nonexistent RF99"),
        ("POST", "/api/tune", b'{"rf":"abc","prog":1}', "tune: rf wrong type"),
        ("POST", "/api/tune", b"not json", "tune: malformed JSON"),
        ("POST", "/api/tune", b"", "tune: empty body"),
        ("POST", "/api/antenna", b'{"antenna":"garbage"}', "antenna: garbage"),
        ("POST", "/api/antenna", b"{}", "antenna: missing"),
        ("POST", "/api/e7", b"{}", "e7: nothing tuned"),
        ("POST", "/api/e7/play", b"{}", "e7 play: no file"),
        ("GET", "/api/nope", None, "unknown path (404 expected)"),
        ("POST", "/api/record", b"{}", "record: no fields"),
    ]
    for m, p, b, desc in edge:
        code, txt, el = req(m, p, b)
        alive = panel_alive()
        bad = (code == "ERR" or (isinstance(code, int) and code >= 500))
        rec = {"event": "edge", "desc": desc, "code": code,
               "secs": el, "panel_alive": alive}
        if bad or not alive:
            rec["BUG"] = True
            bugs.append(f"{desc} -> code={code} alive={alive}")
        log_event(rec)
        if not alive and not ensure_panel():
            log_event({"event": "FATAL", "why": f"panel dead after {desc}"})
            return bugs
    # clean up anything the edge tunes started
    req("POST", "/api/stop", b"{}")
    time.sleep(3)

    # A2 — concurrency flood: many parallel reads + a few writes at once.
    # Watch for hangs, 5xx, or the panel wedging.
    errors = {"n": 0}

    def hammer(path, method="GET", body=None):
        code, _, el = req(method, path, body, timeout=10)
        if code == "ERR" or (isinstance(code, int) and code >= 500) or el > 8:
            errors["n"] += 1

    threads = []
    for i in range(60):
        for path in ("/api/status", "/api/grid", "/api/nerd", "/api/meter"):
            threads.append(threading.Thread(target=hammer, args=(path,)))
    # sprinkle in some concurrent antenna flips (state writes)
    for a in ("Antenna A", "Antenna B", "Antenna C", "auto"):
        threads.append(threading.Thread(
            target=hammer, args=("/api/antenna", "POST", {"antenna": a})))
    t0 = time.time()
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=15)
    flood = {"event": "flood", "requests": len(threads),
             "errors": errors["n"], "secs": round(time.time() - t0, 1),
             "panel_alive": panel_alive()}
    if errors["n"] or not flood["panel_alive"]:
        flood["BUG"] = True
        bugs.append(f"concurrency flood: {errors['n']} errors, "
                    f"alive={flood['panel_alive']}")
    log_event(flood)
    ensure_panel()

    # A3 — real state-machine cycles: rapid tune / stop / hop / antenna,
    # each a chance for a stuck state or a leaked process. Uses the real
    # radio, so a handful only.
    cycles = [
        ("tune+stop", {"rf": 36, "prog": 3, "virt": "4.1", "name": "t",
                       "antenna": "Antenna B"}, True),
        ("hop-same-mux", {"rf": 36, "prog": 4, "virt": "4.2", "name": "t",
                          "antenna": "Antenna B"}, True),
        ("cross-mux", {"rf": 34, "prog": 3, "virt": "34.1", "name": "t",
                       "antenna": "Antenna B"}, True),
        ("bad-then-good", {"rf": 99, "prog": 1, "virt": "99", "name": "t",
                           "antenna": "Antenna B"}, False),
        ("recover-good", {"rf": 36, "prog": 3, "virt": "4.1", "name": "t",
                          "antenna": "Antenna B"}, True),
    ]
    for name, body, expect_play in cycles:
        if stopped():
            break
        req("POST", "/api/tune", body)
        # give it up to 75s to reach a terminal state
        played = False
        stage = ""
        for _ in range(75):
            time.sleep(1)
            code, txt, _ = req("GET", "/api/status")
            if code == 200:
                try:
                    s = json.loads(txt)
                    stage = s.get("stage") or ""
                    if s.get("tuned") and not s.get("tuning"):
                        played = True
                        break
                    if "PLAYER never appeared" in stage:
                        break
                except ValueError:
                    pass
        rec = {"event": "state-cycle", "name": name, "played": played,
               "mpv": mpv_count(), "stage": stage[:50]}
        # leak check: after a tune, mpv should exist iff it played
        if expect_play and not played:
            rec["NOTE"] = "expected play, didn't (may be propagation)"
        log_event(rec)
        req("POST", "/api/stop", b"{}")
        time.sleep(4)
        # after stop, no mpv should linger
        lingering = mpv_count()
        if lingering > 0:
            bugs.append(f"{name}: {lingering} mpv lingering after stop")
            log_event({"event": "leak", "name": name, "mpv_after_stop":
                       lingering, "BUG": True})
            kill_radio()

    # A4 — final leak audit
    time.sleep(3)
    leaked_live = proc_count("tv_live") - base_live
    leaked_mpv = mpv_count()
    audit = {"event": "leak-audit", "leaked_tv_live": leaked_live,
             "leaked_mpv": leaked_mpv, "bugs_found": len(bugs)}
    if leaked_live > 0 or leaked_mpv > 0:
        audit["BUG"] = True
        bugs.append(f"process leak: +{leaked_live} tv_live, {leaked_mpv} mpv")
    log_event(audit)
    log_event({"event": "STRESS-DONE", "bug_count": len(bugs), "bugs": bugs})
    return bugs


# ── PHASE B: video quality sweep ───────────────────────────────────
def null_sink(prog):
    """Real decode quality on the just-captured live.ts: (frames, errors)."""
    if not LIVE.exists() or LIVE.stat().st_size < 2_000_000:
        return 0, 0, 0.0
    tmp = LAB / "q.ts"
    try:
        data = LIVE.read_bytes()[-40_000_000:]
        tmp.write_bytes(data)
    except OSError:
        return 0, 0, 0.0
    frames = errors = 0
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "info", "-i", str(tmp),
             "-map", f"0:p:{prog}?", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120)
        err = r.stderr or ""
        for tok in err.split("frame="):
            try:
                frames = int(tok.strip().split()[0])
            except (ValueError, IndexError):
                pass
        errors = sum(1 for ln in err.splitlines()
                     if "error" in ln.lower() or "corrupt" in ln.lower())
    except Exception:
        pass
    secs = len(data) / 1e6 / 0.8      # rough: ~0.8 MB/s program video
    gpm = round(errors / max(1, secs) * 60, 1)
    return frames, errors, gpm


def phase_quality_sweep():
    # free the radio from the panel for headless sampling
    kill_radio()
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-WmiObject Win32_Process -Filter \"Name='python.exe'\""
                    " | Where-Object {$_.CommandLine -match 'tv_tuna_panel'} |"
                    # kill-ok: panel stop for radio handoff - pre-warden TV family; graceful stop arrives with warden citizenship (bare-open campaign)
                    " ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
                   capture_output=True, timeout=30)
    time.sleep(3)
    cycle = 0
    while not stopped() and not past_end():
        cycle += 1
        for port, ant, rfs in SWEEP:
            for rf in rfs:
                if stopped() or past_end():
                    break
                rfg, ifgr = GAINS.get(rf, DEFAULT_GAIN)
                try:
                    s = oc.sample(rf, port, rfg, ifgr, secs=75)
                except Exception as e:
                    log_event({"event": "q-error", "rf": rf, "ant": ant,
                               "err": str(e)[:100]})
                    continue
                # honest video quality on the captured stream
                prog = 3 if rf in (34, 35, 36) else 1
                frames, errs, gpm = null_sink(prog)
                log_event({"event": "QUALITY", "cycle": cycle, "rf": rf,
                           "ant": ant, "mer_med": s.get("mer_med"),
                           "mer_p10": s.get("mer_p10"), "hdr": s.get("hdr"),
                           "rs_bad": s.get("rs_bad"),
                           "rs_pkts": s.get("rs_pkts"),
                           "frames": frames, "decode_errors": errs,
                           "glitch_per_min": gpm})
        time.sleep(90)
    # restore the panel for the evening
    env = dict(os.environ)
    env["PATH"] = r"C:\Program Files\SDRplay\API\x64;" + env.get("PATH", "")
    subprocess.Popen([PY, "-u", str(HERE / "tv_tuna_panel.py")], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log_event({"event": "PANEL-RESTORED"})


def main():
    log_event({"event": "UI-LAB-START", "end_at": END_AT})
    bugs = phase_ui_stress()
    log_event({"event": "PHASE-A-COMPLETE", "bugs": len(bugs)})
    phase_quality_sweep()
    log_event({"event": "UI-LAB-END"})


if __name__ == "__main__":
    main()
