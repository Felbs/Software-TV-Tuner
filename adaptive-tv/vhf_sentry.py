"""vhf_sentry.py — patient fisherman for VHF television.

Every CHECK_MIN minutes (radio permitting), meters RF7 and RF9 for 45 s
each and logs the MER. The moment either holds >= TRIGGER dB, it
auto-tunes that channel's .1 service, announces by voice, and exits.
Time-of-day does the aiming we can't: VHF breathes with the clock.

Run after the evening antenna is settled:
  python vhf_sentry.py            # defaults: 30 min, trigger 15.5
"""
import json
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8642"
CHECK_MIN = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
TRIGGER = float(sys.argv[2]) if len(sys.argv) > 2 else 15.5
WATCH = [(7, 1, "7.1", "WJLA ABC"), (9, 1, "9.1", "WUSA CBS")]
LOG = r"Z:\src\adaptive-tv\lab\vhf_sentry.jsonl"


def api(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def log(rec):
    rec["iso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)


def say(text):
    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         "Add-Type -AssemblyName System.Speech; "
         "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
         "$s.Speak('" + text.replace("'", "") + "')"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def radio_busy():
    try:
        st = api("/api/status")
        m = api("/api/meter")
        return (st.get("rf") is not None or st.get("tuning")
                or st["scan"]["running"]
                or (m.get("rf") is not None and not m.get("watching")))
    except Exception:
        return True


log({"event": "sentry_start", "trigger": TRIGGER, "check_min": CHECK_MIN})
while True:
    if radio_busy():
        log({"event": "radio_busy_skip"})
        time.sleep(300)
        continue
    for rf, prog, virt, name in WATCH:
        api("/api/meter", {"rf": rf})
        time.sleep(45)
        mers = []
        for _ in range(6):
            m = api("/api/meter")
            if m.get("mer_last") is not None:
                mers.append(m["mer_last"])
            time.sleep(4)
        api("/api/meter/stop", {})
        med = sorted(mers)[len(mers) // 2] if mers else None
        log({"rf": rf, "mer_median": med, "n": len(mers)})
        if med is not None and med >= TRIGGER:
            log({"event": "FISH_ON", "rf": rf, "mer": med})
            say(f"V H F alert: channel {virt.split('.')[0]} is decodable "
                f"right now at {med:.1f} dee bee. Tuning it.")
            api("/api/tune", {"rf": rf, "prog": prog,
                              "virt": virt, "name": name})
            sys.exit(0)
        time.sleep(5)
    time.sleep(CHECK_MIN * 60)
