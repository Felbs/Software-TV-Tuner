"""flat_tone.py v3 — aiming audio with VOICE announcements.

Continuous pitch beeps for instant motion feedback (higher = better),
plus a spoken readout every few seconds: the actual number, and
"down from best" hints so better-vs-worse is words, not pitch memory.
Auto-detects the panel's active mode (MER meter or flatness sweep).
"""
import json
import subprocess
import time
import urllib.request
import winsound

BASE = "http://127.0.0.1:8642"


def say(text):
    # SAPI via PowerShell — fire and forget, ~no latency for short phrases
    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         "Add-Type -AssemblyName System.Speech; "
         "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
         "$s.Rate = 3; $s.Speak('" + text.replace("'", "") + "')"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


best = None
last_say = 0.0
last_best_hint = 0.0
say("aiming voice ready")
print("tone+voice daemon running", flush=True)
while True:
    try:
        with urllib.request.urlopen(BASE + "/api/meter", timeout=3) as r:
            m = json.loads(r.read())
        flat = m.get("flat") or {}
        rip = flat.get("ripple")
        mer = m.get("mer_last")
        if rip is not None:
            score, val, unit = 28.0 - rip, rip, "ripple"
            good = max(0.0, min(1.0, score / 22.0))
        elif mer is not None:
            score, val, unit = mer, mer, "mer"
            good = max(0.0, min(1.0, (mer - 8.0) / 12.0))
        else:
            time.sleep(0.5)
            continue
        freq = int(200 + good * 1400)
        now = time.time()
        if best is None or score > best + 0.25:
            newbest = best is not None
            best = score
            if newbest:
                winsound.Beep(1800, 90)
                winsound.Beep(2200, 140)
                say("new best, %.1f" % val)
                last_say = now
        # spoken readout every 6 s; add a gap-to-best hint every 20 s
        if now - last_say > 6:
            phrase = "%.1f" % val
            if best is not None and unit == "mer" and score < best - 0.8 \
                    and now - last_best_hint > 20:
                phrase += ", down from best %.1f" % best
                last_best_hint = now
            say(phrase)
            last_say = now
        winsound.Beep(freq, 200)
        time.sleep(0.12)
    except KeyboardInterrupt:
        break
    except Exception:
        time.sleep(1)
